#!/usr/bin/env python3
"""Fail-closed preflight for token billing on an isolated staging target.

Never prints a payment credential, authorization response, or full Mongo URI.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from urllib.parse import urlsplit

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from staging_config import validate_staging_configuration


REQUIRED_STRIPE_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.expired",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}
REQUIRED_PAYPAL_EVENTS = {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.SALE.COMPLETED"}


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required staging configuration is missing: {name}")
    return value


async def verify_mongo_transactions(mongo_uri: str, db_name: str) -> None:
    client = AsyncIOMotorClient(
        mongo_uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000
    )
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("MongoDB hello response does not identify a replica set")
        if not hello.get("logicalSessionTimeoutMinutes"):
            raise RuntimeError("MongoDB logical sessions are unavailable")
        target_db = client[db_name]
        # Pre-create the collection outside the transaction so this works on MongoDB
        # versions that forbid implicit collection creation inside a transaction.
        await target_db.token_staging_preflight.create_index("purpose")
        probe_id = f"token-billing-preflight-{uuid.uuid4()}"

        class _IntentionalAbort(Exception):
            pass

        async def transaction(session):
            await target_db.token_staging_preflight.insert_one(
                {"_id": probe_id, "purpose": "transaction-capability-check"},
                session=session,
            )
            raise _IntentionalAbort()

        async with await client.start_session() as session:
            try:
                await session.with_transaction(transaction)
            except _IntentionalAbort:
                pass
            else:
                raise RuntimeError("MongoDB preflight transaction did not abort as expected")
        if await target_db.token_staging_preflight.count_documents({"_id": probe_id}):
            raise RuntimeError("MongoDB transaction rollback left a staging probe record behind")
    except Exception as exc:
        raise RuntimeError(
            "MongoDB replica-set transaction check failed "
            f"({type(exc).__name__}); check staging URI permissions and replica-set health"
        ) from None
    finally:
        client.close()


async def verify_api_uses_staging_database(api_url: str, mongo_uri: str, db_name: str) -> None:
    """Verify the remote API authenticates against the same seeded staging user in this DB."""
    email = require("STAGING_SUPERADMIN_EMAIL").lower()
    password = require("STAGING_SUPERADMIN_PASSWORD")
    client = AsyncIOMotorClient(
        mongo_uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000
    )
    try:
        staged_user = await client[db_name].users.find_one(
            {"email": email}, {"_id": 0, "id": 1, "role": 1}
        )
    except Exception as exc:
        client.close()
        raise RuntimeError(
            f"Could not read the seeded super-admin from the isolated staging DB ({type(exc).__name__})"
        ) from None
    finally:
        client.close()
    if not staged_user or not staged_user.get("id"):
        raise RuntimeError("The configured super-admin was not found in the isolated staging database")

    async with httpx.AsyncClient(timeout=20) as client:
        login_response = await client.post(
            f"{api_url.rstrip('/')}/api/auth/login",
            json={"email": email, "password": password},
        )
        if login_response.status_code != 200:
            raise RuntimeError(
                "Staging API login failed for the configured super-admin "
                f"(HTTP {login_response.status_code}); check seeded account and API/database settings"
            )
        try:
            session = login_response.json()
            token = session["access_token"]
            api_user = session["user"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Staging API login response was missing the expected session fields") from None
        if api_user.get("id") != staged_user["id"]:
            raise RuntimeError(
                "Staging API authenticated a different super-admin record than the isolated MongoDB target; "
                "verify the staging API database configuration"
            )
        wallet_response = await client.get(
            f"{api_url.rstrip('/')}/api/billing/wallet",
            headers={"Authorization": f"Bearer {token}"},
        )
        if wallet_response.status_code != 200:
            raise RuntimeError(
                "Staging API token-wallet probe failed "
                f"(HTTP {wallet_response.status_code}); check API transaction support and staging database wiring"
            )
        wallet = wallet_response.json()
        if not isinstance(wallet.get("balance"), int) or not isinstance(wallet.get("lots"), list):
            raise RuntimeError("Staging API token-wallet probe returned an unexpected response")


async def verify_stripe(api_url: str) -> None:
    key = require("STAGING_STRIPE_API_KEY")
    secret = require("STAGING_STRIPE_WEBHOOK_SECRET")
    endpoint_id = require("STAGING_STRIPE_WEBHOOK_ENDPOINT_ID")
    if not key.startswith("sk_test_"):
        raise RuntimeError("STAGING_STRIPE_API_KEY must be a Stripe test secret key")
    if not secret.startswith("whsec_"):
        raise RuntimeError("STAGING_STRIPE_WEBHOOK_SECRET does not have the expected format")
    expected_url = f"{api_url.rstrip('/')}/api/payments/stripe/webhook"

    async with httpx.AsyncClient(timeout=20) as client:
        account_response = await client.get(
            "https://api.stripe.com/v1/account", auth=(key, "")
        )
        if account_response.status_code != 200:
            raise RuntimeError(
                "Stripe test API authentication failed "
                f"(HTTP {account_response.status_code}); credential values were not logged"
            )
        account = account_response.json()
        if account.get("livemode") is not False:
            raise RuntimeError("Stripe credential did not resolve to test mode")

        endpoint_response = await client.get(
            f"https://api.stripe.com/v1/webhook_endpoints/{endpoint_id}",
            auth=(key, ""),
        )
        if endpoint_response.status_code != 200:
            raise RuntimeError(
                "Stripe webhook endpoint lookup failed "
                f"(HTTP {endpoint_response.status_code})"
            )
        endpoint = endpoint_response.json()
        if endpoint.get("url", "").rstrip("/") != expected_url:
            raise RuntimeError("Stripe webhook endpoint URL does not point to this staging API")
        if endpoint.get("status") != "enabled":
            raise RuntimeError("Stripe staging webhook endpoint is not enabled")
        enabled_events = set(endpoint.get("enabled_events") or [])
        if "*" not in enabled_events and not REQUIRED_STRIPE_EVENTS.issubset(enabled_events):
            missing = sorted(REQUIRED_STRIPE_EVENTS - enabled_events)
            raise RuntimeError(
                "Stripe staging webhook endpoint is missing required event types: "
                + ", ".join(missing)
            )


async def verify_paypal(api_url: str) -> None:
    client_id = require("STAGING_PAYPAL_CLIENT_ID")
    client_secret = require("STAGING_PAYPAL_SECRET")
    webhook_id = require("STAGING_PAYPAL_WEBHOOK_ID")
    if os.environ.get("PAYPAL_MODE", "").strip().lower() != "sandbox":
        raise RuntimeError("PAYPAL_MODE must be sandbox for staging")
    expected_url = f"{api_url.rstrip('/')}/api/payments/paypal/webhook"
    base_url = "https://api-m.sandbox.paypal.com"

    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            f"{base_url}/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        if token_response.status_code != 200:
            raise RuntimeError(
                "PayPal sandbox authentication failed "
                f"(HTTP {token_response.status_code}); credential values were not logged"
            )
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise RuntimeError("PayPal sandbox did not return an access token")
        response = await client.get(
            f"{base_url}/v1/notifications/webhooks/{webhook_id}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise RuntimeError(
                "PayPal sandbox webhook lookup failed "
                f"(HTTP {response.status_code})"
            )
        webhook = response.json()
        if webhook.get("url", "").rstrip("/") != expected_url:
            raise RuntimeError("PayPal webhook URL does not point to this staging API")
        actual_events = {
            item.get("name") for item in (webhook.get("event_types") or [])
            if isinstance(item, dict)
        }
        missing = sorted(REQUIRED_PAYPAL_EVENTS - actual_events)
        if missing:
            raise RuntimeError(
                "PayPal staging webhook is missing required event types: "
                + ", ".join(missing)
            )


async def main() -> None:
    api_host, web_host, db_name = validate_staging_configuration()
    mongo_uri = require("STAGING_MONGO_URL")
    await verify_mongo_transactions(mongo_uri, db_name)
    print(f"PASS: isolated MongoDB replica-set transaction rollback check ({db_name})")
    await verify_api_uses_staging_database(os.environ["STAGING_API_URL"], mongo_uri, db_name)
    print("PASS: staging API authenticates against the same seeded MongoDB super-admin and token wallet")
    await verify_stripe(os.environ["STAGING_API_URL"])
    print("PASS: Stripe test credentials, enabled webhook endpoint, and required event subscriptions")
    await verify_paypal(os.environ["STAGING_API_URL"])
    print("PASS: PayPal sandbox credentials, webhook destination, and required event subscriptions")
    print(f"PASS: staging target {api_host}; web origin {web_host}")
    print("Credential values and provider authorization tokens were not printed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"STAGING TOKEN BILLING PREFLIGHT FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
