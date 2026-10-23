"""Regression tests for token reserve/action/commit and scheduled migration safety."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "token_actions_test")
os.environ.setdefault("JWT_SECRET_KEY", "token-actions-test-secret-at-least-32-characters")
os.environ.setdefault("JWT_EXPIRE_HOURS", "24")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
os.environ.setdefault("EMERGENT_LLM_KEY", "token-actions-placeholder")
os.environ.setdefault("TRUSTED_PROXY_HOPS", "1")

from token_actions import execute_token_action, record_qualifying_activity  # noqa: E402


class FakeWallet:
    def __init__(self, reserve_result=None):
        self.reserve_result = reserve_result or {
            "reservation_id": "reservation-1", "status": "reserved",
            "balance": 7, "already_processed": False, "allocations": []
        }
        self.reserve_calls = []
        self.commit_calls = []
        self.release_calls = []
        self.commit_result = {
            "reservation_id": self.reserve_result.get("reservation_id", "reservation-1"),
            "status": "committed", "balance": 7, "already_processed": False
        }
        self.release_result = {
            "reservation_id": self.reserve_result.get("reservation_id", "reservation-1"),
            "status": "released", "balance": 10, "restored": 3, "already_processed": False
        }
        self.summary = {"balance": 7, "lots": []}

    async def reserve_tokens(self, user_id, amount, **kwargs):
        self.reserve_calls.append((user_id, amount, kwargs))
        if isinstance(self.reserve_result, Exception):
            raise self.reserve_result
        return self.reserve_result

    async def commit_reservation(self, user_id, reservation_id, **kwargs):
        self.commit_calls.append((user_id, reservation_id, kwargs))
        return self.commit_result

    async def release_reservation(self, user_id, reservation_id, **kwargs):
        self.release_calls.append((user_id, reservation_id, kwargs))
        return self.release_result

    async def wallet_summary(self, user_id):
        return self.summary


def run(coro):
    return asyncio.run(coro)


def test_execute_token_action_reserves_action_then_commits(monkeypatch):
    import token_actions
    fake = FakeWallet()
    monkeypatch.setattr(token_actions, "wallet", fake)
    monkeypatch.setattr(token_actions, "_action_persisted", AsyncMock(return_value=False))
    performed = []

    async def business_action(reservation_id):
        performed.append(reservation_id)
        return {"created": "business-record"}

    result = run(execute_token_action(
        {"id": "user-1", "billing_mode": "tokens_v1"},
        amount=3, idempotency_key="request-key-001", reason="Create paid record",
        action=business_action, reference_id="record-1", metadata={"action_type": "test"}
    ))
    assert performed == ["reservation-1"]
    assert len(fake.reserve_calls) == 1
    assert len(fake.commit_calls) == 1
    assert fake.release_calls == []
    assert result["result"] == {"created": "business-record"}
    assert result["balance"] == 7


def test_execute_token_action_releases_reservation_when_business_action_fails(monkeypatch):
    import token_actions
    fake = FakeWallet()
    monkeypatch.setattr(token_actions, "wallet", fake)
    monkeypatch.setattr(token_actions, "_action_persisted", AsyncMock(return_value=False))

    async def failed_action(reservation_id):
        raise HTTPException(status_code=409, detail="Business record no longer available")

    with pytest.raises(HTTPException) as err:
        run(execute_token_action(
            {"id": "user-1", "billing_mode": "tokens_v1"},
            amount=3, idempotency_key="request-key-002", reason="Create paid record",
            action=failed_action, reference_id="record-2", metadata={"action_type": "test"}
        ))
    assert err.value.status_code == 409
    assert len(fake.release_calls) == 1
    assert fake.commit_calls == []


def test_execute_token_action_recovers_persisted_action_without_replaying_it(monkeypatch):
    import token_actions
    fake = FakeWallet({
        "reservation_id": "reservation-recovered", "status": "reserved",
        "balance": 7, "already_processed": True, "allocations": []
    })
    monkeypatch.setattr(token_actions, "wallet", fake)
    monkeypatch.setattr(token_actions, "_action_persisted", AsyncMock(return_value=True))
    action = AsyncMock(return_value={"should_not_run": True})

    result = run(execute_token_action(
        {"id": "user-1", "billing_mode": "tokens_v1"},
        amount=3, idempotency_key="request-key-003", reason="Recover paid record",
        action=action, reference_id="record-3", metadata={"action_type": "test"}
    ))
    action.assert_not_awaited()
    assert len(fake.commit_calls) == 1
    assert result["already_processed"] is True
    assert result["reservation_id"] == "reservation-recovered"


def test_execute_token_action_does_not_replay_an_inflight_reservation(monkeypatch):
    import token_actions
    fake = FakeWallet({
        "reservation_id": "reservation-inflight", "status": "reserved",
        "balance": 7, "already_processed": True, "allocations": []
    })
    monkeypatch.setattr(token_actions, "wallet", fake)
    monkeypatch.setattr(token_actions, "_action_persisted", AsyncMock(return_value=False))
    action = AsyncMock()

    with pytest.raises(HTTPException) as err:
        run(execute_token_action(
            {"id": "user-1", "billing_mode": "tokens_v1"},
            amount=3, idempotency_key="request-key-004", reason="Recover paid record",
            action=action, reference_id="record-4", metadata={"action_type": "test"}
        ))
    assert err.value.status_code == 409
    action.assert_not_awaited()
    assert fake.commit_calls == []


def test_execute_token_action_fails_closed_for_unmigrated_legacy_user(monkeypatch):
    import token_actions
    fake = FakeWallet()
    monkeypatch.setattr(token_actions, "wallet", fake)
    with pytest.raises(HTTPException) as err:
        run(execute_token_action(
            {"id": "legacy-user", "billing_mode": "legacy_compat"},
            amount=3, idempotency_key="request-key-005", reason="Create paid record",
            action=AsyncMock(), reference_id="record-5"
        ))
    assert err.value.status_code == 409
    assert fake.reserve_calls == []


class MemoryEvents:
    def __init__(self):
        self.rows = []

    async def create_index(self, *args, **kwargs):
        return "created"

    async def insert_one(self, record):
        if any(row["user_id"] == record["user_id"] and row["event_id"] == record["event_id"] for row in self.rows):
            raise DuplicateKeyError("duplicate activity event")
        self.rows.append(dict(record))


class MemoryUsers:
    def __init__(self, user):
        self.user = user

    async def find_one(self, query, projection=None):
        return dict(self.user) if self.user and self.user.get("id") == query.get("id") else None


def test_qualifying_activity_is_whitelisted_and_deduplicated(monkeypatch):
    import token_actions
    events = MemoryEvents()
    monkeypatch.setattr(token_actions, "active_catalog", AsyncMock(return_value=(
        1, SimpleNamespace(token_policy=SimpleNamespace(qualifying_activities=[
            "job_posting", "verified_activated_referral", "job_application", "meaningful_profile_update"
        ]))
    )))
    monkeypatch.setattr(token_actions, "db", SimpleNamespace(
        users=MemoryUsers({"id": "user-6", "billing_mode": "tokens_v1"}),
        token_activity_events=events
    ))
    first = run(record_qualifying_activity("user-6", "job_posting", "event-unique-0001"))
    duplicate = run(record_qualifying_activity("user-6", "job_posting", "event-unique-0001"))
    rejected = run(record_qualifying_activity("user-6", "watch_video", "event-unique-0002"))
    assert first is True
    assert duplicate is False
    assert rejected is False
    assert len(events.rows) == 1


def test_qualifying_activity_is_not_recorded_for_legacy_compat_accounts(monkeypatch):
    import token_actions
    events = MemoryEvents()
    monkeypatch.setattr(token_actions, "active_catalog", AsyncMock(return_value=(
        1, SimpleNamespace(token_policy=SimpleNamespace(qualifying_activities=["job_posting"]))
    )))
    monkeypatch.setattr(token_actions, "db", SimpleNamespace(
        users=MemoryUsers({"id": "legacy-user", "billing_mode": "legacy_compat"}),
        token_activity_events=events
    ))
    assert run(record_qualifying_activity("legacy-user", "job_posting", "event-unique-0003")) is False
    assert events.rows == []


def test_legacy_unused_job_credits_convert_at_most_once(monkeypatch):
    import token_maintenance
    cutover = datetime(2026, 10, 23, tzinfo=timezone.utc)
    subscription_end = datetime(2026, 10, 20, tzinfo=timezone.utc)
    period = (subscription_end - timedelta(days=30)).date().isoformat()
    user = {
        "id": "legacy-contractor", "role": "contractor", "subscription_plan": "contractor_business",
        "subscription_end": subscription_end.isoformat(), "job_post_period": period,
        "job_posts_used": 7
    }
    catalog = SimpleNamespace(
        token_costs=SimpleNamespace(contractor_post_job=10),
        token_policy=SimpleNamespace(free_paid_token_expiry_days=60)
    )
    monkeypatch.setattr(token_maintenance, "active_catalog", AsyncMock(return_value=(2, catalog)))
    ledger = SimpleNamespace(find_one=AsyncMock(return_value=None))
    users = SimpleNamespace(update_one=AsyncMock())
    wallet = SimpleNamespace(grant_tokens=AsyncMock(return_value={"already_processed": False}))
    monkeypatch.setattr(token_maintenance, "db", SimpleNamespace(token_ledger=ledger, users=users))
    monkeypatch.setattr(token_maintenance, "wallet", wallet)

    minted = run(token_maintenance._convert_unused_legacy_job_credits(user, cutover_at=cutover))
    assert minted == 130  # 20 plan credits - 7 used = 13 credits × 10 tokens
    kwargs = wallet.grant_tokens.await_args.kwargs
    assert kwargs["idempotency_key"] == f"legacy-post-credit:legacy-contractor:{period}"
    assert kwargs["source"] == "legacy_credit_conversion"
    assert kwargs["metadata"]["legacy_credits_converted"] == 13
    users.update_one.assert_awaited_once()


def test_legacy_unused_credit_conversion_skips_expired_or_stale_period(monkeypatch):
    import token_maintenance
    cutover = datetime(2026, 10, 23, tzinfo=timezone.utc)
    subscription_end = datetime(2026, 10, 20, tzinfo=timezone.utc)
    catalog = SimpleNamespace(
        token_costs=SimpleNamespace(contractor_post_job=10),
        token_policy=SimpleNamespace(free_paid_token_expiry_days=60)
    )
    monkeypatch.setattr(token_maintenance, "active_catalog", AsyncMock(return_value=(1, catalog)))
    wallet = SimpleNamespace(grant_tokens=AsyncMock())
    db = SimpleNamespace(token_ledger=SimpleNamespace(find_one=AsyncMock(return_value=None)),
                         users=SimpleNamespace(update_one=AsyncMock()))
    monkeypatch.setattr(token_maintenance, "wallet", wallet)
    monkeypatch.setattr(token_maintenance, "db", db)
    user = {
        "id": "stale-period", "role": "contractor", "subscription_plan": "contractor_basic",
        "subscription_end": subscription_end.isoformat(), "job_post_period": "2026-01-01",
        "job_posts_used": 0
    }
    assert run(token_maintenance._convert_unused_legacy_job_credits(user, cutover_at=cutover)) == 0
    wallet.grant_tokens.assert_not_awaited()
