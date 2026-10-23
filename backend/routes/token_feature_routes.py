"""Server-side token-gated feature actions: boosts, video portfolio, and verification."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from auth import get_current_user
from database import db
from token_actions import (
    active_catalog, execute_token_action, pro_entitled, record_qualifying_activity,
    resolve_feature_entitlement, uses_token_billing, wallet
)
from token_billing import utc_iso, utc_now
from utils.storage import MAX_VIDEO_BYTES, detect_video, save_video

router = APIRouter()
BOOST_DURATION_DAYS = 7
VIDEO_DURATION_SECONDS = 30
MAX_PROFILE_VIDEOS = 5


def _request_key(supplied: str | None, fallback: str) -> str:
    value = (supplied or fallback).strip()
    if not 8 <= len(value) <= 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key must contain 8 to 128 characters.")
    return value


def _verify_video_duration(data: bytes) -> float:
    """Use ffprobe on server-side file bytes; never trust a client-supplied duration."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise HTTPException(status_code=503, detail="Server video validation is not configured; install ffprobe.")
    suffix = ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        suffix = ".webm"
    try:
        with tempfile.NamedTemporaryFile(prefix="dlc-video-", suffix=suffix) as temp:
            temp.write(data)
            temp.flush()
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", temp.name],
                capture_output=True, text=True, timeout=10, check=False
            )
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(status_code=400, detail="Video could not be validated.") from None
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="Video file is invalid or unreadable.")
    try:
        duration = float(proc.stdout.strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Video duration could not be read.") from None
    if duration <= 0 or duration > VIDEO_DURATION_SECONDS:
        raise HTTPException(status_code=400, detail="Portfolio videos must be 30 seconds or shorter.")
    return duration


@router.post("/jobs/{job_id}/boost")
async def token_boost_job(
    job_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user)
):
    catalog = await resolve_feature_entitlement(current_user, role="contractor", pro=True)
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("contractor_id") != current_user["id"] and current_user.get("role") not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not your job")
    expiry = job.get("boost_expires_at")
    try:
        expiry_dt = __import__("datetime").datetime.fromisoformat(expiry.replace("Z", "+00:00")) if isinstance(expiry, str) else None
        if expiry_dt and expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=utc_now().tzinfo)
    except (TypeError, ValueError):
        expiry_dt = None
    if job.get("is_featured") and expiry_dt and expiry_dt > utc_now():
        return {"status": "already_featured", "boost_expires_at": expiry, "balance": (await wallet.wallet_summary(current_user["id"]))["balance"]}
    cost = int(catalog.token_costs.contractor_job_boost)
    key = _request_key(idempotency_key, f"jobboost:{job_id}:{expiry or 'initial'}")

    async def action(reservation_id: str):
        now = utc_now()
        boost_until = utc_iso(now + timedelta(days=BOOST_DURATION_DAYS))
        result = await db.jobs.update_one(
            {"id": job_id, "contractor_id": current_user["id"],
             "$or": [{"is_featured": {"$ne": True}}, {"boost_expires_at": {"$lte": utc_iso(now)}}]},
            {"$set": {"is_featured": True, "boost_expires_at": boost_until,
                      "token_boost_reservation_id": reservation_id, "boost_payment_type": "tokens"}}
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=409, detail="Job boost state changed; refresh and try again.")
        return {"job_id": job_id, "boost_expires_at": boost_until}

    result = await execute_token_action(
        current_user, amount=cost, idempotency_key=key, reason="Boost contractor job",
        action=action, reference_id=job_id,
        metadata={"action_type": "job_boost", "job_id": job_id, "feature": "contractor_job_boost"}
    )
    return {"status": "boosted", "tokens_charged": cost, "balance": result["balance"],
            "already_processed": result["already_processed"], **(result["result"] or {})}


@router.post("/profile/boost")
async def token_boost_profile(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user)
):
    catalog = await resolve_feature_entitlement(current_user, role="crew", pro=True)
    expiry = current_user.get("profile_boost_expires_at")
    old_dt = None
    try:
        from datetime import datetime
        old_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00")) if isinstance(expiry, str) else None
        if old_dt and old_dt.tzinfo is None:
            old_dt = old_dt.replace(tzinfo=utc_now().tzinfo)
    except (TypeError, ValueError):
        old_dt = None
    if old_dt and old_dt > utc_now():
        return {"status": "already_featured", "profile_boost_expires_at": expiry,
                "balance": (await wallet.wallet_summary(current_user["id"]))["balance"]}
    cost = int(catalog.token_costs.crew_profile_boost)
    key = _request_key(idempotency_key, f"profileboost:{current_user['id']}:{expiry or 'initial'}")

    async def action(reservation_id: str):
        until = utc_iso(utc_now() + timedelta(days=BOOST_DURATION_DAYS))
        result = await db.users.update_one(
            {"id": current_user["id"],
             "$or": [{"profile_boost_expires_at": {"$exists": False}},
                     {"profile_boost_expires_at": {"$lte": utc_iso()}}]},
            {"$set": {"profile_featured": True, "profile_boost_expires_at": until,
                      "profile_boost_reservation_id": reservation_id}}
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=409, detail="Profile boost state changed; refresh and try again.")
        return {"profile_boost_expires_at": until}

    result = await execute_token_action(
        current_user, amount=cost, idempotency_key=key, reason="Boost crew profile",
        action=action, reference_id=current_user["id"],
        metadata={"action_type": "profile_boost", "feature": "crew_profile_boost"}
    )
    return {"status": "boosted", "tokens_charged": cost, "balance": result["balance"],
            "already_processed": result["already_processed"], **(result["result"] or {})}


