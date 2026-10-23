"""Versioned billing catalog and transaction-safe token wallet primitives.

Points remain a separate reputation/reward counter. Spendable tokens are represented by
expiring lots plus an append-only ledger. All balance-changing operations run in a MongoDB
transaction; the deployment must use a transaction-capable replica set (Atlas qualifies).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo.errors import DuplicateKeyError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


DEFAULT_BILLING_CATALOG: dict[str, Any] = {
    "currency": "usd",
    "trial_days": 14,
    "annual_discount_percent": 33,
    "plans": {
        "free_crew": {
            "role": "crew", "monthly_price_cents": 0, "annual_price_cents": 0,
            "monthly_tokens": 10, "enabled": True, "features": ["browse_jobs", "apply_with_tokens"]
        },
        "free_contractor": {
            "role": "contractor", "monthly_price_cents": 0, "annual_price_cents": 0,
            "monthly_tokens": 10, "enabled": True, "features": ["manage_profile", "post_jobs_with_tokens"]
        },
        "pro_crew": {
            "role": "crew", "monthly_price_cents": 999, "annual_price_cents": 8032,
            "monthly_tokens": 20, "enabled": True,
            "features": ["featured_profile", "profile_boost", "video_portfolio"]
        },
        "pro_contractor": {
            "role": "contractor", "monthly_price_cents": 1999, "annual_price_cents": 16072,
            "monthly_tokens": 40, "enabled": True,
            "features": ["featured_jobs", "job_boost", "offer_jobs_to_pro_crew", "video_portfolio"]
        },
        "enterprise": {
            "role": "enterprise", "monthly_price_cents": 0, "annual_price_cents": 0,
            "monthly_tokens": 0, "enabled": False, "features": []
        }
    },
    "token_costs": {
        "crew_apply_job": 5,
        "contractor_post_job": 10,
        "emergency_job_surcharge": 10,
        "crew_profile_boost": 10,
        "contractor_job_boost": 10,
        "video_30_seconds_30_days": 2,
        "verified_contractor_renewal": 100,
        "pro_verified_contractor_renewal": 80
    },
    "token_pack": {"tokens": 20, "price_cents": 1000, "enabled": True},
    "token_policy": {
        "free_grant_expiry_days": 30,
        "pro_grant_expiry_days": 60,
        "free_paid_token_expiry_days": 60,
        "pro_paid_tokens_expire_at": "billing_cycle_end",
        "trial_tokens_expire_at": "trial_end",
        "trial_crew_tokens": 10,
        "trial_contractor_tokens": 20,
        "active_free_tasks_required": 2,
        "active_free_period_days": 30,
        "qualifying_activities": [
            "job_posting", "verified_activated_referral", "job_application", "meaningful_profile_update"
        ],
        "tokens_transferable": False,
        "tokens_cash_redeemable": False,
        "tokens_refundable": False
    },
    "guest_project_manager": {
        "enabled": True, "payment_amount_cents": 1000, "token_equivalent": 20,
        "anonymous_wallet": False, "job_post_requires_images": True
    },
    "payment_providers": {
        "stripe": True, "paypal": True, "square": False,
        "cash_app_pay": False, "crypto": False
    }
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanConfig(StrictModel):
    role: Literal["crew", "contractor", "enterprise"]
    monthly_price_cents: int = Field(ge=0, le=100_000_000)
    annual_price_cents: int = Field(ge=0, le=1_000_000_000)
    monthly_tokens: int = Field(ge=0, le=1_000_000)
    enabled: bool
    features: list[str] = Field(default_factory=list, max_length=100)


class TokenCosts(StrictModel):
    crew_apply_job: int = Field(ge=0, le=1_000_000)
    contractor_post_job: int = Field(ge=0, le=1_000_000)
    emergency_job_surcharge: int = Field(ge=0, le=1_000_000)
    crew_profile_boost: int = Field(ge=0, le=1_000_000)
    contractor_job_boost: int = Field(ge=0, le=1_000_000)
    video_30_seconds_30_days: int = Field(ge=0, le=1_000_000)
    verified_contractor_renewal: int = Field(ge=0, le=1_000_000)
    pro_verified_contractor_renewal: int = Field(ge=0, le=1_000_000)


class TokenPackConfig(StrictModel):
    tokens: int = Field(ge=1, le=1_000_000)
    price_cents: int = Field(ge=1, le=100_000_000)
    enabled: bool


class TokenPolicyConfig(StrictModel):
    free_grant_expiry_days: int = Field(ge=1, le=3650)
    pro_grant_expiry_days: int = Field(ge=1, le=3650)
    free_paid_token_expiry_days: int = Field(ge=1, le=3650)
    pro_paid_tokens_expire_at: Literal["billing_cycle_end", "purchase_plus_days"]
    trial_tokens_expire_at: Literal["trial_end"]
    trial_crew_tokens: int = Field(ge=0, le=1_000_000)
    trial_contractor_tokens: int = Field(ge=0, le=1_000_000)
    active_free_tasks_required: int = Field(ge=0, le=1000)
    active_free_period_days: int = Field(ge=1, le=3650)
    qualifying_activities: list[str] = Field(min_length=1, max_length=30)
    tokens_transferable: Literal[False] = False
    tokens_cash_redeemable: Literal[False] = False
    tokens_refundable: Literal[False] = False


class GuestProjectManagerConfig(StrictModel):
    enabled: bool
    payment_amount_cents: int = Field(ge=0, le=100_000_000)
    token_equivalent: int = Field(ge=1, le=1_000_000)
    anonymous_wallet: Literal[False] = False
    job_post_requires_images: bool


class BillingCatalog(StrictModel):
    currency: str = Field(pattern=r"^[a-z]{3}$")
    trial_days: int = Field(ge=0, le=365)
    annual_discount_percent: int = Field(ge=0, le=80)
    plans: dict[str, PlanConfig]
    token_costs: TokenCosts
    token_pack: TokenPackConfig
    token_policy: TokenPolicyConfig
    guest_project_manager: GuestProjectManagerConfig
    payment_providers: dict[str, bool]

    @model_validator(mode="after")
    def validate_catalog_consistency(self) -> "BillingCatalog":
        required = {"free_crew", "free_contractor", "pro_crew", "pro_contractor", "enterprise"}
        if set(self.plans) != required:
            raise ValueError(f"plans must contain exactly: {', '.join(sorted(required))}")
        for plan_id, role in (
            ("free_crew", "crew"), ("pro_crew", "crew"),
            ("free_contractor", "contractor"), ("pro_contractor", "contractor"),
            ("enterprise", "enterprise")
        ):
            if self.plans[plan_id].role != role:
                raise ValueError(f"{plan_id} must use role {role}")
        if self.plans["free_crew"].monthly_price_cents or self.plans["free_crew"].annual_price_cents:
            raise ValueError("Free Crew must remain free")
        if self.plans["free_contractor"].monthly_price_cents or self.plans["free_contractor"].annual_price_cents:
            raise ValueError("Free Contractor must remain free")
        required_providers = {"stripe", "paypal", "square", "cash_app_pay", "crypto"}
        if not required_providers.issubset(self.payment_providers):
            raise ValueError("payment_providers must specify Stripe, PayPal, Square, Cash App Pay, and crypto")
        return self


class TokenBillingError(Exception):
    """Base class for safe-to-display token billing errors."""


class InsufficientTokens(TokenBillingError):
    pass


class IdempotencyConflict(TokenBillingError):
    pass


class TokenStoreUnavailable(TokenBillingError):
    pass


def allocate_lots(lots: list[dict[str, Any]], amount: int) -> list[dict[str, Any]]:
    """Allocate a spend from earliest-expiring lots first (lots should be unexpired)."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("Token amount must be a positive integer")
    ordered = sorted(
        lots,
        key=lambda lot: (
            lot.get("expires_at") or "9999-12-31T23:59:59+00:00",
            lot.get("created_at") or "",
            lot.get("lot_id") or str(lot.get("_id", ""))
        )
    )
    remaining = amount
    allocations: list[dict[str, Any]] = []
    for lot in ordered:
        available = max(0, int(lot.get("remaining", 0)))
        if not available:
            continue
        used = min(available, remaining)
        if used:
            allocations.append({
                "lot_id": lot.get("lot_id") or str(lot.get("_id", "")),
                "amount": used,
                "expires_at": lot.get("expires_at")
            })
            remaining -= used
        if remaining == 0:
            break
    if remaining:
        raise InsufficientTokens("Not enough unexpired tokens")
    return allocations


