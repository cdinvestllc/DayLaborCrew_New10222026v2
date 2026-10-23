"""Token-billing checkout and verified provider fulfillment.

This module owns token-v1 Stripe Checkout subscriptions/one-off packs and PayPal billing.
Provider events are dispatched from the existing verified payment webhook routes. A wallet
grant is keyed by subscription + billing period (or pack purchase id), so retries cannot
mint duplicate tokens.
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
import stripe as stripe_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import DuplicateKeyError

from auth import get_current_user
from database import db
from routes import payment_routes as legacy_payments
from token_billing import (
    BillingCatalog, DEFAULT_BILLING_CATALOG, IdempotencyConflict, InsufficientTokens,
    TokenBillingError, TokenStoreUnavailable, TokenWalletService, utc_iso, utc_now
)
from routes.token_billing_routes import _active_catalog

router = APIRouter()
wallet = TokenWalletService(db)
TOKEN_METADATA_KEY = "billing_system"
TOKEN_METADATA_VALUE = "token_wallet_v1"
ADMIN_ROLES = {"admin", "super_admin"}


class CheckoutBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_url: str = Field(min_length=8, max_length=2048)
    request_key: str = Field(min_length=8, max_length=128)


class SubscriptionCheckoutRequest(CheckoutBase):
    interval: Literal["month", "year"] = "month"


class TokenPackCheckoutRequest(CheckoutBase):
    pass


class CaptureResult(BaseModel):
    status: str
    order_id: str
    payment_status: str
    already_processed: bool = False


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {key: _to_dict(v) if isinstance(v, dict) else v for key, v in value.items()}
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value) if hasattr(value, "items") else {}


def _required_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _stable_provider_key(namespace: str, value: str) -> str:
    return f"{namespace}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:48]}"


def _period_label(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


def _add_months_anchored(anchor: datetime, month_index: int) -> datetime:
    """Add months from the original day-of-month without drifting after short months."""
    anchor = anchor.astimezone(timezone.utc)
    month_number = anchor.month - 1 + month_index
    year = anchor.year + month_number // 12
    month = month_number % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def _amount_is_exact(actual: Any, expected_cents: int, currency: Any, expected_currency: str) -> bool:
    try:
        value = Decimal(str(actual))
        if not value.is_finite():
            return False
        if value > Decimal("1000000000") or value < 0:
            return False
        cents = int((value * 100).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return str(currency or "").lower() == expected_currency.lower() and cents == expected_cents


async def _indexes() -> None:
    await db.billing_payments.create_index(
        [("user_id", 1), ("provider", 1), ("kind", 1), ("request_key", 1)],
        unique=True, name="uniq_billing_payment_request"
    )
    await db.billing_payments.create_index(
        [("provider", 1), ("provider_session_id", 1)], unique=True,
        partialFilterExpression={"provider_session_id": {"$type": "string"}},
        name="uniq_billing_provider_session"
    )
    await db.billing_subscriptions.create_index(
        [("user_id", 1), ("provider", 1), ("request_key", 1)],
        unique=True, name="uniq_billing_subscription_request"
    )
    await db.billing_subscriptions.create_index(
        [("provider", 1), ("provider_subscription_id", 1)], unique=True,
        partialFilterExpression={"provider_subscription_id": {"$type": "string"}},
        name="uniq_billing_provider_subscription"
    )
    await db.billing_subscriptions.create_index(
        [("user_id", 1), ("status", 1)], name="billing_user_subscription_status"
    )
    await db.billing_subscriptions.create_index(
        [("user_id", 1)], unique=True,
        partialFilterExpression={"status": {"$in": ["pending", "checkout_completed", "active", "past_due", "suspended"]}},
        name="uniq_current_token_subscription_per_user"
    )


async def _catalog() -> tuple[int, BillingCatalog]:
    record = await _active_catalog()
    return int(record.get("version", 1)), BillingCatalog.model_validate(record.get("catalog") or DEFAULT_BILLING_CATALOG)


def _ensure_provider_enabled(catalog: BillingCatalog, provider: str) -> None:
    if not catalog.payment_providers.get(provider, False):
        raise HTTPException(status_code=503, detail=f"{provider.replace('_', ' ').title()} payments are not enabled")


async def _find_request(collection, user_id: str, provider: str, kind: str, request_key: str):
    return await collection.find_one({
        "user_id": user_id, "provider": provider, "kind": kind, "request_key": request_key
    }, {"_id": 0})


async def _create_payment_record(
    *, payment_id: str, user_id: str, provider: str, kind: str, request_key: str,
    amount_cents: int, currency: str, catalog_version: int, **extra
) -> dict:
    await _indexes()
    existing = await _find_request(db.billing_payments, user_id, provider, kind, request_key)
    if existing:
        return existing
    record = {
        "_id": payment_id, "user_id": user_id, "provider": provider, "kind": kind,
        "request_key": request_key, "amount_cents": amount_cents, "currency": currency,
        "catalog_version": catalog_version, "status": "pending",
        "id": payment_id, "created_at": utc_iso(), "updated_at": utc_iso(), **extra
    }
    try:
        await db.billing_payments.insert_one(record)
        return {key: value for key, value in record.items() if key != "_id"} | {"id": payment_id}
    except DuplicateKeyError:
        existing = await _find_request(db.billing_payments, user_id, provider, kind, request_key)
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="Payment request conflicted; retry with the same request key") from None


async def _validate_subscription_request(user: dict, plan_id: str, interval: str):
    version, catalog = await _catalog()
    plan = catalog.plans.get(plan_id)
    if not plan or not plan.enabled or plan.role not in {"crew", "contractor"}:
        raise HTTPException(status_code=400, detail="This subscription plan is not available")
    if plan.role != user.get("role"):
        raise HTTPException(status_code=403, detail="This plan is not available for your account type")
    if plan_id not in {"pro_crew", "pro_contractor"}:
        raise HTTPException(status_code=400, detail="Only Pro plans require a paid checkout")
    price_cents = plan.monthly_price_cents if interval == "month" else plan.annual_price_cents
    if price_cents <= 0:
        raise HTTPException(status_code=409, detail="This billing interval is not priced")
    return version, catalog, plan, price_cents


async def _begin_subscription(
    *, user: dict, provider: str, plan_id: str, interval: str, request_key: str,
    catalog_version: int, catalog: BillingCatalog, plan, price_cents: int,
    provider_plan_id: str | None = None
) -> dict:
    await _indexes()
    existing = await _find_request(db.billing_subscriptions, user["id"], provider, "subscription", request_key)
    if existing:
        if existing.get("plan_id") != plan_id or existing.get("billing_interval") != interval:
            raise HTTPException(status_code=409, detail="This idempotency key was already used for a different subscription plan or interval.")
        return existing
    live = await db.billing_subscriptions.find_one(
        {"user_id": user["id"], "status": {"$in": ["pending", "checkout_completed", "active", "past_due", "suspended"]}},
        {"_id": 0, "id": 1, "status": 1}
    )
    if live:
        raise HTTPException(status_code=409, detail="An existing token subscription is pending or active; manage it before starting another.")

    local_id = str(uuid.uuid4())
    now = utc_iso()
    record = {
        "_id": local_id, "id": local_id, "user_id": user["id"], "role": user["role"],
        "provider": provider, "kind": "subscription", "request_key": request_key,
        "plan_id": plan_id, "billing_interval": interval,
        "price_cents": price_cents, "currency": catalog.currency,
        "monthly_tokens": plan.monthly_tokens,
        "grant_expiry_days": catalog.token_policy.pro_grant_expiry_days,
        "catalog_version": catalog_version, "provider_plan_id": provider_plan_id,
        "provider_subscription_id": None, "status": "pending",
        "created_at": now, "updated_at": now
    }
    try:
        await db.billing_subscriptions.insert_one(record)
    except DuplicateKeyError:
        existing = await _find_request(db.billing_subscriptions, user["id"], provider, "subscription", request_key)
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="Subscription request conflicted; retry with the same request key") from None
    return {key: value for key, value in record.items() if key != "_id"}


async def _link_stripe_session(payment_id: str, session: dict) -> None:
    metadata = session.get("metadata") or {}
    payment = await db.billing_payments.find_one({"_id": payment_id}, {"_id": 0})
    if not payment or payment.get("provider") != "stripe":
        raise RuntimeError("Billing payment record was not found")
    if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE or metadata.get("billing_payment_id") != payment_id:
        raise RuntimeError("Stripe metadata did not match the token billing record")
    if metadata.get("user_id") != payment.get("user_id"):
        raise RuntimeError("Stripe checkout user did not match the token billing record")
    provider_session_id = session.get("id")
    if not provider_session_id or payment.get("provider_session_id") not in (None, provider_session_id):
        raise RuntimeError("Stripe checkout session did not match the token billing record")
    if not _amount_is_exact(
        Decimal(str(session.get("amount_total", 0))) / 100,
        int(payment["amount_cents"]), session.get("currency"), payment["currency"]
    ):
        raise RuntimeError("Stripe checkout amount or currency did not match the recorded payment")
    await db.billing_payments.update_one(
        {"_id": payment_id, "provider_session_id": {"$in": [None, provider_session_id]}},
        {"$set": {"provider_session_id": provider_session_id, "updated_at": utc_iso()}}
    )
    if payment.get("kind") == "subscription_checkout":
        local_sub_id = payment.get("subscription_id")
        stripe_sub_id = session.get("subscription")
        if isinstance(stripe_sub_id, dict):
            stripe_sub_id = stripe_sub_id.get("id")
        if not local_sub_id or not stripe_sub_id:
            raise RuntimeError("Stripe checkout is missing its subscription reference")
        sub = await db.billing_subscriptions.find_one({"_id": local_sub_id}, {"_id": 0})
        if not sub or sub.get("user_id") != payment.get("user_id"):
            raise RuntimeError("Stripe subscription did not match the local subscriber")
        if sub.get("provider_subscription_id") not in (None, stripe_sub_id):
            raise RuntimeError("Stripe subscription reference conflicted")
        await db.billing_subscriptions.update_one(
            {"_id": local_sub_id, "provider_subscription_id": {"$in": [None, stripe_sub_id]}},
            {"$set": {
                "provider_subscription_id": stripe_sub_id,
                "status": "checkout_completed" if sub.get("status") not in {"cancelled", "expired"} else sub.get("status"),
                "checkout_session_id": provider_session_id, "updated_at": utc_iso()
            }}
        )


async def _fulfill_token_pack(
    payment_id: str, *, provider: str, provider_reference: str,
    amount: Any, currency: Any
) -> dict:
    payment = await db.billing_payments.find_one({"_id": payment_id}, {"_id": 0})
    if not payment or payment.get("kind") != "token_pack" or payment.get("provider") != provider:
        raise RuntimeError("Token pack payment record was not found")
    if payment.get("status") == "fulfilled":
        return {"status": "fulfilled", "already_processed": True, "tokens": payment["token_amount"]}
    if payment.get("status") in {"cancelled", "canceled", "refunded", "expired"}:
        raise RuntimeError("Token pack payment can no longer be fulfilled")
    if not _amount_is_exact(amount, int(payment["amount_cents"]), currency, payment["currency"]):
        raise RuntimeError("Token pack capture amount or currency did not match its recorded order")
    expires_at = datetime.fromisoformat(payment["token_expires_at"])
    result = await wallet.grant_tokens(
        payment["user_id"], int(payment["token_amount"]),
        idempotency_key=f"tokenpack:{payment_id}", reason=f"Purchased token pack ({provider})",
        source="token_pack_purchase", expires_at=expires_at,
        metadata={
            "provider": provider, "provider_reference": provider_reference,
            "billing_payment_id": payment_id, "catalog_version": payment["catalog_version"]
        },
        reference_id=provider_reference
    )
    await db.billing_payments.update_one(
        {"_id": payment_id, "status": {"$nin": ["refunded", "cancelled", "canceled"]}},
        {"$set": {
            "status": "fulfilled", "provider_reference": provider_reference,
            "tokens_granted": int(payment["token_amount"]), "fulfilled_at": utc_iso(),
            "updated_at": utc_iso(), "ledger_entry_id": result["entry"].get("entry_id")
        }}
    )
    return {"status": "fulfilled", "already_processed": result["already_processed"], "tokens": int(payment["token_amount"]), "balance": result["balance"]}


async def _grant_subscription_period(
    sub: dict, *, period_start: datetime, period_end: datetime,
    provider_reference: str, payment_amount_cents: int
) -> dict:
    if int(sub.get("price_cents", -1)) != payment_amount_cents:
        raise RuntimeError("Subscription invoice amount did not match the recorded plan price")
    if sub.get("status") in {"refunded", "fraud", "expired"}:
        raise RuntimeError("Subscription is not eligible for a token grant")
    period_start = period_start.astimezone(timezone.utc)
    period_end = period_end.astimezone(timezone.utc)
    period_label = _period_label(period_start)
    idempotency_key = f"subgrant:{sub['id']}:{period_label}"
    expires_at = period_start + timedelta(days=int(sub.get("grant_expiry_days", 60)))
    if expires_at <= utc_now():
        # An old/replayed paid invoice must not mint an already-expired lot or abort webhook retries forever.
        await db.billing_subscriptions.update_one(
            {"_id": sub["id"]},
            {"$addToSet": {"skipped_expired_periods": period_label},
             "$set": {"last_paid_reference": provider_reference, "updated_at": utc_iso()}}
        )
        return {"status": "skipped_expired_period", "period": period_label, "already_processed": True}

    result = await wallet.grant_tokens(
        sub["user_id"], int(sub["monthly_tokens"]),
        idempotency_key=idempotency_key,
        reason=f"{sub['plan_id']} token allowance for {period_label}",
        source="subscription_grant", expires_at=expires_at,
        # Metadata/references are deliberately stable across invoice webhooks and the
        # annual-release scheduler. The same subscription/month must be byte-for-byte
        # idempotent regardless of which delivery path wins the race.
        metadata={
            "provider": sub["provider"], "billing_subscription_id": sub["id"],
            "billing_period": period_label, "catalog_version": sub["catalog_version"]
        },
        reference_id=sub.get("provider_subscription_id") or sub["id"]
    )
    next_grant_at = None
    next_grant_index = None
    if sub.get("billing_interval") == "year":
        anchor = datetime.fromisoformat(sub.get("current_period_start") or utc_iso(period_start))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        next_grant_index = int(sub.get("next_grant_index", 1))
        candidate = _add_months_anchored(anchor, next_grant_index)
        if candidate < period_end:
            next_grant_at = utc_iso(candidate)
            next_grant_index += 1
    patch = {
        "last_paid_reference": provider_reference,
        "last_granted_period": period_label,
        "current_period_start": utc_iso(period_start),
        "current_period_end": utc_iso(period_end),
        "next_token_grant_at": next_grant_at,
        "next_grant_index": next_grant_index,
        "updated_at": utc_iso()
    }
    # Do not resurrect a locally canceled subscription if an older invoice event is delivered late.
    await db.billing_subscriptions.update_one(
        {"_id": sub["id"], "status": {"$nin": ["cancelled", "expired", "refunded"]}},
        {"$set": {"status": "active", "first_grant_at": sub.get("first_grant_at") or utc_iso(), **patch}}
    )
    await db.users.update_one(
        {"id": sub["user_id"]},
        {"$set": {
            "billing_plan": sub["plan_id"], "billing_subscription_id": sub["id"],
            "billing_subscription_status": "active", "billing_subscription_provider": sub["provider"],
            "billing_subscription_interval": sub.get("billing_interval"),
            "billing_subscription_period_end": utc_iso(period_end),
            "billing_subscription_tokens_per_month": int(sub["monthly_tokens"])
        }}
    )
    return {
        "status": "fulfilled", "tokens": int(sub["monthly_tokens"]),
        "already_processed": result["already_processed"], "balance": result["balance"],
        "period": period_label
    }


async def _invoice_period(invoice: dict) -> tuple[datetime, datetime] | None:
    lines = (invoice.get("lines") or {}).get("data") or []
    for line in lines:
        if (line.get("parent") or {}).get("subscription_item_details", {}).get("proration") or line.get("proration"):
            continue
        period = line.get("period") or {}
        try:
            start = datetime.fromtimestamp(int(period["start"]), timezone.utc)
            end = datetime.fromtimestamp(int(period["end"]), timezone.utc)
            if end > start:
                return start, end
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return None


async def _retrieve_stripe_object(resource, object_id: str):
    function = getattr(resource, "retrieve")
    return _to_dict(await asyncio.to_thread(function, object_id, api_key=legacy_payments._stripe_api_key()))


async def _process_stripe_invoice(invoice_value: Any, subscription_value: Any = None) -> bool:
    invoice = _to_dict(invoice_value)
    subscription_id = invoice.get("subscription")
    if isinstance(subscription_id, dict):
        subscription_id = subscription_id.get("id")
    if not subscription_id:
        parent = invoice.get("parent") or {}
        details = parent.get("subscription_details") or {}
        subscription_id = details.get("subscription")
    if not subscription_id:
        return False
    subscription = _to_dict(subscription_value) if subscription_value is not None else await _retrieve_stripe_object(stripe_sdk.Subscription, subscription_id)
    metadata = subscription.get("metadata") or {}
    if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE:
        return False
    local_sub_id = metadata.get("billing_subscription_id")
    sub = await db.billing_subscriptions.find_one({"_id": local_sub_id}, {"_id": 0}) if local_sub_id else None
    if not sub or sub.get("provider") != "stripe" or sub.get("provider_subscription_id") not in (None, subscription_id):
        raise RuntimeError("Stripe invoice subscription did not match the local billing record")
    if invoice.get("status") != "paid" or invoice.get("paid") is False:
        return True
    if invoice.get("billing_reason") not in {"subscription_create", "subscription_cycle"}:
        # No proration/upgrade token grants are supported until a separate plan-change policy exists.
        return True
    currency = str(invoice.get("currency") or "").lower()
    amount_paid = invoice.get("amount_paid")
    if currency != sub["currency"] or isinstance(amount_paid, bool) or int(amount_paid or -1) != int(sub["price_cents"]):
        raise RuntimeError("Stripe invoice amount/currency did not match the saved catalog snapshot")
    period = await _invoice_period(invoice)
    if not period:
        raise RuntimeError("Stripe invoice was missing its paid billing period")
    period_start, period_end = period
    if sub.get("billing_interval") == "month" and (period_end - period_start).days > 40:
        raise RuntimeError("Monthly Stripe invoice contained an unexpected billing period")
    await db.billing_subscriptions.update_one(
        {"_id": sub["id"], "provider_subscription_id": {"$in": [None, subscription_id]}},
        {"$set": {
            "provider_subscription_id": subscription_id,
            "current_period_start": sub.get("current_period_start") or utc_iso(period_start),
            "current_period_end": utc_iso(period_end), "updated_at": utc_iso()
        }}
    )
    if sub.get("billing_interval") == "year":
        # One grant is minted now; a protected scheduled runner releases the remaining
        # monthly allowances. Ledger idempotency protects concurrent runner/webhook retries.
        sub["current_period_start"] = sub.get("current_period_start") or utc_iso(period_start)
        sub["next_grant_index"] = 1
    result = await _grant_subscription_period(
        sub, period_start=period_start, period_end=period_end,
        provider_reference=invoice.get("id") or subscription_id,
        payment_amount_cents=int(amount_paid)
    )
    await db.billing_subscriptions.update_one(
        {"_id": sub["id"]},
        {"$addToSet": {"paid_invoice_ids": invoice.get("id")},
         "$set": {"last_invoice_id": invoice.get("id"), "updated_at": utc_iso()}}
    )
    await db.billing_payments.update_one(
        {"subscription_id": sub["id"], "kind": "subscription_checkout"},
        {"$set": {
            "status": "fulfilled", "last_paid_invoice_id": invoice.get("id"),
            "fulfilled_at": utc_iso(), "updated_at": utc_iso()
        }}
    )
    return True


async def _mark_stripe_invoice_failed(invoice_value: Any) -> bool:
    invoice = _to_dict(invoice_value)
    subscription_id = invoice.get("subscription")
    if isinstance(subscription_id, dict):
        subscription_id = subscription_id.get("id")
    if not subscription_id:
        parent = invoice.get("parent") or {}
        subscription_id = ((parent.get("subscription_details") or {}).get("subscription"))
    if not subscription_id:
        return False
    subscription = await _retrieve_stripe_object(stripe_sdk.Subscription, str(subscription_id))
    metadata = subscription.get("metadata") or {}
    if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE:
        return False
    local_id = metadata.get("billing_subscription_id")
    sub = await db.billing_subscriptions.find_one({"_id": local_id}, {"_id": 0}) if local_id else None
    if not sub or sub.get("provider_subscription_id") not in (None, subscription_id):
        raise RuntimeError("Failed Stripe invoice did not match the local token subscription")
    await db.billing_subscriptions.update_one(
        {"_id": local_id, "status": {"$nin": ["cancelled", "expired", "refunded"]}},
        {"$set": {"status": "past_due", "last_failed_invoice_id": invoice.get("id"), "updated_at": utc_iso()}}
    )
    await db.users.update_one({"billing_subscription_id": local_id}, {"$set": {"billing_subscription_status": "past_due"}})
    return True


async def handle_stripe_event(event_value: Any) -> bool:
    event = _to_dict(event_value)
    event_type = event.get("type")
    data = event.get("data") or {}
    obj = data.get("object") or {}
    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        metadata = obj.get("metadata") or {}
        if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE:
            return False
        if not obj.get("id"):
            raise RuntimeError("Stripe token checkout session did not contain an id")
        payment_id = metadata.get("billing_payment_id")
        if not payment_id:
            raise RuntimeError("Stripe token checkout metadata did not contain payment id")
        payment = await db.billing_payments.find_one({"_id": payment_id}, {"_id": 0})
        if not payment or payment.get("provider") != "stripe":
            raise RuntimeError("Stripe token checkout has no local payment record")
        expected_mode = "subscription" if payment.get("kind") == "subscription_checkout" else "payment"
        if obj.get("mode") != expected_mode:
            raise RuntimeError("Stripe checkout mode did not match the local billing operation")
        await _link_stripe_session(payment_id, obj)
        if payment.get("kind") == "token_pack":
            if obj.get("payment_status") != "paid":
                return True
            await _fulfill_token_pack(
                payment_id, provider="stripe", provider_reference=obj["id"],
                amount=Decimal(str(obj.get("amount_total", 0))) / 100, currency=obj.get("currency")
            )
        elif payment.get("kind") == "subscription_checkout":
            await db.billing_payments.update_one(
                {"_id": payment_id, "status": {"$ne": "fulfilled"}},
                {"$set": {"status": "provider_paid" if obj.get("payment_status") == "paid" else "pending",
                          "provider_subscription_id": obj.get("subscription"), "updated_at": utc_iso()}}
            )
        return True
    if event_type == "checkout.session.expired":
        metadata = obj.get("metadata") or {}
        if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE:
            return False
        payment_id = metadata.get("billing_payment_id")
        if payment_id:
            payment = await db.billing_payments.find_one({"_id": payment_id}, {"_id": 0})
            if payment and payment.get("status") != "fulfilled":
                await db.billing_payments.update_one(
                    {"_id": payment_id, "status": {"$nin": ["fulfilled", "refunded"]}},
                    {"$set": {"status": "expired", "updated_at": utc_iso()}}
                )
                if payment.get("kind") == "subscription_checkout" and payment.get("subscription_id"):
                    local_id = payment["subscription_id"]
                    await db.billing_subscriptions.update_one(
                        {"_id": local_id, "status": {"$in": ["pending", "checkout_completed"]},
                         "first_grant_at": {"$exists": False}},
                        {"$set": {"status": "expired", "updated_at": utc_iso()}}
                    )
        return True
    if event_type in {"invoice.paid", "invoice.payment_failed"}:
        return await _process_stripe_invoice(obj) if event_type == "invoice.paid" else await _mark_stripe_invoice_failed(obj)
    if event_type == "customer.subscription.updated":
        metadata = obj.get("metadata") or {}
        if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE:
            return False
        local_id = metadata.get("billing_subscription_id")
        if not local_id:
            return False
        sub = await db.billing_subscriptions.find_one({"_id": local_id}, {"_id": 0})
        if not sub or sub.get("provider") != "stripe":
            return False
        provider_status = str(obj.get("status") or "").lower()
        if provider_status in {"canceled", "incomplete_expired"}:
            new_status = "cancelled" if provider_status == "canceled" else "expired"
        elif provider_status in {"past_due", "unpaid", "paused"}:
            new_status = "past_due"
        elif provider_status == "active":
            new_status = "active" if sub.get("first_grant_at") else "checkout_completed"
        else:
            return True
        await db.billing_subscriptions.update_one(
            {"_id": local_id, "status": {"$nin": ["cancelled", "expired", "refunded"]}},
            {"$set": {"status": new_status, "updated_at": utc_iso()}}
        )
        await db.users.update_one({"billing_subscription_id": local_id}, {"$set": {"billing_subscription_status": new_status}})
        return True
    if event_type == "invoice.paid":
        return await _process_stripe_invoice(obj)
    if event_type == "customer.subscription.deleted":
        metadata = obj.get("metadata") or {}
        if metadata.get(TOKEN_METADATA_KEY) != TOKEN_METADATA_VALUE:
            return False
        local_sub_id = metadata.get("billing_subscription_id")
        if local_sub_id:
            await db.billing_subscriptions.update_one(
                {"_id": local_sub_id, "provider": "stripe"},
                {"$set": {"status": "cancelled", "cancelled_at": utc_iso(), "updated_at": utc_iso()}}
            )
            await db.users.update_one(
                {"billing_subscription_id": local_sub_id},
                {"$set": {"billing_subscription_status": "cancelled"}}
            )
        return True
    return False


async def _paypal_request(method: str, path: str, *, json_body: dict | None = None, request_id: str | None = None) -> dict:
    access_token = await legacy_payments.get_paypal_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id[:38]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, f"{legacy_payments.PAYPAL_BASE_URL}{path}",
                                            headers=headers, json=json_body)
            legacy_payments._raise_for_paypal(response)
            return response.json() if response.content else {}
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="PayPal is temporarily unavailable") from None


async def _paypal_plan_matches(plan_id: str, expected_cents: int, interval: str, currency: str) -> bool:
    plan = await _paypal_request("GET", f"/v1/billing/plans/{plan_id}")
    if plan.get("status") != "ACTIVE":
        return False
    expected_unit = "MONTH" if interval == "month" else "YEAR"
    for cycle in plan.get("billing_cycles") or []:
        frequency = cycle.get("frequency") or {}
        fixed_price = (cycle.get("pricing_scheme") or {}).get("fixed_price") or {}
        if (
            cycle.get("tenure_type") == "REGULAR"
            and str(frequency.get("interval_unit", "")).upper() == expected_unit
            and int(frequency.get("interval_count", 1)) == 1
            and _amount_is_exact(fixed_price.get("value"), expected_cents, fixed_price.get("currency_code"), currency)
        ):
            return True
    return False


async def _create_token_pack_record(user: dict, provider: str, request_key: str, catalog_version: int, catalog: BillingCatalog) -> dict:
    token_count = catalog.token_pack.tokens
    price_cents = catalog.token_pack.price_cents
    expiry = utc_now() + timedelta(days=catalog.token_policy.free_paid_token_expiry_days)
    if catalog.token_policy.pro_paid_tokens_expire_at == "billing_cycle_end":
        pro = await db.billing_subscriptions.find_one(
            {"user_id": user["id"], "status": "active", "current_period_end": {"$gt": utc_iso()}},
            {"_id": 0, "current_period_end": 1}
        )
        if pro and pro.get("current_period_end"):
            expiry = datetime.fromisoformat(pro["current_period_end"])
    existing = await _find_request(db.billing_payments, user["id"], provider, "token_pack", request_key)
    if existing:
        return existing
    payment_id = str(uuid.uuid4())
    return await _create_payment_record(
        payment_id=payment_id, user_id=user["id"], provider=provider, kind="token_pack",
        request_key=request_key, amount_cents=price_cents, currency=catalog.currency,
        catalog_version=catalog_version, token_amount=token_count, token_expires_at=utc_iso(expiry)
    )


@router.post("/payments/stripe/subscription-session")
async def stripe_subscription_session(data: SubscriptionCheckoutRequest, current_user: dict = Depends(get_current_user)):
    version, catalog, plan, price_cents = await _validate_subscription_request(current_user, "pro_crew" if current_user.get("role") == "crew" else "pro_contractor", data.interval)
    _ensure_provider_enabled(catalog, "stripe")
    existing = await _find_request(db.billing_subscriptions, current_user["id"], "stripe", "subscription", data.request_key)
    if existing and existing.get("checkout_url"):
        return {"url": existing["checkout_url"], "session_id": existing.get("checkout_session_id"), "subscription_id": existing["id"], "already_created": True}
    sub = await _begin_subscription(
        user=current_user, provider="stripe", plan_id="pro_crew" if current_user["role"] == "crew" else "pro_contractor",
        interval=data.interval, request_key=data.request_key, catalog_version=version,
        catalog=catalog, plan=plan, price_cents=price_cents
    )
    local_id = sub["id"]
    price_cents = int(sub["price_cents"])
    payment_id = f"checkout-{local_id}"
    await _create_payment_record(
        payment_id=payment_id, user_id=current_user["id"], provider="stripe", kind="subscription_checkout",
        request_key=data.request_key, amount_cents=price_cents, currency=catalog.currency, catalog_version=version,
        subscription_id=local_id
    )
    origin = legacy_payments._origin(None, {"origin_url": data.origin_url})
    metadata = {
        TOKEN_METADATA_KEY: TOKEN_METADATA_VALUE, "billing_payment_id": payment_id,
        "billing_subscription_id": local_id, "user_id": current_user["id"], "plan_id": sub["plan_id"],
        "billing_interval": data.interval, "catalog_version": str(version)
    }
    key = _stable_provider_key("token-sub", f"{current_user['id']}:{data.request_key}")
    try:
        session = await asyncio.to_thread(
            stripe_sdk.checkout.Session.create,
            mode="subscription",
            customer_email=current_user.get("email"),
            line_items=[{"price_data": {
                "currency": catalog.currency, "product_data": {"name": sub["plan_id"].replace("_", " ").title()},
                "unit_amount": price_cents, "recurring": {"interval": data.interval}
            }, "quantity": 1}],
            success_url=f"{origin}/subscription?billing_return=stripe-subscription&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/subscription?billing_cancelled=1",
            metadata=metadata,
            subscription_data={"metadata": metadata},
            api_key=legacy_payments._stripe_api_key(), idempotency_key=key
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Don't reveal provider internals; retrying with the same request_key is safe.
        raise HTTPException(status_code=502, detail="Stripe subscription checkout could not be created") from None
    await db.billing_subscriptions.update_one({"_id": local_id}, {"$set": {
        "checkout_session_id": session.id, "checkout_url": session.url, "updated_at": utc_iso()
    }})
    await db.billing_payments.update_one({"_id": payment_id}, {"$set": {
        "provider_session_id": session.id, "checkout_url": session.url, "updated_at": utc_iso()
    }})
    return {"url": session.url, "session_id": session.id, "subscription_id": local_id, "amount_cents": price_cents}


@router.post("/payments/stripe/token-pack-session")
async def stripe_token_pack_session(data: TokenPackCheckoutRequest, current_user: dict = Depends(get_current_user)):
    version, catalog = await _catalog()
    _ensure_provider_enabled(catalog, "stripe")
    if not catalog.token_pack.enabled:
        raise HTTPException(status_code=403, detail="Token packs are not available")
    payment = await _create_token_pack_record(current_user, "stripe", data.request_key, version, catalog)
    payment_id = payment.get("id") or payment.get("_id")
    if payment.get("checkout_url"):
        return {"url": payment["checkout_url"], "session_id": payment.get("provider_session_id"), "payment_id": payment_id, "already_created": True}
    origin = legacy_payments._origin(None, {"origin_url": data.origin_url})
    metadata = {
        TOKEN_METADATA_KEY: TOKEN_METADATA_VALUE, "billing_payment_id": payment_id,
        "user_id": current_user["id"], "kind": "token_pack", "catalog_version": str(version)
    }
    try:
        session = await asyncio.to_thread(
            stripe_sdk.checkout.Session.create, mode="payment",
            customer_email=current_user.get("email"),
            line_items=[{"price_data": {
                "currency": payment["currency"], "product_data": {"name": f'{payment["token_amount"]} DayLaborCrew Tokens'},
                "unit_amount": int(payment["amount_cents"])
            }, "quantity": 1}],
            success_url=f"{origin}/subscription?billing_return=stripe-pack&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/subscription?billing_cancelled=1",
            metadata=metadata, api_key=legacy_payments._stripe_api_key(),
            idempotency_key=_stable_provider_key("token-pack", f"{current_user['id']}:{data.request_key}")
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Stripe token checkout could not be created") from None
    await db.billing_payments.update_one({"_id": payment_id}, {"$set": {
        "provider_session_id": session.id, "checkout_url": session.url, "updated_at": utc_iso()
    }})
    return {"url": session.url, "session_id": session.id, "payment_id": payment_id, "amount_cents": payment["amount_cents"]}


@router.post("/payments/paypal/subscription")
async def paypal_subscription(data: SubscriptionCheckoutRequest, current_user: dict = Depends(get_current_user)):
    plan_id = "pro_crew" if current_user.get("role") == "crew" else "pro_contractor"
    version, catalog, plan, price_cents = await _validate_subscription_request(current_user, plan_id, data.interval)
    _ensure_provider_enabled(catalog, "paypal")
    env_name = f"PAYPAL_PLAN_ID_{plan_id.upper()}_{data.interval.upper()}"
    paypal_plan_id = _required_str(os.environ.get(env_name))
    if not paypal_plan_id:
        raise HTTPException(status_code=503, detail=f"PayPal recurring plan is not configured ({env_name})")
    if not await _paypal_plan_matches(paypal_plan_id, price_cents, data.interval, catalog.currency):
        raise HTTPException(status_code=503, detail="Configured PayPal plan does not match the active catalog price and interval")
    existing = await _find_request(db.billing_subscriptions, current_user["id"], "paypal", "subscription", data.request_key)
    if existing and existing.get("approval_url"):
        return {"subscription_id": existing["id"], "approve_url": existing["approval_url"], "already_created": True}
    sub = await _begin_subscription(
        user=current_user, provider="paypal", plan_id=plan_id, interval=data.interval,
        request_key=data.request_key, catalog_version=version, catalog=catalog,
        plan=plan, price_cents=price_cents, provider_plan_id=paypal_plan_id
    )
    price_cents = int(sub["price_cents"])
    paypal_plan_id = sub.get("provider_plan_id") or paypal_plan_id
    origin = legacy_payments._origin(None, {"origin_url": data.origin_url})
    try:
        response = await _paypal_request("POST", "/v1/billing/subscriptions", json_body={
            "plan_id": paypal_plan_id, "custom_id": sub["id"],
            "application_context": {
                "brand_name": "DayLaborCrew", "user_action": "SUBSCRIBE_NOW",
                "return_url": f"{origin}/subscription?billing_return=paypal-subscription&billing_subscription_id={sub['id']}",
                "cancel_url": f"{origin}/subscription?billing_cancelled=1"
            }
        }, request_id=_stable_provider_key("token-sub", f"{current_user['id']}:{data.request_key}")[:38])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="PayPal subscription checkout could not be created") from None
    approval_url = next((item.get("href") for item in response.get("links", []) if item.get("rel") in {"approve", "payer-action"}), None)
    provider_sub_id = response.get("id")
    if not approval_url or not provider_sub_id:
        raise HTTPException(status_code=502, detail="PayPal did not return a subscription approval link")
    await db.billing_subscriptions.update_one({"_id": sub["id"]}, {"$set": {
        "provider_subscription_id": provider_sub_id, "approval_url": approval_url, "updated_at": utc_iso()
    }})
    return {"subscription_id": sub["id"], "approve_url": approval_url, "amount_cents": price_cents}


@router.post("/payments/paypal/token-pack-order")
async def paypal_token_pack_order(data: TokenPackCheckoutRequest, current_user: dict = Depends(get_current_user)):
    version, catalog = await _catalog()
    _ensure_provider_enabled(catalog, "paypal")
    if not catalog.token_pack.enabled:
        raise HTTPException(status_code=403, detail="Token packs are not available")
    payment = await _create_token_pack_record(current_user, "paypal", data.request_key, version, catalog)
    payment_id = payment.get("id") or payment.get("_id")
    if payment.get("approval_url"):
        return {"order_id": payment.get("provider_session_id"), "approve_url": payment["approval_url"], "payment_id": payment_id, "already_created": True}
    origin = legacy_payments._origin(None, {"origin_url": data.origin_url})
    response = await _paypal_request("POST", "/v2/checkout/orders", json_body={
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": payment_id, "custom_id": payment_id,
            "description": f'{payment["token_amount"]} DayLaborCrew Tokens',
            "amount": {"currency_code": payment["currency"].upper(),
                       "value": f'{Decimal(payment["amount_cents"]) / Decimal(100):.2f}'}
        }],
        "application_context": {
            "return_url": f"{origin}/subscription?billing_return=paypal-pack&billing_payment_id={payment_id}",
            "cancel_url": f"{origin}/subscription?billing_cancelled=1"
        }
    }, request_id=_stable_provider_key("token-pack", f"{current_user['id']}:{data.request_key}")[:38])
    order_id = response.get("id")
    approve_url = next((item.get("href") for item in response.get("links", []) if item.get("rel") == "approve"), None)
    if not order_id or not approve_url:
        raise HTTPException(status_code=502, detail="PayPal did not return an approval link")
    await db.billing_payments.update_one({"_id": payment_id}, {"$set": {
        "provider_session_id": order_id, "approval_url": approve_url, "updated_at": utc_iso()
    }})
    return {"order_id": order_id, "approve_url": approve_url, "payment_id": payment_id, "amount_cents": payment["amount_cents"]}


async def _capture_paypal_pack(order_id: str, user_id: str | None = None) -> dict:
    payment = await db.billing_payments.find_one({"provider": "paypal", "kind": "token_pack", "provider_session_id": order_id}, {"_id": 0})
    if not payment or (user_id and payment.get("user_id") != user_id):
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.get("status") == "fulfilled":
        return {"status": "COMPLETED", "payment_status": "fulfilled", "already_processed": True,
                "tokens": payment.get("tokens_granted", payment.get("token_amount"))}
    response = await _paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture")
    status = response.get("status")
    purchases = response.get("purchase_units") or []
    captures = ((purchases[0].get("payments") or {}).get("captures") or []) if purchases else []
    capture = captures[0] if captures else {}
    amount = capture.get("amount") or {}
    if status == "COMPLETED":
        result = await _fulfill_token_pack(
            payment.get("id") or payment["_id"], provider="paypal", provider_reference=capture.get("id") or order_id,
            amount=amount.get("value"), currency=amount.get("currency_code")
        )
        return {"status": "COMPLETED", "payment_status": result["status"], "already_processed": result.get("already_processed", False),
                "tokens": result.get("tokens"), "balance": result.get("balance")}
    return {"status": status or "UNKNOWN", "payment_status": payment.get("status", "pending"), "already_processed": False}


@router.post("/payments/paypal/token-pack-capture/{order_id}")
async def paypal_token_pack_capture(order_id: str, current_user: dict = Depends(get_current_user)):
    return await _capture_paypal_pack(order_id, current_user["id"])


@router.post("/payments/paypal/token-pack-capture-payment/{payment_id}")
async def paypal_token_pack_capture_payment(payment_id: str, current_user: dict = Depends(get_current_user)):
    payment = await db.billing_payments.find_one(
        {"_id": payment_id, "user_id": current_user["id"], "provider": "paypal", "kind": "token_pack"},
        {"_id": 0, "provider_session_id": 1}
    )
    if not payment or not payment.get("provider_session_id"):
        raise HTTPException(status_code=404, detail="Payment not found")
    return await _capture_paypal_pack(payment["provider_session_id"], current_user["id"])


@router.get("/payments/paypal/subscription-status/{subscription_id}")
async def paypal_subscription_status(subscription_id: str, current_user: dict = Depends(get_current_user)):
    sub = await db.billing_subscriptions.find_one({"_id": subscription_id, "user_id": current_user["id"], "provider": "paypal"}, {"_id": 0})
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.get("provider_subscription_id"):
        try:
            response = await _paypal_request("GET", f"/v1/billing/subscriptions/{sub['provider_subscription_id']}")
            if response.get("custom_id") != sub["id"] or response.get("plan_id") != sub.get("provider_plan_id"):
                raise HTTPException(status_code=409, detail="PayPal subscription does not match the saved billing record")
            provider_status = response.get("status")
            if provider_status in {"CANCELLED", "SUSPENDED", "EXPIRED"}:
                normalized = provider_status.lower()
                await db.billing_subscriptions.update_one({"_id": sub["id"]}, {"$set": {"status": normalized, "updated_at": utc_iso()}})
                await db.users.update_one({"billing_subscription_id": sub["id"]}, {"$set": {"billing_subscription_status": normalized}})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="PayPal subscription status is temporarily unavailable") from None
    fresh = await db.billing_subscriptions.find_one({"_id": sub["id"]}, {"_id": 0})
    return {"subscription_id": sub["id"], "status": (fresh or sub).get("status"),
            "payment_status": "paid" if (fresh or sub).get("first_grant_at") else "processing",
            "tokens_granted": bool((fresh or sub).get("first_grant_at"))}


@router.get("/payments/stripe/status/{session_id}")
async def stripe_token_payment_status(session_id: str, current_user: dict = Depends(get_current_user)):
    payment = await db.billing_payments.find_one(
        {"provider": "stripe", "provider_session_id": session_id, "user_id": current_user["id"]}, {"_id": 0}
    )
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    try:
        session = await _retrieve_stripe_object(stripe_sdk.checkout.Session, session_id)
        await handle_stripe_event({"type": "checkout.session.completed", "data": {"object": session}})
        if payment.get("kind") == "subscription_checkout":
            sub = await db.billing_subscriptions.find_one({"_id": payment["subscription_id"]}, {"_id": 0})
            provider_sub_id = (sub or {}).get("provider_subscription_id")
            if session.get("payment_status") == "paid" and provider_sub_id:
                stripe_sub = await _retrieve_stripe_object(stripe_sdk.Subscription, provider_sub_id)
                latest_invoice = stripe_sub.get("latest_invoice")
                if isinstance(latest_invoice, dict):
                    latest_invoice = latest_invoice.get("id")
                if latest_invoice:
                    invoice = await _retrieve_stripe_object(stripe_sdk.Invoice, latest_invoice)
                    if invoice.get("status") == "paid":
                        await _process_stripe_invoice(invoice, stripe_sub)
            sub = await db.billing_subscriptions.find_one({"_id": payment["subscription_id"]}, {"_id": 0})
            return {"status": (sub or {}).get("status", "pending"),
                    "payment_status": "paid" if (sub or {}).get("first_grant_at") else session.get("payment_status", "pending"),
                    "tokens_granted": bool((sub or {}).get("first_grant_at"))}
        fresh = await db.billing_payments.find_one({"_id": payment.get("id") or payment["_id"]}, {"_id": 0})
        return {"status": (fresh or {}).get("status", "pending"),
                "payment_status": "paid" if (fresh or {}).get("status") == "fulfilled" else session.get("payment_status", "pending"),
                "tokens_granted": bool((fresh or {}).get("tokens_granted"))}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Stripe payment status is temporarily unavailable") from None


@router.post("/payments/release-due-grants")
async def release_due_annual_subscription_grants(
    authorization: str | None = Header(default=None, alias="Authorization"),
    limit: int = Query(100, ge=1, le=500)
):
    """Scheduler endpoint: call daily with Authorization: Bearer BILLING_SCHEDULER_SECRET."""
    configured = _required_str(os.environ.get("BILLING_SCHEDULER_SECRET"))
    if not configured:
        raise HTTPException(status_code=503, detail="Billing grant scheduler is not configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    import secrets
    if not secrets.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="Unauthorized")
    now = utc_now()
    cursor = db.billing_subscriptions.find({
        "status": "active", "billing_interval": "year",
        "next_token_grant_at": {"$lte": utc_iso(now)},
        "current_period_end": {"$gt": utc_iso(now)}
    }).sort("next_token_grant_at", 1).limit(limit)
    due = await cursor.to_list(length=limit)
    granted = 0
    for sub_doc in due:
        sub = {key: value for key, value in sub_doc.items() if key != "_id"}
        if not sub.get("id"):
            continue
        anchor = datetime.fromisoformat(sub["current_period_start"])
        target = datetime.fromisoformat(sub["next_token_grant_at"])
        end = datetime.fromisoformat(sub["current_period_end"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        expected_period_start = _add_months_anchored(anchor, int(sub.get("next_grant_index", 1)))
        if _period_label(expected_period_start) != _period_label(target):
            # Never trust a mutated due-date independent of its known annual anchor.
            continue
        expiry = expected_period_start + timedelta(days=int(sub.get("grant_expiry_days", 60)))
        if expiry <= now:
            await db.billing_subscriptions.update_one(
                {"_id": sub["id"], "next_token_grant_at": sub_doc["next_token_grant_at"]},
                {"$addToSet": {"skipped_expired_periods": _period_label(expected_period_start)},
                 "$set": {"next_token_grant_at": utc_iso(_add_months_anchored(anchor, int(sub.get("next_grant_index", 1)) + 1)),
                          "next_grant_index": int(sub.get("next_grant_index", 1)) + 1, "updated_at": utc_iso()}}
            )
            continue
        key = f"subgrant:{sub['id']}:{_period_label(expected_period_start)}"
        result = await wallet.grant_tokens(
            sub["user_id"], int(sub["monthly_tokens"]), idempotency_key=key,
            reason=f"{sub['plan_id']} token allowance for {_period_label(expected_period_start)}",
            source="subscription_grant", expires_at=expiry,
            metadata={"provider": sub["provider"], "billing_subscription_id": sub["id"],
                      "billing_period": _period_label(expected_period_start),
                      "catalog_version": sub["catalog_version"]},
            reference_id=sub.get("provider_subscription_id") or sub["id"]
        )
        next_index = int(sub.get("next_grant_index", 1)) + 1
        next_at = _add_months_anchored(anchor, next_index)
        if next_at >= end:
            next_value = None
        else:
            next_value = utc_iso(next_at)
        await db.billing_subscriptions.update_one(
            {"_id": sub["id"], "status": "active", "next_token_grant_at": sub_doc["next_token_grant_at"]},
            {"$set": {"next_token_grant_at": next_value, "next_grant_index": next_index,
                      "last_granted_period": _period_label(expected_period_start), "updated_at": utc_iso()}}
        )
        if not result.get("already_processed"):
            granted += int(sub["monthly_tokens"])
    return {"processed_subscriptions": len(due), "tokens_granted": granted, "ran_at": utc_iso()}


async def handle_paypal_event(event_value: Any) -> bool:
    event = _to_dict(event_value)
    event_type = event.get("event_type")
    resource = event.get("resource") or {}
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        related = ((resource.get("supplementary_data") or {}).get("related_ids") or {})
        order_id = related.get("order_id") or resource.get("order_id")
        if not order_id:
            return False
        payment = await db.billing_payments.find_one(
            {"provider": "paypal", "kind": "token_pack", "provider_session_id": order_id}, {"_id": 0}
        )
        if not payment:
            return False
        amount = resource.get("amount") or {}
        await _fulfill_token_pack(
            payment.get("id") or payment["_id"], provider="paypal",
            provider_reference=resource.get("id") or order_id,
            amount=amount.get("value"), currency=amount.get("currency_code")
        )
        return True
    if event_type == "PAYMENT.SALE.COMPLETED":
        provider_sub_id = resource.get("billing_agreement_id") or resource.get("subscription_id")
        if not provider_sub_id:
            return False
        sub = await db.billing_subscriptions.find_one(
            {"provider": "paypal", "provider_subscription_id": provider_sub_id}, {"_id": 0}
        )
        if not sub:
            return False
        amount = resource.get("amount") or {}
        if not _amount_is_exact(amount.get("total"), int(sub["price_cents"]), amount.get("currency"), sub["currency"]):
            raise RuntimeError("PayPal subscription sale did not match the saved catalog price")
        sale_id = _required_str(resource.get("id"))
        if not sale_id:
            raise RuntimeError("PayPal subscription sale did not include a transaction id")
        try:
            sale_time = datetime.fromisoformat(str(resource.get("create_time") or event.get("create_time")).replace("Z", "+00:00"))
            if sale_time.tzinfo is None:
                sale_time = sale_time.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            sale_time = utc_now()
        response = await _paypal_request("GET", f"/v1/billing/subscriptions/{provider_sub_id}")
        if response.get("custom_id") != sub["id"] or response.get("plan_id") != sub.get("provider_plan_id"):
            raise RuntimeError("PayPal subscription metadata did not match its local billing record")
        info = response.get("billing_info") or {}
        next_billing = info.get("next_billing_time")
        try:
            period_end = datetime.fromisoformat(str(next_billing).replace("Z", "+00:00")) if next_billing else (
                _add_months_anchored(sale_time, 1 if sub["billing_interval"] == "month" else 12))
            if period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=timezone.utc)
        except ValueError:
            period_end = _add_months_anchored(sale_time, 1 if sub["billing_interval"] == "month" else 12)
        # PayPal sale IDs are stable across redelivery. The month label prevents two event
        # variants of the same subscription period from creating a second monthly allowance.
        start_period = datetime(sale_time.year, sale_time.month, 1, tzinfo=timezone.utc)
        if sub["billing_interval"] == "year":
            sub["current_period_start"] = utc_iso(sale_time)
            sub["next_grant_index"] = 1
        result = await _grant_subscription_period(
            sub, period_start=start_period, period_end=period_end,
            provider_reference=sale_id, payment_amount_cents=int(sub["price_cents"])
        )
        await db.billing_subscriptions.update_one(
            {"_id": sub["id"], "provider": "paypal", "status": {"$nin": ["cancelled", "expired", "refunded"]}},
            {"$addToSet": {"paid_sale_ids": sale_id},
             "$set": {"current_period_start": sub.get("current_period_start") or utc_iso(sale_time),
                      "current_period_end": utc_iso(period_end),
                      "next_token_grant_at": utc_iso(_add_months_anchored(sale_time, 1)) if sub["billing_interval"] == "year" else None,
                      "next_grant_index": 1 if sub["billing_interval"] == "year" else None,
                      "status": "active", "updated_at": utc_iso()}}
        )
        return True
    if event_type in {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED"}:
        provider_sub_id = resource.get("id")
        sub = await db.billing_subscriptions.find_one({"provider": "paypal", "provider_subscription_id": provider_sub_id}, {"_id": 0})
        if not sub:
            return False
        status = {"BILLING.SUBSCRIPTION.CANCELLED": "cancelled", "BILLING.SUBSCRIPTION.SUSPENDED": "suspended",
                  "BILLING.SUBSCRIPTION.EXPIRED": "expired"}[event_type]
        await db.billing_subscriptions.update_one({"_id": sub["id"]}, {"$set": {"status": status, "updated_at": utc_iso()}})
        await db.users.update_one({"billing_subscription_id": sub["id"]}, {"$set": {"billing_subscription_status": status}})
        return True
    return False
