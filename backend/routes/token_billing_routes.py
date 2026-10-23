"""API endpoints for the token billing foundation."""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo.errors import DuplicateKeyError

from auth import get_current_user
from database import db
from token_billing import (
    BillingCatalog, DEFAULT_BILLING_CATALOG, IdempotencyConflict, InsufficientTokens,
    TokenBillingError, TokenStoreUnavailable, TokenWalletService, utc_iso
)

router = APIRouter()
wallet_service = TokenWalletService(db)
ADMIN_ROLES = {"admin", "super_admin"}


class CatalogUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    catalog: BillingCatalog


class AdminTokenGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int = Field(ge=1, le=10_000)
    reason: str = Field(min_length=5, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=128)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        if value.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return value.astimezone(timezone.utc)


async def _active_catalog() -> dict[str, Any]:
    now = utc_iso()
    default = {
        "_id": "active", "version": 1, "catalog": DEFAULT_BILLING_CATALOG,
        "created_at": now, "updated_at": now, "updated_by": "system",
        "history": [{
            "version": 1, "catalog": DEFAULT_BILLING_CATALOG,
            "updated_at": now, "updated_by": "system"
        }]
    }
    await db.billing_catalog.update_one(
        {"_id": "active"}, {"$setOnInsert": default}, upsert=True
    )
    record = await db.billing_catalog.find_one({"_id": "active"}, {"_id": 0})
    return record or {key: value for key, value in default.items() if key != "_id"}


def _require_admin(user: dict) -> None:
    if user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")


def _translate_wallet_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InsufficientTokens):
        return HTTPException(status_code=402, detail=str(exc))
    if isinstance(exc, IdempotencyConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TokenStoreUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, TokenBillingError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (DuplicateKeyError,)):
        return HTTPException(status_code=409, detail="Token operation conflicts with an existing ledger entry")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="Token operation failed")


@router.get("/catalog")
async def get_billing_catalog():
    """Public read-only catalog for pricing pages; includes its immutable version number."""
    record = await _active_catalog()
    return {
        "version": record["version"], "catalog": record["catalog"],
        "updated_at": record.get("updated_at"), "updated_by": record.get("updated_by")
    }


@router.put("/catalog")
async def update_billing_catalog(
    data: CatalogUpdateRequest,
    current_user: dict = Depends(get_current_user)
):
    """Replace the whole validated catalog with optimistic version control and audit history."""
    _require_admin(current_user)
    record = await _active_catalog()
    if record.get("version") != data.expected_version:
        raise HTTPException(
            status_code=409,
            detail={"message": "Catalog version changed; reload before saving.", "current_version": record.get("version")}
        )

    new_version = data.expected_version + 1
    now = utc_iso()
    catalog = data.catalog.model_dump(mode="json")
    history_entry = {
        "version": new_version, "catalog": catalog,
        "updated_at": now, "updated_by": current_user.get("id", "unknown")
    }
    result = await db.billing_catalog.update_one(
        {"_id": "active", "version": data.expected_version},
        {
            "$set": {
                "version": new_version, "catalog": catalog,
                "updated_at": now, "updated_by": current_user.get("id", "unknown")
            },
            "$push": {"history": {"$each": [history_entry], "$slice": -100}}
        }
    )
    if result.modified_count != 1:
        fresh = await db.billing_catalog.find_one({"_id": "active"}, {"_id": 0, "version": 1})
        raise HTTPException(
            status_code=409,
            detail={"message": "Catalog changed concurrently; reload before saving.",
                    "current_version": (fresh or {}).get("version")}
        )
    return {"version": new_version, "catalog": catalog, "updated_at": now}


@router.get("/catalog/history")
async def get_billing_catalog_history(current_user: dict = Depends(get_current_user)):
    _require_admin(current_user)
    record = await _active_catalog()
    return {"history": record.get("history", [])}


@router.get("/wallet")
async def get_token_wallet(current_user: dict = Depends(get_current_user)):
    """Return only the current user's spendable tokens and expiry lots."""
    try:
        result = await wallet_service.wallet_summary(current_user["id"])
        return {
            "balance": result["balance"],
            "lots": result["lots"],
            "currency": "tokens",
            "points_are_separate": True
        }
    except Exception as exc:
        raise _translate_wallet_error(exc) from None


@router.get("/wallet/ledger")
async def get_token_ledger(
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    try:
        entries = await wallet_service.recent_ledger(current_user["id"], limit)
        return {"entries": entries}
    except Exception as exc:
        raise _translate_wallet_error(exc) from None


@router.post("/admin/wallets/{user_id}/grant")
async def admin_grant_tokens(
    user_id: str,
    data: AdminTokenGrantRequest,
    current_user: dict = Depends(get_current_user)
):
    _require_admin(current_user)
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "id": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        result = await wallet_service.grant_tokens(
            user_id, data.amount, idempotency_key=data.idempotency_key,
            reason=data.reason, source="admin_adjustment", expires_at=data.expires_at,
            metadata={"admin_id": current_user["id"], "admin_role": current_user.get("role", "admin")}
        )
        return {"balance": result["balance"], "entry": result["entry"],
                "already_processed": result["already_processed"]}
    except Exception as exc:
        raise _translate_wallet_error(exc) from None