class TokenWalletService:
    """Append-only token ledger and expiring lot wallet backed by MongoDB transactions."""

    def __init__(self, database):
        self.db = database
        self._indexes_ready = False

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        await self.db.token_wallets.create_index("user_id", unique=True)
        await self.db.token_lots.create_index([("user_id", 1), ("expires_at", 1), ("remaining", 1)])
        await self.db.token_ledger.create_index([("user_id", 1), ("idempotency_key", 1)], unique=True)
        await self.db.token_ledger.create_index([("user_id", 1), ("created_at", -1)])
        self._indexes_ready = True

    async def _transaction(self, callback):
        try:
            async with await self.db.client.start_session() as session:
                return await session.with_transaction(callback)
        except (TokenBillingError, DuplicateKeyError):
            raise
        except Exception:
            # Keep provider/database internals out of API responses. Most importantly, don't
            # silently fall back to non-atomic balance changes on a standalone Mongo server.
            raise TokenStoreUnavailable(
                "Token transactions are unavailable; configure MongoDB as a transaction-capable replica set."
            ) from None

    def _validate_operation(self, user_id: str, amount: int, idempotency_key: str, reason: str) -> None:
        if not user_id or len(user_id) > 256:
            raise ValueError("Invalid user id")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > 1_000_000:
            raise ValueError("Token amount must be a positive integer")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
            raise ValueError("Idempotency key must contain 8 to 128 characters")
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 300:
            raise ValueError("A reason between 3 and 300 characters is required")

    async def _expire_in_session(self, session, user_id: str, now: datetime) -> int:
        now_text = utc_iso(now)
        cursor = self.db.token_lots.find(
            {"user_id": user_id, "remaining": {"$gt": 0}, "expires_at": {"$lte": now_text}},
            session=session
        )
        expired_lots = await cursor.to_list(length=None)
        total_expired = 0
        for lot in expired_lots:
            remaining = int(lot.get("remaining", 0))
            if remaining <= 0:
                continue
            result = await self.db.token_lots.update_one(
                {"_id": lot["_id"], "remaining": remaining},
                {"$set": {"remaining": 0, "expired_at": now_text}},
                session=session
            )
            if result.modified_count != 1:
                raise TokenStoreUnavailable("Token lots changed while expiring; retry the operation.")
            lot_id = lot.get("lot_id") or str(lot["_id"])
            await self.db.token_ledger.insert_one({
                "entry_id": str(uuid.uuid4()), "user_id": user_id,
                "idempotency_key": f"expire:{lot_id}", "operation": "expire",
                "delta": -remaining, "amount": remaining, "reason": "Token lot expired",
                "source": "expiration", "source_lot_id": lot_id, "metadata": {},
                "created_at": now_text
            }, session=session)
            total_expired += remaining
        if total_expired:
            wallet = await self.db.token_wallets.find_one({"_id": user_id}, session=session)
            if not wallet or int(wallet.get("balance", 0)) < total_expired:
                raise TokenStoreUnavailable("Token wallet balance does not match its lots.")
            result = await self.db.token_wallets.update_one(
                {"_id": user_id, "balance": {"$gte": total_expired}},
                {"$inc": {"balance": -total_expired}, "$set": {"updated_at": now_text}},
                session=session
            )
            if result.modified_count != 1:
                raise TokenStoreUnavailable("Token wallet changed while expiring; retry the operation.")
        return total_expired

    def _assert_same_operation(
        self, entry: dict[str, Any], operation: str, delta: int, reason: str,
        source: str, expires_at: str | None, metadata: dict[str, Any], reference_id: str | None
    ) -> None:
        if (
            entry.get("operation") != operation or entry.get("delta") != delta
            or entry.get("reason") != reason or entry.get("source") != source
            or entry.get("expires_at") != expires_at or entry.get("metadata", {}) != metadata
            or entry.get("reference_id") != reference_id
        ):
            raise IdempotencyConflict("This idempotency key was already used for a different token operation.")

    async def _existing_result(
        self, user_id: str, idempotency_key: str, operation: str, delta: int,
        reason: str, source: str, expires_at: str | None, metadata: dict[str, Any],
        reference_id: str | None
    ) -> dict[str, Any] | None:
        entry = await self.db.token_ledger.find_one(
            {"user_id": user_id, "idempotency_key": idempotency_key}, {"_id": 0}
        )
        if not entry:
            return None
        self._assert_same_operation(entry, operation, delta, reason, source, expires_at, metadata, reference_id)
        summary = await self.wallet_summary(user_id)
        return {"entry": entry, "balance": summary["balance"], "already_processed": True}

    async def grant_tokens(
        self, user_id: str, amount: int, *, idempotency_key: str, reason: str,
        source: str, expires_at: datetime, metadata: dict[str, Any] | None = None,
        reference_id: str | None = None
    ) -> dict[str, Any]:
        self._validate_operation(user_id, amount, idempotency_key, reason)
        if not isinstance(source, str) or not 1 <= len(source) <= 60:
            raise ValueError("Invalid token source")
        expiry_text = utc_iso(expires_at)
        if expires_at.astimezone(timezone.utc) <= utc_now():
            raise ValueError("Token expiry must be in the future")
        metadata = metadata or {}
        await self.ensure_indexes()
        existing = await self._existing_result(
            user_id, idempotency_key, "grant", amount, reason, source, expiry_text, metadata, reference_id
        )
        if existing:
            return existing
        now_text = utc_iso()
        lot_id = str(uuid.uuid4())
        entry_id = str(uuid.uuid4())

        async def operation(session):
            existing_inside = await self.db.token_ledger.find_one(
                {"user_id": user_id, "idempotency_key": idempotency_key}, session=session
            )
            if existing_inside:
                self._assert_same_operation(
                    existing_inside, "grant", amount, reason, source, expiry_text, metadata, reference_id
                )
                wallet = await self.db.token_wallets.find_one({"_id": user_id}, session=session) or {}
                return {"entry": existing_inside, "balance": int(wallet.get("balance", 0)), "already_processed": True}
            now = utc_now()
            await self._expire_in_session(session, user_id, now)
            lot = {
                "_id": lot_id, "lot_id": lot_id, "user_id": user_id,
                "granted": amount, "remaining": amount, "source": source,
                "created_at": now_text, "expires_at": expiry_text, "reference_id": reference_id,
                "metadata": metadata
            }
            await self.db.token_lots.insert_one(lot, session=session)
            await self.db.token_wallets.update_one(
                {"_id": user_id},
                {"$inc": {"balance": amount}, "$set": {"updated_at": now_text},
                 "$setOnInsert": {"user_id": user_id, "created_at": now_text}},
                upsert=True, session=session
            )
            entry = {
                "entry_id": entry_id, "user_id": user_id, "idempotency_key": idempotency_key,
                "operation": "grant", "delta": amount, "amount": amount, "reason": reason,
                "source": source, "expires_at": expiry_text, "reference_id": reference_id,
                "metadata": metadata, "lot_id": lot_id, "created_at": now_text
            }
            await self.db.token_ledger.insert_one(entry, session=session)
            wallet = await self.db.token_wallets.find_one({"_id": user_id}, session=session) or {}
            return {"entry": entry, "balance": int(wallet.get("balance", amount)), "already_processed": False}

        try:
            return await self._transaction(operation)
        except DuplicateKeyError:
            result = await self._existing_result(
                user_id, idempotency_key, "grant", amount, reason, source, expiry_text, metadata, reference_id
            )
            if result:
                return result
            raise IdempotencyConflict("Token operation conflicted with another write; retry with a fresh key.") from None

    async def spend_tokens(
        self, user_id: str, amount: int, *, idempotency_key: str, reason: str,
        metadata: dict[str, Any] | None = None, reference_id: str | None = None
    ) -> dict[str, Any]:
        self._validate_operation(user_id, amount, idempotency_key, reason)
        metadata = metadata or {}
        await self.ensure_indexes()
        existing = await self._existing_result(
            user_id, idempotency_key, "spend", -amount, reason, "spend", None, metadata, reference_id
        )
        if existing:
            return existing
        now_text = utc_iso()
        entry_id = str(uuid.uuid4())

        async def operation(session):
            existing_inside = await self.db.token_ledger.find_one(
                {"user_id": user_id, "idempotency_key": idempotency_key}, session=session
            )
            if existing_inside:
                self._assert_same_operation(
                    existing_inside, "spend", -amount, reason, "spend", None, metadata, reference_id
                )
                wallet = await self.db.token_wallets.find_one({"_id": user_id}, session=session) or {}
                return {"entry": existing_inside, "balance": int(wallet.get("balance", 0)), "already_processed": True}
            now = utc_now()
            await self._expire_in_session(session, user_id, now)
            wallet = await self.db.token_wallets.find_one({"_id": user_id}, session=session)
            balance = int((wallet or {}).get("balance", 0))
            if balance < amount:
                raise InsufficientTokens("Not enough unexpired tokens")
            cursor = self.db.token_lots.find(
                {"user_id": user_id, "remaining": {"$gt": 0}, "expires_at": {"$gt": utc_iso(now)}},
                session=session
            ).sort([("expires_at", 1), ("created_at", 1), ("lot_id", 1)])
            lots = await cursor.to_list(length=None)
            allocations = allocate_lots(lots, amount)
            for allocation in allocations:
                lot_id = allocation["lot_id"]
                lot = next(item for item in lots if (item.get("lot_id") or str(item["_id"])) == lot_id)
                previous = int(lot["remaining"])
                result = await self.db.token_lots.update_one(
                    {"_id": lot["_id"], "remaining": previous},
                    {"$inc": {"remaining": -allocation["amount"]}, "$set": {"updated_at": now_text}},
                    session=session
                )
                if result.modified_count != 1:
                    raise TokenStoreUnavailable("Token lots changed while spending; retry the operation.")
            result = await self.db.token_wallets.update_one(
                {"_id": user_id, "balance": {"$gte": amount}},
                {"$inc": {"balance": -amount}, "$set": {"updated_at": now_text}},
                session=session
            )
            if result.modified_count != 1:
                raise TokenStoreUnavailable("Token wallet changed while spending; retry the operation.")
            entry = {
                "entry_id": entry_id, "user_id": user_id, "idempotency_key": idempotency_key,
                "operation": "spend", "delta": -amount, "amount": amount, "reason": reason,
                "source": "spend", "expires_at": None, "reference_id": reference_id,
                "metadata": metadata, "allocations": allocations, "created_at": now_text
            }
            await self.db.token_ledger.insert_one(entry, session=session)
            return {"entry": entry, "balance": balance - amount, "already_processed": False}

        try:
            return await self._transaction(operation)
        except DuplicateKeyError:
            result = await self._existing_result(
                user_id, idempotency_key, "spend", -amount, reason, "spend", None, metadata, reference_id
            )
            if result:
                return result
            raise IdempotencyConflict("Token operation conflicted with another write; retry with a fresh key.") from None

    async def wallet_summary(self, user_id: str) -> dict[str, Any]:
        await self.ensure_indexes()

        async def operation(session):
            now = utc_now()
            await self._expire_in_session(session, user_id, now)
            wallet = await self.db.token_wallets.find_one({"_id": user_id}, session=session)
            cursor = self.db.token_lots.find(
                {"user_id": user_id, "remaining": {"$gt": 0}, "expires_at": {"$gt": utc_iso(now)}},
                {"_id": 0, "lot_id": 1, "remaining": 1, "source": 1, "created_at": 1, "expires_at": 1},
                session=session
            ).sort([("expires_at", 1), ("created_at", 1)]).limit(100)
            lots = await cursor.to_list(length=100)
            return {"balance": int((wallet or {}).get("balance", 0)), "lots": lots}

        return await self._transaction(operation)

    async def recent_ledger(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        await self.ensure_indexes()
        cursor = self.db.token_ledger.find({"user_id": user_id}, {"_id": 0}).sort(
            [("created_at", -1), ("entry_id", -1)]
        ).limit(max(1, min(int(limit), 100)))
        return await cursor.to_list(length=max(1, min(int(limit), 100)))
