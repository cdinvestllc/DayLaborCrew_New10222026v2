"""Payment fulfillment invariants for token-pack and recurring token grants."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "daylaborcrew_test")
os.environ.setdefault("JWT_SECRET_KEY", "token-payment-tests-secret-at-least-32-chars")
os.environ.setdefault("JWT_EXPIRE_HOURS", "24")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
os.environ.setdefault("EMERGENT_LLM_KEY", "token-payment-test-placeholder")
os.environ.setdefault("TRUSTED_PROXY_HOPS", "1")

from routes import token_payment_routes as token_payments  # noqa: E402


class MemoryResult:
    modified_count = 1


class MemoryCollection:
    def __init__(self, *documents):
        self.documents = [dict(document) for document in documents]
        self.updates = []

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return dict(document)
        return None

    async def update_one(self, query, update, **kwargs):
        self.updates.append((dict(query), dict(update)))
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(update.get("$set", {}))
                for key, value in update.get("$addToSet", {}).items():
                    document.setdefault(key, [])
                    if value not in document[key]:
                        document[key].append(value)
                return MemoryResult()
        return MemoryResult()


class IdempotentFakeWallet:
    def __init__(self):
        self.calls = []
        self.keys = {}

    async def grant_tokens(self, user_id, amount, **kwargs):
        self.calls.append((user_id, amount, kwargs))
        key = kwargs["idempotency_key"]
        if key in self.keys:
            previous = self.keys[key]
            assert previous["kwargs"]["reason"] == kwargs["reason"]
            assert previous["kwargs"]["source"] == kwargs["source"]
            assert previous["kwargs"]["metadata"] == kwargs["metadata"]
            assert previous["kwargs"]["reference_id"] == kwargs["reference_id"]
            return {"entry": {"entry_id": "ledger-existing"}, "balance": amount,
                    "already_processed": True}
        self.keys[key] = {"user_id": user_id, "amount": amount, "kwargs": kwargs}
        return {"entry": {"entry_id": "ledger-first"}, "balance": amount,
                "already_processed": False}


def test_exact_amount_verification_rejects_price_currency_and_bad_values():
    assert token_payments._amount_is_exact("9.99", 999, "USD", "usd")
    assert not token_payments._amount_is_exact("9.98", 999, "USD", "usd")
    assert not token_payments._amount_is_exact("9.99", 999, "EUR", "usd")
    assert not token_payments._amount_is_exact("NaN", 999, "USD", "usd")
    assert not token_payments._amount_is_exact(None, 999, "USD", "usd")


def test_annual_grant_anchor_handles_month_ends_without_drift():
    jan_31 = datetime(2027, 1, 31, 15, 30, tzinfo=timezone.utc)
    feb = token_payments._add_months_anchored(jan_31, 1)
    mar = token_payments._add_months_anchored(jan_31, 2)
    assert (feb.year, feb.month, feb.day) == (2027, 2, 28)
    assert (mar.year, mar.month, mar.day) == (2027, 3, 31)


def test_duplicate_subscription_period_uses_same_ledger_idempotency_key(monkeypatch):
    wallet = IdempotentFakeWallet()
    database = SimpleNamespace(
        billing_subscriptions=MemoryCollection(),
        users=MemoryCollection()
    )
    monkeypatch.setattr(token_payments, "wallet", wallet)
    monkeypatch.setattr(token_payments, "db", database)

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=1)
    end = now + timedelta(days=28)
    sub = {
        "id": "billing-sub-123", "user_id": "user-123", "provider": "stripe",
        "provider_subscription_id": "sub_provider_123", "plan_id": "pro_crew",
        "monthly_tokens": 20, "price_cents": 999, "grant_expiry_days": 60,
        "catalog_version": 1, "billing_interval": "month"
    }
    first = asyncio.run(token_payments._grant_subscription_period(
        sub, period_start=start, period_end=end, provider_reference="in_first", payment_amount_cents=999
    ))
    second = asyncio.run(token_payments._grant_subscription_period(
        sub, period_start=start, period_end=end, provider_reference="in_redelivery", payment_amount_cents=999
    ))

    assert first["already_processed"] is False
    assert second["already_processed"] is True
    assert len(wallet.calls) == 2  # two delivery attempts, one unique ledger operation
    assert wallet.calls[0][2]["idempotency_key"] == wallet.calls[1][2]["idempotency_key"]
    assert wallet.calls[0][2]["metadata"] == wallet.calls[1][2]["metadata"]
    assert wallet.calls[0][2]["reference_id"] == wallet.calls[1][2]["reference_id"]
    assert wallet.calls[0][2]["idempotency_key"].startswith("subgrant:billing-sub-123:")


def test_annual_scheduler_and_invoice_paths_use_identical_grant_metadata(monkeypatch):
    wallet = IdempotentFakeWallet()
    database = SimpleNamespace(billing_subscriptions=MemoryCollection(), users=MemoryCollection())
    monkeypatch.setattr(token_payments, "wallet", wallet)
    monkeypatch.setattr(token_payments, "db", database)
    now = datetime.now(timezone.utc)
    anchor = now - timedelta(days=2)
    sub = {
        "id": "annual-sub-123", "user_id": "user-456", "provider": "stripe",
        "provider_subscription_id": "sub_annual_456", "plan_id": "pro_contractor",
        "monthly_tokens": 40, "price_cents": 1999, "grant_expiry_days": 60,
        "catalog_version": 3, "billing_interval": "year", "current_period_start": anchor.isoformat(),
        "next_grant_index": 1
    }
    period_start = token_payments._add_months_anchored(anchor, 1)
    period_end = anchor + timedelta(days=365)
    invoice_result = asyncio.run(token_payments._grant_subscription_period(
        sub, period_start=period_start, period_end=period_end,
        provider_reference="in_annual_cycle", payment_amount_cents=1999
    ))
    scheduled_sub = {**sub, "current_period_start": anchor.isoformat(), "next_grant_index": 1}
    scheduled_result = asyncio.run(token_payments._grant_subscription_period(
        scheduled_sub, period_start=period_start, period_end=period_end,
        provider_reference="sub_annual_456", payment_amount_cents=1999
    ))
    assert invoice_result["already_processed"] is False
    assert scheduled_result["already_processed"] is True
    assert wallet.calls[0][2]["idempotency_key"] == wallet.calls[1][2]["idempotency_key"]
    assert wallet.calls[0][2]["metadata"] == wallet.calls[1][2]["metadata"]


def test_unpaid_stripe_token_pack_never_calls_wallet_grant(monkeypatch):
    payment = {
        "_id": "pack-123", "id": "pack-123", "user_id": "user-123",
        "provider": "stripe", "kind": "token_pack", "amount_cents": 1000, "currency": "usd",
        "token_amount": 20, "token_expires_at": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
        "status": "pending", "provider_session_id": None
    }
    payments_collection = MemoryCollection(payment)
    fake_wallet = AsyncMock()
    monkeypatch.setattr(token_payments, "wallet", fake_wallet)
    monkeypatch.setattr(token_payments, "db", SimpleNamespace(billing_payments=payments_collection))
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "object": "checkout.session", "id": "cs_unpaid", "mode": "payment",
            "payment_status": "unpaid", "amount_total": 1000, "currency": "usd",
            "metadata": {
                "billing_system": "token_wallet_v1", "billing_payment_id": "pack-123",
                "user_id": "user-123"
            }
        }}
    }
    assert asyncio.run(token_payments.handle_stripe_event(event)) is True
    fake_wallet.grant_tokens.assert_not_awaited()


def test_token_pack_amount_mismatch_never_grants_tokens(monkeypatch):
    payment = {
        "_id": "pack-124", "id": "pack-124", "user_id": "user-124",
        "provider": "stripe", "kind": "token_pack", "amount_cents": 1000, "currency": "usd",
        "token_amount": 20, "token_expires_at": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
        "status": "pending", "catalog_version": 1
    }
    fake_wallet = AsyncMock()
    monkeypatch.setattr(token_payments, "wallet", fake_wallet)
    monkeypatch.setattr(token_payments, "db", SimpleNamespace(billing_payments=MemoryCollection(payment)))
    with pytest.raises(RuntimeError, match="amount or currency"):
        asyncio.run(token_payments._fulfill_token_pack(
            "pack-124", provider="stripe", provider_reference="cs_mismatch",
            amount="9.98", currency="usd"
        ))
    fake_wallet.grant_tokens.assert_not_awaited()


def test_subscription_invoice_with_wrong_amount_is_rejected_before_grant(monkeypatch):
    sub = {
        "_id": "billing-sub-789", "id": "billing-sub-789", "provider": "stripe",
        "provider_subscription_id": "sub_789", "user_id": "user-789", "plan_id": "pro_crew",
        "price_cents": 999, "currency": "usd", "status": "checkout_completed"
    }
    fake_wallet = AsyncMock()
    monkeypatch.setattr(token_payments, "wallet", fake_wallet)
    monkeypatch.setattr(token_payments, "db", SimpleNamespace(billing_subscriptions=MemoryCollection(sub)))
    async def fake_retrieve(*args, **kwargs):
        return {
            "id": "sub_789",
            "metadata": {
                "billing_system": "token_wallet_v1",
                "billing_subscription_id": "billing-sub-789"
            }
        }
    monkeypatch.setattr(token_payments, "_retrieve_stripe_object", fake_retrieve)
    invoice = {
        "id": "in_wrong_amount", "subscription": "sub_789", "status": "paid", "paid": True,
        "billing_reason": "subscription_cycle", "currency": "usd", "amount_paid": 998
    }
    with pytest.raises(RuntimeError, match="amount/currency"):
        asyncio.run(token_payments._process_stripe_invoice(invoice))
    fake_wallet.grant_tokens.assert_not_awaited()
