"""Write-capable token-wallet integration tests; manual isolated-staging workflow only.

These tests use the real staging MongoDB wallet and maintenance routines. Every fixture
uses unique IDs and is deleted in finally blocks. Never run against production or a shared
preview database; staging_config provides the environment guard.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "tests"))

import httpx
import pytest
from unittest.mock import AsyncMock

from staging_config import validate_staging_configuration
from database import db
from token_billing import InsufficientTokens, TokenWalletService, utc_iso, utc_now


def run(coro):
    return asyncio.run(coro)


class ScopedCollection:
    """Add a fixture-specific predicate to reads/writes made by maintenance code."""

    def __init__(self, collection, key: str, value: str):
        self.collection = collection
        self.key = key
        self.value = value

    def query(self, query=None):
        result = dict(query or {})
        result[self.key] = self.value
        return result

    def find(self, query=None, projection=None, *args, **kwargs):
        return self.collection.find(self.query(query), projection, *args, **kwargs)

    async def find_one(self, query=None, *args, **kwargs):
        return await self.collection.find_one(self.query(query), *args, **kwargs)

    async def update_one(self, query, update, *args, **kwargs):
        return await self.collection.update_one(self.query(query), update, *args, **kwargs)

    async def delete_one(self, query=None, *args, **kwargs):
        return await self.collection.delete_one(self.query(query), *args, **kwargs)

    async def count_documents(self, query, *args, **kwargs):
        return await self.collection.count_documents(self.query(query), *args, **kwargs)


def make_scoped_maintenance_db(user_id: str):
    return SimpleNamespace(
        users=ScopedCollection(db.users, "id", user_id),
        token_activity_events=ScopedCollection(db.token_activity_events, "user_id", user_id),
        token_reservations=ScopedCollection(db.token_reservations, "user_id", user_id),
        token_ledger=ScopedCollection(db.token_ledger, "user_id", user_id),
        jobs=db.jobs,
    )


async def clean_user(user_id: str, payment_id: str | None = None):
    await db.token_ledger.delete_many({"user_id": user_id})
    await db.token_reservations.delete_many({"user_id": user_id})
    await db.token_lots.delete_many({"user_id": user_id})
    await db.token_wallets.delete_many({"user_id": user_id})
    await db.token_activity_events.delete_many({"user_id": user_id})
    await db.users.delete_many({"id": user_id})
    await db.billing_payments.delete_many({"user_id": user_id})
    if payment_id:
        await db.billing_payments.delete_many({"_id": payment_id})


def test_staging_guard_and_provider_variables_are_present():
    """Fail early if this suite was accidentally run outside the protected staging job."""
    api_host, web_host, db_name = validate_staging_configuration()
    assert os.environ.get("STAGING_STRIPE_MODE", "").lower() == "test"
    assert os.environ.get("STRIPE_MODE", "").lower() == "test"
    assert os.environ.get("STRIPE_API_KEY", "").startswith("sk_test_")
    assert os.environ.get("STRIPE_WEBHOOK_SECRET", "").startswith("whsec_")
    assert os.environ.get("PAYPAL_MODE", "").lower() == "sandbox"
    assert os.environ.get("PAYPAL_CLIENT_ID")
    assert os.environ.get("PAYPAL_SECRET")
    assert os.environ.get("PAYPAL_WEBHOOK_ID")
    assert api_host and web_host and any(
        marker in db_name.lower() for marker in ("staging", "stage", "stg", "test")
    )


def test_concurrent_reservations_never_overspend_wallet():
    user_id = f"staging-token-concurrency-{uuid.uuid4().hex}"
    wallet = TokenWalletService(db)

    async def scenario():
        await wallet.grant_tokens(
            user_id, 10, idempotency_key=f"staging-grant-{uuid.uuid4().hex}",
            reason="Staging concurrency seed grant", source="staging_test",
            expires_at=utc_now() + timedelta(days=2), metadata={"staging_test": True}
        )

        async def reserve(index):
            try:
                return await wallet.reserve_tokens(
                    user_id, 3, idempotency_key=f"staging-reserve-{index:02d}-{uuid.uuid4().hex[:12]}",
                    reason="Staging concurrent reservation",
                    metadata={"staging_test": True, "case": "concurrent"},
                )
            except InsufficientTokens as exc:
                return exc

        outcomes = await asyncio.gather(*(reserve(i) for i in range(8)))
        winners = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, InsufficientTokens)]
        assert len(winners) == 3, f"expected 3 reservations from 10 tokens, got {len(winners)}"
        assert len(failures) == 5, f"expected 5 insufficient-token rejections, got {len(failures)}"
        assert all(item["status"] == "reserved" for item in winners)

        for item in winners:
            await wallet.commit_reservation(
                user_id, item["reservation_id"], metadata={"staging_test": True}
            )
        summary = await wallet.wallet_summary(user_id)
        assert summary["balance"] == 1
        ledger = await db.token_ledger.find(
            {"user_id": user_id, "source": "reservation"}
        ).to_list(length=20)
        assert sum(int(item["delta"]) for item in ledger) == -9
        assert await db.token_reservations.count_documents(
            {"user_id": user_id, "status": "committed"}
        ) == 3

    try:
        run(scenario())
    finally:
        run(clean_user(user_id))


def test_interrupted_action_reconciliation_commits_or_releases_safely(monkeypatch):
    import token_maintenance

    user_id = f"staging-token-recovery-{uuid.uuid4().hex}"
    wallet = TokenWalletService(db)
    monkeypatch.setattr(token_maintenance, "wallet", wallet)
    monkeypatch.setattr(token_maintenance, "db", make_scoped_maintenance_db(user_id))

    async def scenario():
        await db.users.insert_one({
            "id": user_id, "role": "crew", "billing_mode": "tokens_v1",
            "created_at": utc_iso(), "is_verified": True,
        })
        await wallet.grant_tokens(
            user_id, 20, idempotency_key=f"staging-recovery-grant-{uuid.uuid4().hex}",
            reason="Staging recovery seed grant", source="staging_test",
            expires_at=utc_now() + timedelta(days=2), metadata={"staging_test": True}
        )

        committed_reservation = await wallet.reserve_tokens(
            user_id, 4, idempotency_key=f"staging-recovery-commit-{uuid.uuid4().hex}",
            reason="Staging interrupted action", metadata={"action_type": "profile_boost", "staging_test": True}
        )
        await db.users.update_one(
            {"id": user_id},
            {"$set": {"profile_boost_reservation_id": committed_reservation["reservation_id"]}}
        )
        commit_report = await token_maintenance._reconcile_reservations(limit=10)
        persisted = await db.token_reservations.find_one(
            {"reservation_id": committed_reservation["reservation_id"]}
        )
        assert persisted["status"] == "committed"
        assert commit_report["reservations_committed"] == 1
        assert (await wallet.wallet_summary(user_id))["balance"] == 16

        released_reservation = await wallet.reserve_tokens(
            user_id, 5, idempotency_key=f"staging-recovery-release-{uuid.uuid4().hex}",
            reason="Staging abandoned action", metadata={"action_type": "profile_boost", "staging_test": True}
        )
        await db.token_reservations.update_one(
            {"reservation_id": released_reservation["reservation_id"]},
            {"$set": {"expires_at": utc_iso(utc_now() - timedelta(seconds=5))}}
        )
        release_report = await token_maintenance._reconcile_reservations(limit=10)
        persisted = await db.token_reservations.find_one(
            {"reservation_id": released_reservation["reservation_id"]}
        )
        assert persisted["status"] == "released"
        assert release_report["reservations_released"] == 1
        assert (await wallet.wallet_summary(user_id))["balance"] == 16

    try:
        run(scenario())
    finally:
        run(clean_user(user_id))


def test_qualified_monthly_free_grant_is_deduplicated(monkeypatch):
    import token_maintenance

    user_id = f"staging-token-freegrant-{uuid.uuid4().hex}"
    wallet = TokenWalletService(db)
    monkeypatch.setattr(token_maintenance, "wallet", wallet)
    monkeypatch.setattr(token_maintenance, "db", make_scoped_maintenance_db(user_id))

    async def scenario():
        now = utc_now()
        anchor = now - timedelta(days=2)
        await db.users.insert_one({
            "id": user_id, "role": "crew", "billing_mode": "tokens_v1",
            "billing_subscription_status": "free", "billing_plan": "free_crew",
            "is_verified": True, "created_at": utc_iso(anchor),
            "token_free_period_anchor": utc_iso(anchor),
        })
        await db.token_activity_events.insert_many([
            {"user_id": user_id, "event_id": f"staging-free-post-{uuid.uuid4().hex}",
             "activity": "job_posting", "created_at": utc_iso(now - timedelta(hours=4))},
            {"user_id": user_id, "event_id": f"staging-free-apply-{uuid.uuid4().hex}",
             "activity": "job_application", "created_at": utc_iso(now - timedelta(hours=2))},
        ])

        first = await token_maintenance._issue_qualified_free_grants(limit=10)
        second = await token_maintenance._issue_qualified_free_grants(limit=10)
        assert first["free_accounts_qualified"] == 1
        assert first["free_grants"] == 1
        assert second["free_accounts_qualified"] == 0
        assert second["free_grants"] == 0
        expected_start = (await db.users.find_one({"id": user_id}))["last_free_token_grant_period"]
        assert await db.token_ledger.count_documents({
            "user_id": user_id, "idempotency_key": f"freegrant:{user_id}:{expected_start}",
            "operation": "grant",
        }) == 1
        assert (await wallet.wallet_summary(user_id))["balance"] == 10

    try:
        run(scenario())
    finally:
        run(clean_user(user_id))


def test_legacy_unused_job_credits_convert_once(monkeypatch):
    import token_maintenance

    user_id = f"staging-token-migration-{uuid.uuid4().hex}"
    wallet = TokenWalletService(db)
    monkeypatch.setattr(token_maintenance, "wallet", wallet)
    monkeypatch.setattr(token_maintenance, "db", make_scoped_maintenance_db(user_id))

    async def scenario():
        cutover = utc_now()
        subscription_end = cutover - timedelta(days=2)
        expected_period = (subscription_end - timedelta(days=30)).date().isoformat()
        user = {
            "id": user_id, "role": "contractor", "subscription_plan": "contractor_business",
            "subscription_end": subscription_end.isoformat(), "job_post_period": expected_period,
            "job_posts_used": 7,
        }
        await db.users.insert_one(user)
        first = await token_maintenance._convert_unused_legacy_job_credits(user, cutover_at=cutover)
        refreshed = await db.users.find_one({"id": user_id}, {"_id": 0})
        second = await token_maintenance._convert_unused_legacy_job_credits(refreshed, cutover_at=cutover)
        entry = await db.token_ledger.find_one({
            "user_id": user_id, "idempotency_key": f"legacy-post-credit:{user_id}:{expected_period}",
            "operation": "grant",
        })
        assert first == 130
        assert second == 0
        assert entry is not None
        assert entry["amount"] == 130
        assert entry["source"] == "legacy_credit_conversion"
        assert (await wallet.wallet_summary(user_id))["balance"] == 130
        saved = await db.users.find_one({"id": user_id})
        assert saved["legacy_credit_conversion_done"] is True

    try:
        run(scenario())
    finally:
        run(clean_user(user_id))


def _stripe_signature(payload: bytes, secret: str) -> str:
    timestamp = int(time.time())
    signed = str(timestamp).encode("utf-8") + b"." + payload
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_stripe_signed_webhook_redelivery_grants_token_pack_once():
    from server import app

    user_id = f"staging-token-stripe-{uuid.uuid4().hex}"
    payment_id = f"staging-token-payment-{uuid.uuid4().hex}"
    session_id = f"cs_test_staging_{uuid.uuid4().hex[:20]}"
    webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]

    async def scenario():
        await db.billing_payments.insert_one({
            "_id": payment_id, "id": payment_id, "user_id": user_id,
            "provider": "stripe", "kind": "token_pack", "provider_session_id": None,
            "amount_cents": 1000, "currency": "usd", "token_amount": 20,
            "token_expires_at": utc_iso(utc_now() + timedelta(days=45)),
            "catalog_version": 1, "status": "pending", "created_at": utc_iso(),
        })
        event = {
            "id": f"evt_test_{uuid.uuid4().hex[:18]}", "object": "event",
            "type": "checkout.session.completed", "livemode": False,
            "created": int(time.time()), "data": {"object": {
                "id": session_id, "object": "checkout.session", "mode": "payment",
                "payment_status": "paid", "amount_total": 1000, "currency": "usd",
                "subscription": None,
                "metadata": {
                    "billing_system": "token_wallet_v1", "billing_payment_id": payment_id,
                    "user_id": user_id, "kind": "token_pack", "catalog_version": "1",
                },
            }},
        }
        payload = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {
            "Stripe-Signature": _stripe_signature(payload, webhook_secret),
            "Content-Type": "application/json",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://staging.test") as client:
            first = await client.post("/api/payments/stripe/webhook", content=payload, headers=headers)
            second = await client.post("/api/payments/stripe/webhook", content=payload, headers=headers)
        assert first.status_code == 200, f"first Stripe event returned HTTP {first.status_code}"
        assert second.status_code == 200, f"redelivered Stripe event returned HTTP {second.status_code}"
        assert (await db.billing_payments.find_one({"_id": payment_id}))["status"] == "fulfilled"
        assert await db.token_ledger.count_documents({
            "user_id": user_id, "idempotency_key": f"tokenpack:{payment_id}", "operation": "grant"
        }) == 1
        summary = await TokenWalletService(db).wallet_summary(user_id)
        assert summary["balance"] == 20

    try:
        run(scenario())
    finally:
        run(clean_user(user_id, payment_id))


def test_paypal_capture_webhook_redelivery_fulfills_token_pack_once(monkeypatch):
    from server import app
    import routes.payment_routes as payment_routes

    # PayPal signatures include provider-generated transmission headers that a synthetic
    # fixture cannot validly produce. The preflight separately authenticates the sandbox
    # app and validates this staging webhook registration. This test focuses on HTTP
    # dispatch and duplicate fulfillment once the provider event has been verified.
    monkeypatch.setattr(payment_routes, "_verify_paypal_webhook", AsyncMock(return_value=None))

    user_id = f"staging-token-paypal-{uuid.uuid4().hex}"
    payment_id = f"staging-token-payment-{uuid.uuid4().hex}"
    order_id = f"STAGINGORDER{uuid.uuid4().hex[:18]}"

    async def scenario():
        await db.billing_payments.insert_one({
            "_id": payment_id, "id": payment_id, "user_id": user_id,
            "provider": "paypal", "kind": "token_pack", "provider_session_id": order_id,
            "amount_cents": 1000, "currency": "usd", "token_amount": 20,
            "token_expires_at": utc_iso(utc_now() + timedelta(days=45)),
            "catalog_version": 1, "status": "pending", "created_at": utc_iso(),
        })
        event = {
            "id": f"WH-STAGING-{uuid.uuid4().hex[:18]}",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "create_time": utc_iso(),
            "resource": {
                "id": f"STAGINGCAPTURE{uuid.uuid4().hex[:10]}",
                "status": "COMPLETED",
                "amount": {"value": "10.00", "currency_code": "USD"},
                "supplementary_data": {"related_ids": {"order_id": order_id}},
            },
        }
        payload = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://staging.test") as client:
            first = await client.post(
                "/api/payments/paypal/webhook", content=payload,
                headers={"Content-Type": "application/json"},
            )
            second = await client.post(
                "/api/payments/paypal/webhook", content=payload,
                headers={"Content-Type": "application/json"},
            )
        assert first.status_code == 200, f"first PayPal event returned HTTP {first.status_code}"
        assert second.status_code == 200, f"redelivered PayPal event returned HTTP {second.status_code}"
        assert (await db.billing_payments.find_one({"_id": payment_id}))["status"] == "fulfilled"
        assert await db.token_ledger.count_documents({
            "user_id": user_id, "idempotency_key": f"tokenpack:{payment_id}", "operation": "grant"
        }) == 1
        assert (await TokenWalletService(db).wallet_summary(user_id))["balance"] == 20

    try:
        run(scenario())
    finally:
        run(clean_user(user_id, payment_id))
