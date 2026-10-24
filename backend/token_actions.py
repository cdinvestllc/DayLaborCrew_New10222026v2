"""Shared token feature policy and reserve/action/commit orchestration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
import uuid

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from database import db
from token_billing import (
    BillingCatalog, DEFAULT_BILLING_CATALOG, IdempotencyConflict, InsufficientTokens,
    TokenBillingError, TokenStoreUnavailable, TokenWalletService, utc_iso, utc_now
)

wallet = TokenWalletService(db)
ADMIN_ROLES = {"admin", "super_admin"}


async def active_catalog() -> tuple[int, BillingCatalog]:
    record = await db.billing_catalog.find_one({"_id": "active"}, {"_id": 0})
    if not record:
        now = utc_iso()
        catalog = dict(DEFAULT_BILLING_CATALOG)
        await db.billing_catalog.update_one(
            {"_id": "active"},
            {"$setOnInsert": {
                "version": 1, "catalog": catalog, "created_at": now,
                "updated_at": now, "updated_by": "system",
                "history": [{"version": 1, "catalog": catalog, "updated_at": now, "updated_by": "system"}]
            }},
            upsert=True
        )
        record = await db.billing_catalog.find_one({"_id": "active"}, {"_id": 0})
    if not record:
        return 1, BillingCatalog.model_validate(DEFAULT_BILLING_CATALOG)
    return int(record.get("version", 1)), BillingCatalog.model_validate(record.get("catalog") or DEFAULT_BILLING_CATALOG)


def token_billing_enabled() -> bool:
    """Production rollout gate. Token billing is disabled unless explicitly enabled."""
    import os
    return os.environ.get("TOKEN_BILLING_ENABLED", "").strip().lower() == "true"


def uses_token_billing(user: dict | None) -> bool:
    if not token_billing_enabled() or not user:
        return False
    if user.get("billing_mode") == "tokens_v1":
        return True
    # Accounts upgraded through the new, verified billing system are always token-backed.
    return bool(user.get("billing_subscription_id") and user.get("billing_subscription_status") == "active")


def token_trial_active(user: dict | None, now: datetime | None = None) -> bool:
    user = user or {}
    now = now or utc_now()
    value = user.get("token_trial_end_at")
    if not value or not user.get("token_trial_started_at"):
        return False
    try:
        end = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end > now and user.get("billing_subscription_status") in {"trial", "pending", "free", None}
    except (TypeError, ValueError):
        return False


def pro_entitled(user: dict | None, *, role: str | None = None, now: datetime | None = None) -> bool:
    """Pro features require the appropriate paid token plan or its unexpired opt-in trial."""
    user = user or {}
    if role and user.get("role") != role:
        return False
    if user.get("role") in ADMIN_ROLES:
        return True
    if not uses_token_billing(user):
        return False
    plan_id = user.get("billing_plan") or user.get("token_plan")
    if user.get("billing_subscription_status") == "active" and plan_id in {"pro_crew", "pro_contractor"}:
        return True
    return token_trial_active(user, now)


def cost_from_catalog(catalog: BillingCatalog, key: str) -> int:
    return int(getattr(catalog.token_costs, key))


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _require_action_id(key: str) -> str:
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key (8-128 characters) is required.")
    return key


async def _action_persisted(user_id: str, reservation_id: str, reference_id: str | None,
                          metadata: dict[str, Any] | None) -> bool:
    """Check durable business evidence before recovering a reserved action after a crash."""
    action_type = (metadata or {}).get("action_type")
    if action_type == "job_posting":
        found = await db.jobs.find_one({"id": reference_id, "token_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "job_application":
        found = await db.jobs.find_one(
            {"id": reference_id,
             "crew_acceptance_receipts": {"$elemMatch": {"crew_id": user_id, "reservation_id": reservation_id}}},
            {"_id": 1}
        )
    elif action_type == "job_boost":
        found = await db.jobs.find_one({"id": reference_id, "token_boost_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "profile_boost":
        found = await db.users.find_one({"id": user_id, "profile_boost_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "verification_renewal":
        found = await db.users.find_one({"id": user_id, "verification_token_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "portfolio_video":
        found = await db.users.find_one(
            {"id": user_id, "portfolio_videos": {"$elemMatch": {"reservation_id": reservation_id}}},
            {"_id": 1}
        )
    else:
        return False
    return found is not None


async def execute_token_action(
    user: dict, *, amount: int, idempotency_key: str, reason: str, action: Callable[[str], Awaitable[Any]],
    metadata: dict[str, Any] | None = None, reference_id: str | None = None
) -> dict[str, Any]:
    """Reserve -> perform business action -> commit. Releases only when the action fails.

    If committing fails after the business write succeeds, the reservation is intentionally
    left in place for reconciliation; it is never released on the assumption that the action
    did not happen.
    """
    if not uses_token_billing(user):
        raise HTTPException(status_code=409, detail="This account is still on legacy billing; token checkout is unavailable for this action.")
    idempotency_key = _require_action_id(idempotency_key)
    try:
        reserved = await wallet.reserve_tokens(
            user["id"], amount, idempotency_key=idempotency_key, reason=reason,
            metadata=metadata or {}, reference_id=reference_id
        )
        if reserved["status"] == "committed":
            return {"already_processed": True, "balance": reserved["balance"], "result": None,
                    "reservation_id": reserved.get("reservation_id")}
        if reserved.get("already_processed") and reserved["status"] == "reserved":
            persisted = await _action_persisted(user["id"], reserved["reservation_id"], reference_id, metadata)
            if persisted:
                committed = await wallet.commit_reservation(
                    user["id"], reserved["reservation_id"],
                    metadata={"action_succeeded": True, "reconciled": True, "action": metadata or {}}
                )
                return {"already_processed": True, "balance": committed["balance"], "result": None,
                        "reservation_id": reserved["reservation_id"]}
            raise HTTPException(status_code=409, detail="This token action is already processing; retry with the same Idempotency-Key.")
        try:
            result = await action(reserved["reservation_id"])
        except Exception:
            # A database write can succeed even when its client loses the acknowledgment.
            # Check for the durable business marker before releasing the reservation.
            try:
                persisted = await _action_persisted(user["id"], reserved["reservation_id"], reference_id, metadata)
            except Exception:
                # Status cannot be proven, so hold the reservation for maintenance instead
                # of risking a free action by refunding tokens after an ambiguous write.
                raise HTTPException(
                    status_code=503,
                    detail="Action status could not be confirmed. The token reservation is held for reconciliation; retry with the same Idempotency-Key."
                ) from None
            if persisted:
                try:
                    committed = await wallet.commit_reservation(
                        user["id"], reserved["reservation_id"],
                        metadata={"action_succeeded": True, "reconciled_after_action_error": True,
                                  "action": metadata or {}}
                    )
                except Exception:
                    raise HTTPException(
                        status_code=503,
                        detail="The action was saved, but its token charge is still being reconciled. Retry with the same Idempotency-Key."
                    ) from None
                return {"already_processed": True, "balance": committed["balance"], "result": None,
                        "reservation_id": reserved["reservation_id"]}
            # No durable business record exists, so it is safe to restore the reservation.
            try:
                await wallet.release_reservation(
                    user["id"], reserved["reservation_id"], reason=f"{reason}: action failed",
                    metadata={"action_failed": True}
                )
            except Exception:
                # Keep the original action error. A later reservation-recovery job can retry.
                pass
            raise
        committed = await wallet.commit_reservation(
            user["id"], reserved["reservation_id"],
            metadata={"action_succeeded": True, "action": metadata or {}}
        )
        return {
            "already_processed": committed.get("already_processed", False),
            "balance": committed["balance"], "result": result,
            "reservation_id": reserved["reservation_id"]
        }
    except InsufficientTokens as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from None
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except TokenStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except TokenBillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


async def record_qualifying_activity(
    user_id: str, activity: str, event_id: str, *, metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None
) -> bool:
    """Record a successful qualifying activity exactly once; points/reputation remain separate."""
    if not user_id or not isinstance(event_id, str) or not 8 <= len(event_id) <= 200:
        return False
    _, catalog = await active_catalog()
    allowed = set(catalog.token_policy.qualifying_activities)
    if activity not in allowed:
        return False
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "id": 1, "billing_mode": 1})
    if not token_billing_enabled() or not user or user.get("billing_mode") != "tokens_v1":
        return False
    try:
        await db.token_activity_events.create_index(
            [("user_id", 1), ("event_id", 1)], unique=True, name="uniq_token_activity_event"
        )
        await db.token_activity_events.create_index([("user_id", 1), ("created_at", -1)])
        await db.token_activity_events.insert_one({
            "id": str(uuid.uuid4()), "user_id": user_id, "event_id": event_id,
            "activity": activity, "metadata": metadata or {},
            "created_at": utc_iso(created_at or utc_now())
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception:
        # Do not fail a completed job/profile update because the activity reward tracker
        # is unavailable; scheduler retries/backfill recover from durable business records.
        return False


async def resolve_feature_entitlement(user: dict, *, role: str | None = None, pro: bool = False) -> BillingCatalog:
    if user.get("role") in ADMIN_ROLES:
        _, catalog = await active_catalog()
        return catalog
    if not uses_token_billing(user):
        raise HTTPException(status_code=409, detail="This account must finish legacy billing migration before using token features.")
    if role and user.get("role") != role:
        raise HTTPException(status_code=403, detail="This feature is not available for your account type.")
    if pro and not pro_entitled(user, role=role):
        raise HTTPException(status_code=403, detail="This feature requires an active Pro membership or an unexpired Pro trial.")
    _, catalog = await active_catalog()
    return catalog
