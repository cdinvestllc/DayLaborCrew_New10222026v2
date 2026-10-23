import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Depends, Query, Request, Header
from database import db
from auth import get_current_user, user_to_response
from models import JobCreate, JobUpdate, RatingCreate, ClockInRequest, DisputeCreate, DisputeMessageCreate, DisputeAppealCreate
from utils.geocoding import geocode_address, valid_coords, distance_between
from utils.email_utils import send_job_completion_email
from utils.ai_utils import generate_smart_job_matches
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def now_str():
    return datetime.now(timezone.utc).isoformat()


CLOCK_IN_RADIUS_MILES = 0.5
MAX_CLOCK_IN_ACCURACY_METERS = 250


def validate_clock_in_accuracy(accuracy_meters) -> None:
    """Reject stale/very imprecise device readings when accuracy is provided."""
    if accuracy_meters is not None and accuracy_meters > MAX_CLOCK_IN_ACCURACY_METERS:
        raise HTTPException(
            status_code=400,
            detail="Your GPS location is not accurate enough yet. Move outdoors or enable precise location, then try again."
        )


def validate_clock_in_location(job: dict, lat, lng, radius_miles: float = CLOCK_IN_RADIUS_MILES) -> float:
    """Return GPS distance to a job or raise if a safe geofence decision is impossible."""
    provided = valid_coords(lat, lng)
    if not provided:
        raise HTTPException(status_code=400, detail="Valid device GPS coordinates are required")

    location = job.get("location") if isinstance(job.get("location"), dict) else {}
    job_coords = valid_coords(location.get("lat"), location.get("lng"))
    if not job_coords:
        job_coords = valid_coords(job.get("lat"), job.get("lng"))
    if not job_coords:
        raise HTTPException(
            status_code=409,
            detail="Clock-in is unavailable because this job has no valid map location. Ask the contractor to update the job address."
        )

    distance = distance_between(provided, job_coords)
    if distance is None or distance > radius_miles:
        raise HTTPException(
            status_code=403,
            detail=f"You must be within {radius_miles:g} mile of the job location to clock in"
        )
    return distance


router = APIRouter()