@router.post("/verification/renew")
async def token_verified_contractor_renewal(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user)
):
    catalog = await resolve_feature_entitlement(current_user, role="contractor")
    expiry = current_user.get("verified_expires_at")
    from datetime import datetime
    try:
        old_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00")) if isinstance(expiry, str) else None
        if old_dt and old_dt.tzinfo is None:
            old_dt = old_dt.replace(tzinfo=utc_now().tzinfo)
    except (TypeError, ValueError):
        old_dt = None
    if current_user.get("is_verified_contractor") and old_dt and old_dt - utc_now() > timedelta(days=30):
        raise HTTPException(status_code=400, detail="Verification renewal opens 30 days before expiry.")
    pro = pro_entitled(current_user, role="contractor")
    cost = int(catalog.token_costs.pro_verified_contractor_renewal if pro else catalog.token_costs.verified_contractor_renewal)
    key = _request_key(idempotency_key, f"verification:{current_user['id']}:{expiry or 'initial'}")

    async def action(reservation_id: str):
        now = utc_now()
        latest = await db.users.find_one({"id": current_user["id"]}, {"_id": 0, "verified_expires_at": 1})
        latest_expiry = latest.get("verified_expires_at") if latest else None
        try:
            latest_dt = datetime.fromisoformat(latest_expiry.replace("Z", "+00:00")) if isinstance(latest_expiry, str) else None
            if latest_dt and latest_dt.tzinfo is None:
                latest_dt = latest_dt.replace(tzinfo=now.tzinfo)
        except (TypeError, ValueError):
            latest_dt = None
        if current_user.get("is_verified_contractor") and latest_dt and latest_dt - now > timedelta(days=30):
            raise HTTPException(status_code=409, detail="Verification was renewed by another request.")
        new_expiry = (max(latest_dt, now) if latest_dt else now) + timedelta(days=365)
        result = await db.users.update_one(
            {"id": current_user["id"]},
            {"$set": {"is_verified_contractor": True, "verified_at": utc_iso(now),
                      "verified_expires_at": utc_iso(new_expiry), "verification_expired": False,
                      "verification_token_reservation_id": reservation_id}}
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=409, detail="Verification renewal could not be saved.")
        return {"verified_expires_at": utc_iso(new_expiry)}

    result = await execute_token_action(
        current_user, amount=cost, idempotency_key=key, reason="Renew verified contractor status",
        action=action, reference_id=current_user["id"],
        metadata={"action_type": "verification_renewal", "feature": "verified_contractor_renewal"}
    )
    return {"status": "verified", "tokens_charged": cost, "pro_discount_applied": pro,
            "balance": result["balance"], "already_processed": result["already_processed"],
            **(result["result"] or {})}


@router.post("/portfolio/video")
async def upload_portfolio_video(
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user)
):
    catalog = await resolve_feature_entitlement(current_user, pro=True)
    if not uses_token_billing(current_user):
        raise HTTPException(status_code=409, detail="Video portfolio is available after token billing migration.")
    data = await file.read(MAX_VIDEO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Video file is empty.")
    if len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=400, detail="Video must be 50 MB or smaller.")
    if not detect_video(data):
        raise HTTPException(status_code=400, detail="Upload a valid MP4 or WebM video.")
    duration = await asyncio.to_thread(_verify_video_duration, data)
    await file.seek(0)
    video_id = str(uuid.uuid4())
    cost = int(catalog.token_costs.video_30_seconds_30_days)
    key = _request_key(idempotency_key, f"portfoliovideo:{current_user['id']}:{video_id}")

    async def action(reservation_id: str):
        await file.seek(0)
        url = await save_video(file, current_user["id"], "portfolio_videos")
        now = utc_now()
        record = {
            "id": video_id, "url": url, "duration_seconds": duration, "max_duration_seconds": VIDEO_DURATION_SECONDS,
            "created_at": utc_iso(now), "expires_at": utc_iso(now + timedelta(days=30)),
            "reservation_id": reservation_id, "billing_source": "tokens"
        }
        current = await db.users.find_one({"id": current_user["id"]}, {"_id": 0, "portfolio_videos": 1})
        if not current:
            raise HTTPException(status_code=404, detail="User not found.")
        if len(current.get("portfolio_videos") or []) >= MAX_PROFILE_VIDEOS:
            raise HTTPException(status_code=400, detail=f"Maximum {MAX_PROFILE_VIDEOS} portfolio videos allowed.")
        await db.users.update_one(
            {"id": current_user["id"]}, {"$push": {"portfolio_videos": record}}
        )
        return {"video": record}

    result = await execute_token_action(
        current_user, amount=cost, idempotency_key=key, reason="Post 30-second portfolio video for 30 days",
        action=action, reference_id=video_id,
        metadata={"action_type": "portfolio_video", "feature": "video_30_seconds_30_days", "video_id": video_id}
    )
    return {"status": "uploaded", "tokens_charged": cost, "balance": result["balance"],
            "already_processed": result["already_processed"], **(result["result"] or {})}