@router.post("/", status_code=201)
async def create_job(
    data: JobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can post jobs")

    # Validate input/address/images before reserving legacy credits or tokens.
    coords = valid_coords(data.lat, data.lng)
    if coords:
        location = {"address": data.address, "lat": coords[0], "lng": coords[1], "city": data.city or "", "state": data.state or ""}
    else:
        location = await geocode_address(data.address)
    if not location:
        raise HTTPException(status_code=400, detail="We couldn't locate that address. Please pick a suggested address.")
    images = _validated_job_images(data.images, current_user["id"])
    from token_actions import active_catalog, execute_token_action, record_qualifying_activity, uses_token_billing
    from token_billing import utc_iso
    token_mode = uses_token_billing(current_user)
    is_emergency = bool(data.is_emergency)
    job_id = str(uuid.uuid4())

    if token_mode:
        _, catalog = await active_catalog()
        cost = int(catalog.token_costs.contractor_post_job)
        if is_emergency:
            cost += int(catalog.token_costs.emergency_job_surcharge)
        action_key = (idempotency_key or f"jobpost:{job_id}").strip()
        job_doc = {
            "id": job_id, "contractor_id": current_user["id"],
            "contractor_name": current_user.get("company_name") or current_user["name"],
            "contractor_photo": current_user.get("logo") or current_user.get("profile_photo"),
            "title": data.title, "description": data.description, "trade": data.trade,
            "crew_needed": data.crew_needed, "crew_accepted": [],
            "start_time": data.start_time, "end_time": data.end_time, "pay_rate": data.pay_rate,
            "location": location, "status": "open", "is_emergency": is_emergency,
            "emergency_paid": is_emergency, "created_at": now_str(), "completed_at": None,
            "rated_crew": [], "rated_by_crew": [], "invited_crew": [], "images": images,
            "billing_source": "tokens", "token_cost": cost
        }

        async def persist_job(reservation_id: str):
            doc = {**job_doc, "token_reservation_id": reservation_id}
            await db.jobs.insert_one(doc)
            doc.pop("_id", None)
            await publish_job(doc)
            return doc

        result = await execute_token_action(
            current_user, amount=cost, idempotency_key=action_key,
            reason="Post emergency job" if is_emergency else "Post contractor job",
            action=persist_job, reference_id=job_id,
            metadata={"action_type": "job_posting", "job_id": job_id, "emergency": is_emergency}
        )
        saved = result.get("result")
        if not saved:
            saved = await db.jobs.find_one({"token_reservation_id": result.get("reservation_id")}, {"_id": 0})
        if not saved:
            saved = await db.jobs.find_one({"id": job_id}, {"_id": 0})
        await record_qualifying_activity(
            current_user["id"], "job_posting", f"jobpost:{job_id}",
            metadata={"job_id": job_id, "emergency": is_emergency}
        )
        if not saved:
            raise HTTPException(status_code=503, detail="Job was saved but is still being reconciled; refresh your jobs shortly.")
        return {**saved, "tokens_charged": cost, "token_balance": result["balance"],
                "already_processed": result["already_processed"]}

    # Legacy-paid accounts remain on the existing credit system until their paid-through
    # date. This avoids charging legacy subscribers tokens for a feature they already bought.
    from routes.payment_routes import consume_job_post_credit, refund_job_post_credit
    await consume_job_post_credit(current_user)
    job_doc = {
        "id": job_id, "contractor_id": current_user["id"],
        "contractor_name": current_user.get("company_name") or current_user["name"],
        "contractor_photo": current_user.get("logo") or current_user.get("profile_photo"),
        "title": data.title, "description": data.description, "trade": data.trade,
        "crew_needed": data.crew_needed, "crew_accepted": [],
        "start_time": data.start_time, "end_time": data.end_time, "pay_rate": data.pay_rate,
        "location": location, "status": "pending_payment" if is_emergency else "open",
        "is_emergency": is_emergency, "emergency_paid": False, "created_at": now_str(),
        "completed_at": None, "rated_crew": [], "rated_by_crew": [], "invited_crew": [], "images": images
    }
    try:
        await db.jobs.insert_one(job_doc)
    except Exception:
        await refund_job_post_credit(current_user)
        raise
    job_doc.pop("_id", None)
    if job_doc["status"] == "pending_payment":
        from routes.payment_routes import get_emergency_price
        return {**job_doc, "requires_payment": True, "emergency_fee": await get_emergency_price()}
    await publish_job(job_doc)
    await record_qualifying_activity(current_user["id"], "job_posting", f"jobpost:{job_id}",
                                     metadata={"job_id": job_id, "emergency": False})
    return job_doc


async def publish_job(job_doc: dict):
    """Announces a live job: WebSocket broadcast + background AI crew matching."""
    try:
        from routes.ws_routes import manager
        await manager.broadcast_new_job(job_doc)
    except Exception as e:
        logger.warning(f"WS broadcast failed: {e}")
    import asyncio
    asyncio.create_task(_run_ai_match(job_doc))


def _validated_job_images(images, user_id: str) -> list:
    images = images or []
    prefix = f"/api/public/files/thedaylaborers/job_images/{user_id}/"
    if len(images) > 6 or any(not isinstance(u, str) or not u.startswith(prefix) for u in images):
        raise HTTPException(status_code=400, detail="Job images must be up to 6 of your uploaded images")
    return images


@router.get("/")
async def list_jobs(
    status: Optional[str] = None,
    trade: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius: Optional[float] = 25,
    smart_match: Optional[bool] = False,
    current_user: dict = Depends(get_current_user)
):
    query = {}

    if current_user["role"] == "contractor":
        # Contractors only see their own non-archived jobs.
        query["contractor_id"] = current_user["id"]
        query["archived_at"] = {"$exists": False}
    else:
        # Crew only ever sees live jobs (never unpaid, cancelled or other contractors' private states)
        crew_visible = ["open", "fulfilled"]
        query["status"] = status if status in crew_visible else {"$in": crew_visible}

    if trade:
        query["trade"] = trade

    jobs = await db.jobs.find(query, {"_id": 0}).sort("created_at", -1).to_list(200)

    # Filter by radius if location provided (jobs without valid coordinates are excluded)
    origin = valid_coords(lat, lng)
    if origin:
        radius = min(max(radius or 25, 1), 250)
        jobs = [j for j in jobs if (d := distance_between(origin, valid_coords((j.get("location") or {}).get("lat"), (j.get("location") or {}).get("lng")))) is not None and d <= radius]

    # AI smart matching for crew
    if smart_match and current_user["role"] == "crew" and jobs:
        jobs = await generate_smart_job_matches(jobs, current_user)

    return jobs


@router.get("/my-jobs")
async def my_jobs(current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "contractor":
        jobs = await db.jobs.find(
            {"contractor_id": current_user["id"], "archived_at": {"$exists": False}},
            {"_id": 0}).sort("created_at", -1).to_list(100)
    else:
        jobs = await db.jobs.find(
            {"crew_accepted": current_user["id"]},
            {"_id": 0}
        ).sort("created_at", -1).to_list(100)
    return jobs


@router.get("/{job_id}")
async def get_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") == "pending_payment" and job["contractor_id"] != current_user["id"] \
            and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.put("/{job_id}")
async def update_job(job_id: str, data: JobUpdate, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")
    if job["status"] not in ("open", "fulfilled", "suspended"):
        raise HTTPException(status_code=400, detail=f"A {job['status']} job can no longer be edited")

    update = {k: v for k, v in data.model_dump().items() if v is not None}
    update.pop("status", None)  # status changes only through workflow endpoints
    if update.get("is_emergency") and not job.get("is_emergency") and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=400, detail="Emergency status requires the emergency fee. Post a new emergency job instead.")
    if "crew_needed" in update:
        if update["crew_needed"] < max(1, len(job.get("crew_accepted", []))):
            raise HTTPException(status_code=400, detail="Crew needed cannot be less than crew already accepted")
        if job["status"] in ("open", "fulfilled"):
            update["status"] = "fulfilled" if len(job.get("crew_accepted", [])) >= update["crew_needed"] else "open"
    if update:
        await db.jobs.update_one({"id": job_id}, {"$set": update})
    return {"message": "Job updated"}


@router.delete("/{job_id}")
async def delete_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")
    if job.get("crew_accepted") and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=400, detail="Jobs with accepted crew must be cancelled, not deleted")

    pending_emergency = job.get("status") == "pending_payment" and job.get("is_emergency")
    if pending_emergency:
        # Atomically cancel every pending checkout. The payment webhook can only claim
        # a transaction while it is still pending, so either deletion or fulfillment wins.
        while True:
            cancelled_tx = await db.payment_transactions.find_one_and_update(
                {"job_id": job_id, "plan": "emergency_job", "payment_status": "pending"},
                {"$set": {"payment_status": "cancelled", "cancelled_at": now_str(),
                          "cancel_reason": "job_deleted"}})
            if not cancelled_tx:
                break

        paid_tx = await db.payment_transactions.find_one({
            "job_id": job_id, "plan": "emergency_job", "payment_status": "paid"})
        if paid_tx:
            await db.payment_transactions.update_many(
                {"job_id": job_id, "plan": "emergency_job", "payment_status": "cancelled",
                 "cancel_reason": "job_deleted"},
                {"$set": {"payment_status": "pending"},
                 "$unset": {"cancelled_at": "", "cancel_reason": ""}})
            raise HTTPException(status_code=409, detail="Payment has completed. Refresh your jobs before deleting.")

    delete_filter = {"id": job_id}
    if pending_emergency:
        delete_filter["status"] = "pending_payment"
    result = await db.jobs.delete_one(delete_filter)
    if result.deleted_count != 1:
        if pending_emergency:
            latest = await db.jobs.find_one({"id": job_id}, {"_id": 0, "status": 1})
            if latest and latest.get("status") == "pending_payment":
                await db.payment_transactions.update_many(
                    {"job_id": job_id, "plan": "emergency_job", "payment_status": "cancelled",
                     "cancel_reason": "job_deleted"},
                    {"$set": {"payment_status": "pending"},
                     "$unset": {"cancelled_at": "", "cancel_reason": ""}})
        raise HTTPException(status_code=409, detail="Job status changed while deleting. Refresh and try again.")

    if pending_emergency and job["contractor_id"] == current_user["id"]:
        from routes.payment_routes import refund_job_post_credit
        await refund_job_post_credit(current_user)
    return {"message": "Job deleted"}


@router.post("/{job_id}/accept")
async def accept_job(job_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "crew":
        raise HTTPException(status_code=403, detail="Only crew members can accept jobs")
    from token_actions import execute_token_action, record_qualifying_activity, uses_token_billing

    token_mode = uses_token_billing(current_user)
    if not token_mode:
        from routes.payment_routes import require_active_subscription
        require_active_subscription(current_user)

    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user["id"] in job.get("crew_accepted", []):
        raise HTTPException(status_code=400, detail="Already accepted this job")
    if job.get("status") != "open":
        raise HTTPException(status_code=400, detail=f"Job is {job.get('status')}, cannot accept")

    async def perform_acceptance(reservation_id: str | None = None):
        update = {"$push": {"crew_accepted": current_user["id"]},
                  "$pull": {"invited_crew": current_user["id"]}}
        if reservation_id:
            update["$push"]["crew_acceptance_receipts"] = {
                "crew_id": current_user["id"], "reservation_id": reservation_id, "accepted_at": now_str()
            }
        changed_job = await db.jobs.find_one_and_update(
            {"id": job_id, "status": "open", "crew_accepted": {"$ne": current_user["id"]},
             "$expr": {"$lt": [{"$size": "$crew_accepted"}, "$crew_needed"]}},
            update, return_document=True, projection={"_id": 0},
        )
        if not changed_job:
            raise HTTPException(status_code=409, detail="This job is no longer accepting crew")
        new_crew = changed_job["crew_accepted"]
        new_status = "open"
        if len(new_crew) >= changed_job["crew_needed"]:
            await db.jobs.update_one({"id": job_id, "status": "open"}, {"$set": {"status": "fulfilled"}})
            new_status = "fulfilled"
        return {"job": changed_job, "status": new_status}

    if token_mode:
        _, catalog = await __import__("token_actions").active_catalog()
        cost = int(catalog.token_costs.crew_apply_job)
        result = await execute_token_action(
            current_user, amount=cost, idempotency_key=f"applyjob:{job_id}:{current_user['id']}",
            reason="Apply to contractor job",
            action=perform_acceptance, reference_id=job_id,
            metadata={"action_type": "job_application", "job_id": job_id, "crew_id": current_user["id"]}
        )
        accepted = result.get("result")
        if not accepted:
            accepted_doc = await db.jobs.find_one({"id": job_id}, {"_id": 0, "crew_accepted": 1, "status": 1})
            accepted = {"job": accepted_doc or {}, "status": (accepted_doc or {}).get("status", "open")}
        await record_qualifying_activity(
            current_user["id"], "job_application", f"apply:{job_id}:{current_user['id']}",
            metadata={"job_id": job_id}
        )
        cost_charged = cost
    else:
        accepted = await perform_acceptance()
        cost_charged = 0

    # Notify contractor via WebSocket
    try:
        from routes.ws_routes import manager
        await manager.send_to_user(job["contractor_id"], {
            "type": "job_accepted", "job_id": job_id, "crew_name": current_user["name"],
            "crew_count": len(accepted["job"].get("crew_accepted", [])), "crew_needed": job["crew_needed"]
        })
    except Exception:
        pass

    try:
        from utils.push_utils import send_push_notification
        await send_push_notification(db, job["contractor_id"], "job_accepted",
                                     job_title=job.get("title", ""), crew_name=current_user["name"])
    except Exception:
        pass

    response = {"message": "Job accepted", "status": accepted["status"]}
    if token_mode:
        response.update({"tokens_charged": cost_charged, "token_balance": result["balance"],
                         "already_processed": result["already_processed"]})
    return response


@router.post("/{job_id}/start")
async def start_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Only the contractor can start this job")
    if not job.get("crew_accepted"):
        raise HTTPException(status_code=400, detail="At least one crew member must accept before starting")
    result = await db.jobs.update_one({"id": job_id, "status": {"$in": ["open", "fulfilled"]}},
                                      {"$set": {"status": "in_progress", "started_at": now_str()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Job cannot be started in current status")
    return {"message": "Job started"}


@router.get("/{job_id}/clock-in/me")
async def get_my_clock_in_status(job_id: str, current_user: dict = Depends(get_current_user)):
    """Return only the current crew member's clock-in status for an accepted job."""
    if current_user.get("role") != "crew":
        raise HTTPException(status_code=403, detail="Only crew members can view crew clock-in status")
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0, "id": 1, "crew_accepted": 1})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user["id"] not in job.get("crew_accepted", []):
        raise HTTPException(status_code=403, detail="Not a crew member on this job")
    record = await db.job_attendance.find_one(
        {"_id": f"{job_id}:{current_user['id']}"},
        {"_id": 0, "checked_in_at": 1, "distance_miles": 1, "radius_miles": 1})
    if not record:
        return {"clocked_in": False}
    return {"clocked_in": True, **record}


@router.post("/{job_id}/clock-in")
async def clock_in_job(job_id: str, data: ClockInRequest, current_user: dict = Depends(get_current_user)):
    """Record a one-time, server-verified proximity check-in for an active job."""
    if current_user.get("role") != "crew":
        raise HTTPException(status_code=403, detail="Only crew members can clock in")
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user["id"] not in job.get("crew_accepted", []):
        raise HTTPException(status_code=403, detail="Not a crew member on this job")
    if job.get("status") != "in_progress":
        raise HTTPException(status_code=400, detail="You can only clock in after the contractor starts the job")
    validate_clock_in_accuracy(data.accuracy_meters)

    attendance_id = f"{job_id}:{current_user['id']}"
    existing = await db.job_attendance.find_one({"_id": attendance_id}, {"_id": 0, "checked_in_at": 1})
    if existing:
        return {"message": "Already clocked in", "clocked_in": True,
                "already_clocked_in": True, "checked_in_at": existing["checked_in_at"]}

    distance = validate_clock_in_location(job, data.lat, data.lng)
    checked_in_at = now_str()
    # Store distance and accuracy for audit; do not store raw GPS coordinates.
    record = {
        "_id": attendance_id,
        "job_id": job_id,
        "crew_id": current_user["id"],
        "contractor_id": job["contractor_id"],
        "checked_in_at": checked_in_at,
        "distance_miles": round(distance, 4),
        "radius_miles": CLOCK_IN_RADIUS_MILES,
        "accuracy_meters": data.accuracy_meters,
        "verification_method": "browser_geolocation",
    }
    from pymongo.errors import DuplicateKeyError
    try:
        await db.job_attendance.insert_one(record)
    except DuplicateKeyError:
        existing = await db.job_attendance.find_one(
            {"_id": attendance_id}, {"_id": 0, "checked_in_at": 1})
        if existing:
            return {"message": "Already clocked in", "clocked_in": True,
                    "already_clocked_in": True, "checked_in_at": existing["checked_in_at"]}
        raise HTTPException(status_code=409, detail="Clock-in is already being recorded. Refresh and try again.")
    return {
        "message": "Clock-in recorded",
        "clocked_in": True,
        "already_clocked_in": False,
        "checked_in_at": checked_in_at,
        "distance_miles": round(distance, 4),
        "radius_miles": CLOCK_IN_RADIUS_MILES,
    }


@router.post("/{job_id}/complete")
async def complete_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if current_user["id"] not in job.get("crew_accepted", []):
        raise HTTPException(status_code=403, detail="Not a crew member on this job")
    result = await db.jobs.update_one({"id": job_id, "status": "in_progress"},
                                      {"$set": {"status": "completed_pending_review"}})
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Job must be in_progress to complete")

    # Notify contractor
    contractor = await db.users.find_one({"id": job["contractor_id"]}, {"_id": 0})
    if contractor:
        await send_job_completion_email(contractor["email"], contractor["name"], job["title"])
        try:
            from routes.ws_routes import manager
            await manager.send_to_user(job["contractor_id"], {
                "type": "job_completed",
                "job_id": job_id,
                "job_title": job["title"]
            })
        except Exception:
            pass
        # Push notification
        try:
            from utils.push_utils import send_push_notification
            await send_push_notification(db, job["contractor_id"], "job_completed", job_title=job.get("title", ""))
        except Exception:
            pass

    return {"message": "Job marked complete, awaiting contractor review"}


JOB_DISPUTE_CATEGORIES = {"completion", "conduct", "safety", "scope", "other"}
RATING_DISPUTE_CATEGORIES = {"inaccurate", "abusive", "retaliatory", "other"}


def validate_dispute_submission(job: dict, reporter: dict, dispute_type: str, category: str,
                                rating: dict | None = None) -> str | None:
    """Validate participant permissions and category before accepting dispute writes."""
    reporter_id = reporter.get("id")
    is_contractor = reporter_id == job.get("contractor_id") and reporter.get("role") == "contractor"
    is_crew = reporter.get("role") == "crew" and reporter_id in job.get("crew_accepted", [])
    if not (is_contractor or is_crew):
        raise HTTPException(status_code=403, detail="Only participants in this job can open a dispute")
    if dispute_type == "job":
        if category not in JOB_DISPUTE_CATEGORIES:
            raise HTTPException(status_code=400, detail="Invalid job dispute category")
        if job.get("status") not in {"in_progress", "completed_pending_review"}:
            raise HTTPException(status_code=409, detail="Job disputes must be opened while work is in progress or awaiting completion review")
        return None if is_contractor else job.get("contractor_id")
    if dispute_type == "rating":
        if category not in RATING_DISPUTE_CATEGORIES:
            raise HTTPException(status_code=400, detail="Invalid rating dispute category")
        if not rating or rating.get("job_id") != job.get("id"):
            raise HTTPException(status_code=404, detail="Rating not found for this job")
        if reporter_id not in {rating.get("rater_id"), rating.get("rated_id")}:
            raise HTTPException(status_code=403, detail="Only the author or recipient of this rating can dispute it")
        return rating.get("rater_id") if reporter_id == rating.get("rated_id") else rating.get("rated_id")
    raise HTTPException(status_code=400, detail="Dispute type must be job or rating")


def normalize_dispute_reason(reason: str) -> str:
    """Trim a dispute reason and reject whitespace-only submissions."""
    normalized = reason.strip()
    if len(normalized) < 10:
        raise HTTPException(status_code=400, detail="Please describe the issue in at least 10 non-space characters")
    return normalized


def normalize_dispute_message(body: str) -> str:
    message = body.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    return message


def dispute_participant_ids(dispute: dict) -> set[str]:
    people = {dispute.get("reporter_id"), dispute.get("respondent_id")}
    if isinstance(dispute.get("respondent_ids"), list):
        people.update(dispute["respondent_ids"])
    people.discard(None)
    return people


def can_access_dispute(dispute: dict, user_id: str) -> bool:
    return user_id in dispute_participant_ids(dispute)


def _parse_dispute_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def dispute_appeal_deadline(dispute: dict):
    explicit = _parse_dispute_time(dispute.get("appeal_deadline"))
    if explicit:
        return explicit
    resolved = _parse_dispute_time(dispute.get("resolved_at"))
    return resolved + timedelta(days=7) if resolved else None


def dispute_can_message(dispute: dict, now=None) -> bool:
    if dispute.get("status") in {"open", "appealed"}:
        return True
    if dispute.get("status") != "resolved":
        return False
    deadline = dispute_appeal_deadline(dispute)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return bool(deadline and current <= deadline)


def dispute_can_appeal(dispute: dict, user_id: str, now=None) -> bool:
    if not can_access_dispute(dispute, user_id) or dispute.get("status") != "resolved" or dispute.get("appeal"):
        return False
    deadline = dispute_appeal_deadline(dispute)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return bool(deadline and current <= deadline)


def validate_dispute_appeal(dispute: dict, user_id: str, now=None):
    if not can_access_dispute(dispute, user_id):
        raise HTTPException(status_code=403, detail="Only participants in this dispute can appeal")
    if dispute.get("status") != "resolved":
        raise HTTPException(status_code=409, detail="Only resolved disputes can be appealed")
    if dispute.get("appeal"):
        raise HTTPException(status_code=409, detail="This dispute has already been appealed")
    deadline = dispute_appeal_deadline(dispute)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not deadline or current > deadline:
        raise HTTPException(status_code=409, detail="The seven-day appeal window has closed")
    return deadline


def validate_appeal_resolution(dispute: dict, resolution: str) -> None:
    if dispute.get("dispute_type") == "job":
        allowed = {"uphold_original", "uphold", "dismiss"}
    else:
        allowed = {"uphold_original", "hide_rating", "restore_rating"}
    if resolution not in allowed:
        raise HTTPException(status_code=400, detail="Appeal resolution is not valid for this dispute type")
    if dispute.get("dispute_type") == "rating" and resolution != "uphold_original" and not dispute.get("rating_id"):
        raise HTTPException(status_code=409, detail="This rating dispute has no linked rating")


def validate_dispute_resolution(dispute: dict, resolution: str) -> None:
    allowed = {"uphold", "dismiss"} if dispute.get("dispute_type") == "job" else {"hide_rating", "restore_rating", "dismiss"}
    if resolution not in allowed:
        raise HTTPException(status_code=400, detail="Resolution is not valid for this dispute type")
    if dispute.get("dispute_type") == "rating" and resolution != "dismiss" and not dispute.get("rating_id"):
        raise HTTPException(status_code=409, detail="This rating dispute has no linked rating")


def rating_summary(stars: list[int | float]) -> dict:
    clean = [value for value in stars if isinstance(value, (int, float)) and 1 <= value <= 5]
    return {"rating": round(sum(clean) / len(clean), 1) if clean else 0, "rating_count": len(clean)}


@router.post("/{job_id}/disputes")
async def create_job_dispute(job_id: str, data: DisputeCreate, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    rating = None
    if data.dispute_type == "rating":
        if not data.rating_id:
            raise HTTPException(status_code=400, detail="rating_id is required for rating disputes")
        rating = await db.ratings.find_one({"id": data.rating_id}, {"_id": 0})
    validate_dispute_submission(job, current_user, data.dispute_type, data.category, rating)
    clean_reason = normalize_dispute_reason(data.reason)
    duplicate_query = {"job_id": job_id, "reporter_id": current_user["id"],
                       "dispute_type": data.dispute_type, "status": "open"}
    if data.dispute_type == "rating":
        duplicate_query["rating_id"] = data.rating_id
    if await db.disputes.find_one(duplicate_query, {"_id": 0, "id": 1}):
        raise HTTPException(status_code=409, detail="You already have an open dispute for this item")
    dispute = {
        "id": str(uuid.uuid4()), "job_id": job_id, "job_title": job.get("title", ""),
        "dispute_type": data.dispute_type, "category": data.category,
        "reason": clean_reason, "evidence": data.evidence.strip() if data.evidence else "",
        "rating_id": data.rating_id if data.dispute_type == "rating" else None,
        "reporter_id": current_user["id"], "reporter_role": current_user["role"],
        "respondent_id": None,
        "respondent_ids": job.get("crew_accepted", []) if current_user["id"] == job.get("contractor_id") else [job.get("contractor_id")],
        "status": "open", "created_at": now_str(),
    }
    if data.dispute_type == "rating":
        dispute["respondent_id"] = rating.get("rated_id") if current_user["id"] == rating.get("rater_id") else rating.get("rater_id")
        dispute["respondent_ids"] = [dispute["respondent_id"]]
    elif current_user["id"] != job.get("contractor_id"):
        dispute["respondent_id"] = job.get("contractor_id")
    await db.disputes.insert_one(dispute)
    if data.dispute_type == "job":
        await db.jobs.update_one({"id": job_id}, {"$set": {"dispute_status": "open", "last_dispute_id": dispute["id"]}})
    return {"message": "Dispute submitted for administrator review", "dispute": {k: v for k, v in dispute.items() if k != "_id"}}


@router.get("/{job_id}/disputes")
async def get_my_job_disputes(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0, "contractor_id": 1, "crew_accepted": 1})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user["id"] != job["contractor_id"] and current_user["id"] not in job.get("crew_accepted", []):
        raise HTTPException(status_code=403, detail="Not part of this job")
    return await db.disputes.find(
        {"job_id": job_id, "$or": [
            {"reporter_id": current_user["id"]},
            {"respondent_id": current_user["id"]},
            {"respondent_ids": current_user["id"]},
        ]},
        {"_id": 0}
    ).sort("created_at", -1).to_list(50)


@router.get("/{job_id}/disputes/{dispute_id}/messages")
async def get_dispute_conversation(job_id: str, dispute_id: str, current_user: dict = Depends(get_current_user)):
    dispute = await db.disputes.find_one({"id": dispute_id, "job_id": job_id}, {"_id": 0})
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if not can_access_dispute(dispute, current_user["id"]):
        raise HTTPException(status_code=403, detail="Only the parties to this dispute can view its conversation")
    visible_fields = (
        "id", "job_id", "job_title", "dispute_type", "category", "reason", "evidence", "rating_id",
        "reporter_id", "reporter_role", "respondent_id", "respondent_ids", "status", "created_at",
        "resolution", "resolution_notes", "resolved_at", "appeal_deadline", "appeal",
        "appeal_resolution", "appeal_resolution_notes", "appeal_resolved_at",
    )
    return {
        "dispute": {key: dispute[key] for key in visible_fields if key in dispute},
        "messages": dispute.get("messages", []),
        "can_message": dispute_can_message(dispute),
        "can_appeal": dispute_can_appeal(dispute, current_user["id"]),
    }


@router.post("/{job_id}/disputes/{dispute_id}/messages")
async def post_dispute_message(job_id: str, dispute_id: str, data: DisputeMessageCreate,
                               current_user: dict = Depends(get_current_user)):
    dispute = await db.disputes.find_one({"id": dispute_id, "job_id": job_id}, {"_id": 0})
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if not can_access_dispute(dispute, current_user["id"]):
        raise HTTPException(status_code=403, detail="Only the parties to this dispute can reply")
    if not dispute_can_message(dispute):
        raise HTTPException(status_code=409, detail="This dispute conversation is closed")
    message = {
        "id": str(uuid.uuid4()), "sender_id": current_user["id"],
        "sender_name": current_user.get("name") or current_user.get("email") or current_user["id"],
        "sender_role": current_user.get("role", "user"),
        "body": normalize_dispute_message(data.body), "message_type": "message", "created_at": now_str(),
    }
    result = await db.disputes.update_one(
        {"id": dispute_id, "status": dispute.get("status")},
        {"$push": {"messages": {"$each": [message], "$slice": -500}},
         "$set": {"updated_at": now_str()}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Dispute status changed. Refresh and try again.")
    return {"message": "Reply added", "item": message}


@router.post("/{job_id}/disputes/{dispute_id}/appeal")
async def appeal_dispute(job_id: str, dispute_id: str, data: DisputeAppealCreate,
                         current_user: dict = Depends(get_current_user)):
    dispute = await db.disputes.find_one({"id": dispute_id, "job_id": job_id}, {"_id": 0})
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    deadline = validate_dispute_appeal(dispute, current_user["id"])
    reason = normalize_dispute_reason(data.reason)
    now = now_str()
    appeal = {
        "submitted_by": current_user["id"],
        "submitted_by_name": current_user.get("name") or current_user.get("email") or current_user["id"],
        "submitted_by_role": current_user.get("role", "user"),
        "reason": reason, "evidence": data.evidence.strip() if data.evidence else "",
        "created_at": now, "deadline": deadline.isoformat(),
    }
    message = {
        "id": str(uuid.uuid4()), "sender_id": current_user["id"],
        "sender_name": appeal["submitted_by_name"], "sender_role": appeal["submitted_by_role"],
        "body": "Formal appeal submitted: " + reason, "evidence": appeal["evidence"],
        "message_type": "appeal", "created_at": now,
    }
    changed = await db.disputes.update_one(
        {"id": dispute_id, "status": "resolved", "appeal": {"$exists": False}},
        {"$set": {"status": "appealed", "appeal": appeal, "updated_at": now},
         "$push": {"messages": {"$each": [message], "$slice": -500}}})
    if changed.modified_count != 1:
        raise HTTPException(status_code=409, detail="This dispute has already been appealed or its status changed")
    if dispute.get("dispute_type") == "job":
        await db.jobs.update_one(
            {"id": job_id},
            {"$set": {"dispute_status": "open", "last_dispute_id": dispute_id}})
    return {"message": "Appeal submitted for administrator review", "appeal": appeal}


@router.post("/{job_id}/verify")
async def verify_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the contractor can verify")
    open_dispute = await db.disputes.find_one(
        {"job_id": job_id, "dispute_type": "job", "status": {"$in": ["open", "appealed"]}},
        {"_id": 0, "id": 1})
    if open_dispute:
        raise HTTPException(status_code=409, detail="This job has an open dispute. An administrator must resolve it before completion can be verified.")
    now = now_str()
    result = await db.jobs.update_one({"id": job_id, "status": "completed_pending_review"},
                                      {"$set": {"status": "completed", "completed_at": now}})
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Job not pending review")

    # Award points to crew members
    for crew_id in job.get("crew_accepted", []):
        await db.users.update_one(
            {"id": crew_id},
            {"$inc": {"points": 50, "jobs_completed": 1}}
        )

    return {"message": "Job verified and completed"}


@router.post("/{job_id}/rate")
async def rate_user(job_id: str, data: RatingCreate, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Can only rate after the contractor verifies completion")

    # Contractor rates accepted crew; crew rate the contractor
    is_contractor = job["contractor_id"] == current_user["id"]
    is_crew = current_user["id"] in job.get("crew_accepted", [])
    if not (is_contractor or is_crew):
        raise HTTPException(status_code=403, detail="Not part of this job")
    if data.rated_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot rate yourself")
    if is_contractor and data.rated_id not in job.get("crew_accepted", []):
        raise HTTPException(status_code=400, detail="You can only rate crew who worked this job")
    if is_crew and data.rated_id != job["contractor_id"]:
        raise HTTPException(status_code=400, detail="Crew can only rate the job's contractor")
    if data.review and len(data.review) > 1000:
        raise HTTPException(status_code=400, detail="Review is too long")

    # Check no duplicate rating
    existing = await db.ratings.find_one({
        "job_id": job_id,
        "rater_id": current_user["id"],
        "rated_id": data.rated_id
    })
    if existing:
        raise HTTPException(status_code=400, detail="Already rated this person for this job")

    if not 1 <= data.stars <= 5:
        raise HTTPException(status_code=400, detail="Stars must be 1-5")

    rating_doc = {
        "id": str(uuid.uuid4()),
        "rater_id": current_user["id"],
        "rated_id": data.rated_id,
        "job_id": job_id,
        "stars": data.stars,
        "review": data.review,
        "created_at": now_str()
    }
    from pymongo.errors import DuplicateKeyError
    try:
        await db.ratings.insert_one(rating_doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=400, detail="Already rated this person for this job")
    field = "rated_crew" if is_contractor else "rated_by_crew"
    await db.jobs.update_one({"id": job_id}, {"$addToSet": {field: data.rated_id if is_contractor else current_user["id"]}})

    # Update average rating (hidden/moderated reviews excluded)
    all_ratings = await db.ratings.find({"rated_id": data.rated_id, "hidden": {"$ne": True}}, {"_id": 0, "stars": 1}).to_list(5000)
    avg = sum(r["stars"] for r in all_ratings) / len(all_ratings) if all_ratings else 0
    await db.users.update_one(
        {"id": data.rated_id},
        {"$set": {"rating": round(avg, 1), "rating_count": len(all_ratings)}}
    )

    return {"message": "Rating submitted", "rating": {k: v for k, v in rating_doc.items() if k != "_id"}}


@router.get("/{job_id}/ratings")
async def get_job_ratings(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0, "contractor_id": 1, "crew_accepted": 1})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user["id"] != job["contractor_id"] and current_user["id"] not in job.get("crew_accepted", []):
        raise HTTPException(status_code=403, detail="Only job participants can view job ratings")
    return await db.ratings.find(
        {"job_id": job_id, "hidden": {"$ne": True}}, {"_id": 0}).to_list(100)


# ─── Contractor: Cancel / Suspend Job ─────────────────────────────────────────

@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    from pydantic import BaseModel
    from typing import Optional

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    reason = body.get("reason", "") if isinstance(body, dict) else ""

    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    is_contractor = job["contractor_id"] == current_user["id"]
    is_admin = current_user.get("role") in ("admin", "super_admin")
    if not (is_contractor or is_admin):
        raise HTTPException(status_code=403, detail="Only the contractor can cancel this job")

    if job["status"] in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {job['status']} job")

    # Keep job visible for 12h after cancel (admin-defined)
    settings = await db.settings.find_one({}, {"_id": 0}) or {}
    visibility_hours = settings.get("job_visibility_hours", 12)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=visibility_hours)).isoformat()

    await db.jobs.update_one(
        {"id": job_id},
        {"$set": {"status": "cancelled", "cancelled_at": now_str(), "cancel_reason": reason, "expires_at": expires_at}}
    )

    # Notify all accepted crew via in-app notification + email
    crew_ids = job.get("crew_accepted", [])
    for crew_id in crew_ids:
        crew = await db.users.find_one({"id": crew_id}, {"_id": 0, "email": 1, "name": 1})
        if crew:
            # In-app notification
            await db.notifications.insert_one({
                "id": str(uuid.uuid4()),
                "user_id": crew_id,
                "type": "job_cancelled",
                "title": "Job Cancelled",
                "message": f'"{job["title"]}" has been cancelled by the contractor. {reason}',
                "job_id": job_id,
                "read": False,
                "created_at": now_str()
            })
            # Email notification
            from utils.email_utils import send_job_cancelled_email
            await send_job_cancelled_email(crew["email"], crew["name"], job["title"], reason)

    return {"message": "Job cancelled. Crew has been notified.", "crew_notified": len(crew_ids)}


@router.post("/{job_id}/archive")
async def archive_job(job_id: str, current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can archive jobs")
    job = await db.jobs.find_one(
        {"id": job_id}, {"_id": 0, "contractor_id": 1, "status": 1, "archived_at": 1})
    if not job or job.get("contractor_id") != current_user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "cancelled":
        raise HTTPException(status_code=400, detail="Only cancelled jobs can be archived")
    if not job.get("archived_at"):
        await db.jobs.update_one(
            {"id": job_id, "contractor_id": current_user["id"], "status": "cancelled",
             "archived_at": {"$exists": False}},
            {"$set": {"archived_at": now_str()}})
    return {"message": "Job archived"}


@router.get("/archived/list")
async def list_archived_jobs(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can view archived jobs")
    return await db.jobs.find(
        {"contractor_id": current_user["id"], "status": "cancelled", "archived_at": {"$exists": True}},
        {"_id": 0}).sort("archived_at", -1).to_list(200)


@router.post("/{job_id}/suspend")
async def suspend_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    is_contractor = job["contractor_id"] == current_user["id"]
    is_admin = current_user.get("role") in ("admin", "super_admin")
    if not (is_contractor or is_admin):
        raise HTTPException(status_code=403, detail="Only the contractor can suspend this job")

    if job["status"] in ("completed", "cancelled", "suspended"):
        raise HTTPException(status_code=400, detail=f"Cannot suspend a {job['status']} job")

    await db.jobs.update_one(
        {"id": job_id},
        {"$set": {"status": "suspended", "suspended_at": now_str(), "previous_status": job["status"]}}
    )

    # Notify crew
    crew_ids = job.get("crew_accepted", [])
    for crew_id in crew_ids:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": crew_id,
            "type": "job_suspended",
            "title": "Job Paused",
            "message": f'"{job["title"]}" has been temporarily paused by the contractor.',
            "job_id": job_id,
            "read": False,
            "created_at": now_str()
        })

    return {"message": "Job suspended. Crew has been notified.", "crew_notified": len(crew_ids)}


@router.post("/{job_id}/resume")
async def resume_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")

    if job["status"] != "suspended":
        raise HTTPException(status_code=400, detail="Job is not suspended")

    prev_status = job.get("previous_status", "open")
    await db.jobs.update_one({"id": job_id}, {"$set": {"status": prev_status}})
    return {"message": "Job resumed", "status": prev_status}


@router.get("/{job_id}/contact")
async def job_poster_contact(job_id: str, current_user: dict = Depends(get_current_user)):
    """Poster contact is released only to crew accepted on the job (or staff)."""
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user["id"] not in job.get("crew_accepted", []) and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Contact details are shared after you're hired on this job")
    if job.get("is_guest_post"):
        guest = await db.guest_jobs.find_one({"id": job["guest_job_id"], "status": "published"}, {"_id": 0, "contact": 1})
        return (guest or {}).get("contact", {})
    c = await db.users.find_one({"id": job["contractor_id"]}, {"_id": 0, "name": 1, "company_name": 1, "email": 1, "phone": 1})
    return c or {}


@router.post("/{job_id}/close")
async def admin_close_job(job_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """Admin ends a job: no further accepts or workflow changes."""
    if current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    reason = str((body or {}).get("reason", ""))[:300]
    result = await db.jobs.update_one(
        {"id": job_id, "status": {"$nin": ["completed", "cancelled", "closed"]}},
        {"$set": {"status": "closed", "closed_at": now_str(), "closed_by": current_user["id"], "close_reason": reason}})
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Job not found or already ended")
    await db.activity_logs.insert_one({"id": str(uuid.uuid4()), "admin_id": current_user["id"], "action": "close_job",
                                       "target_id": job_id, "details": reason, "created_at": now_str()})
    return {"message": "Job closed"}


@router.get("/{job_id}/recommended-crew")
async def recommended_crew(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")
    from utils.ai_utils import ai_match_crew_for_job
    from utils.profile_view import build_user_view
    crew = await db.users.find({"role": "crew", "is_active": True, "availability": True}, {"_id": 0, "password_hash": 0}).to_list(300)
    by_id = {c["id"]: c for c in crew}
    matches = (await ai_match_crew_for_job(job, crew))[:10]
    return [{**await build_user_view(by_id[m["crew_id"]], current_user, include_coords=True),
             "match_score": m["score"], "match_reason": m["reason"], "distance_miles": m["distance_miles"]} for m in matches]


# ─── Notifications ─────────────────────────────────────────────────────────────

@router.get("/notifications/me")
async def get_notifications(current_user: dict = Depends(get_current_user)):
    notifs = await db.notifications.find(
        {"user_id": current_user["id"]},
        {"_id": 0}
    ).sort("created_at", -1).limit(50).to_list(50)
    unread = sum(1 for n in notifs if not n.get("read"))
    return {"notifications": notifs, "unread": unread}


@router.post("/notifications/read-all")
async def mark_all_read(current_user: dict = Depends(get_current_user)):
    await db.notifications.update_many(
        {"user_id": current_user["id"], "read": False},
        {"$set": {"read": True}}
    )
    return {"message": "All notifications marked as read"}


@router.post("/notifications/{notif_id}/read")
async def mark_one_read(notif_id: str, current_user: dict = Depends(get_current_user)):
    await db.notifications.update_one(
        {"id": notif_id, "user_id": current_user["id"]},
        {"$set": {"read": True}}
    )
    return {"message": "Marked as read"}


# ─── AI Matching trigger ───────────────────────────────────────────────────────

@router.post("/{job_id}/ai-match")
async def trigger_ai_match(job_id: str, current_user: dict = Depends(get_current_user)):
    """Manually trigger AI matching for a job (contractors and admins)."""
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")

    matched = await _run_ai_match(job)
    return {"message": f"AI matching complete. {matched} crew notified.", "notified": matched}


async def _run_ai_match(job: dict) -> int:
    """Background task: AI match crew for job, send notifications."""
    try:
        from utils.ai_utils import ai_match_crew_for_job
        crew = await db.users.find(
            {"role": "crew", "is_active": True, "availability": True},
            {"_id": 0, "id": 1, "name": 1, "trade": 1, "skills": 1, "rating": 1, "location": 1, "lat": 1, "lng": 1,
             "availability": 1, "is_active": 1, "jobs_completed": 1, "transportation": 1}
        ).to_list(300)

        if not crew:
            return 0

        matches = await ai_match_crew_for_job(job, crew)
        notified = 0
        for m in matches:
            if not m.get("notify"):
                continue
            crew_id = m.get("crew_id")
            if not crew_id:
                continue
            existing = await db.notifications.find_one({"user_id": crew_id, "job_id": job["id"], "type": {"$in": ["job_match", "job_invite"]}})
            if existing:
                continue
            score_pct = int(m.get("score", 0) * 100)
            await db.notifications.insert_one({
                "id": str(uuid.uuid4()),
                "user_id": crew_id,
                "type": "job_match",
                "title": "New Job Match",
                "message": f'"{job["title"]}" — {score_pct}% match. {m.get("reason", "")}',
                "job_id": job["id"],
                "score": m.get("score", 0),
                "read": False,
                "created_at": now_str()
            })
            notified += 1
        return notified
    except Exception as e:
        logger.error(f"AI match background error: {e}")
        return 0


# ─── Crew Invites ─────────────────────────────────────────────────────────────

@router.post("/{job_id}/invite")
async def invite_crew(job_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """Contractor invites specific crew members to a job."""
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not your job")
    if job["status"] != "open":
        raise HTTPException(status_code=400, detail="Only open jobs with free positions can receive offers")

    body = await request.json()
    crew_ids = body.get("crew_ids", [])
    if not crew_ids or not isinstance(crew_ids, list):
        raise HTTPException(status_code=400, detail="crew_ids array required")
    crew_ids = crew_ids[:10]  # max 10 invites at once

    sent = 0
    for crew_id in crew_ids:
        crew_member = await db.users.find_one({"id": crew_id, "role": "crew", "is_active": True}, {"_id": 0, "id": 1, "name": 1})
        if not crew_member or crew_id in job.get("crew_accepted", []):
            continue
        await db.jobs.update_one({"id": job_id}, {"$addToSet": {"invited_crew": crew_id}})
        existing = await db.notifications.find_one({"user_id": crew_id, "job_id": job_id, "type": "job_invite"})
        if existing:
            continue
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": crew_id,
            "type": "job_invite",
            "title": "You've Been Invited",
            "message": f'{current_user.get("company_name") or current_user["name"]} invited you to "{job["title"]}"',
            "job_id": job_id,
            "read": False,
            "created_at": now_str()
        })
        try:
            from routes.ws_routes import manager
            await manager.send_to_user(crew_id, {"type": "job_invite", "job_id": job_id, "job_title": job["title"]})
        except Exception:
            pass
        sent += 1

    return {"message": f"Invites sent to {sent} crew members", "sent": sent}
