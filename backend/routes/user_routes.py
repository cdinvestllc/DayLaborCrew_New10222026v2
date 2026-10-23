import os
import random
import uuid
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Query, Request
from database import db
from auth import get_current_user, get_optional_user, user_to_response
from utils.storage import save_image
from models import ProfileUpdate, LocationUpdate
from typing import Optional
from datetime import datetime, timezone
import logging
from auth import limiter
from utils.geocoding import valid_coords, distance_between, autocomplete_address
from utils.profile_view import build_user_view, user_coords

router = APIRouter()
logger = logging.getLogger(__name__)



@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return user_to_response(current_user)


@router.put("/profile")
async def update_profile(data: ProfileUpdate, current_user: dict = Depends(get_current_user)):
    update = {k: v for k, v in data.model_dump().items() if v is not None}
    update.pop("location", None)
    if data.is_insured is not None and current_user.get("role") != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can update insurance status")
    if data.transportation is not None and data.transportation not in ("bike", "car", "suv_van", "truck", "bus", "rideshare", "other", ""):
        raise HTTPException(status_code=400, detail="Invalid transportation type")
    if data.portfolio is not None:
        own_prefix = f"/api/public/files/thedaylaborers/portfolio/{current_user['id']}/"
        current = set(current_user.get("portfolio") or [])
        if len(data.portfolio) > 8 or any(u not in current and not u.startswith(own_prefix) for u in data.portfolio):
            raise HTTPException(status_code=400, detail="Portfolio may only contain up to 8 of your uploaded images")
    if data.certifications is not None:
        update["certifications"] = [c.model_dump() for c in data.certifications]
    if data.lat is not None or data.lng is not None:
        coords = valid_coords(data.lat, data.lng)
        if not coords:
            raise HTTPException(status_code=400, detail="Invalid latitude/longitude")
        prev = current_user.get("location") if isinstance(current_user.get("location"), dict) else {}
        update["location"] = {
            **prev,
            "lat": coords[0],
            "lng": coords[1],
            "address": data.address or "",
            "city": data.city if data.city is not None else prev.get("city", ""),
            "state": data.state if data.state is not None else prev.get("state", ""),
        }
    update.pop("city", None)
    update.pop("state", None)
    if update:
        await db.users.update_one({"id": current_user["id"]}, {"$set": update})
    updated = await db.users.find_one({"id": current_user["id"]}, {"_id": 0})
    # Award a Free-tier qualifying activity only for an actual change to meaningful
    # profile fields, and at most once per account-anchored 30-day period.
    meaningful_fields = {
        "bio", "trade", "skills", "certifications", "availability", "portfolio",
        "profile_photo", "transportation", "is_insured", "location"
    }
    changed_meaningful = [
        field for field in meaningful_fields
        if field in update and update.get(field) != current_user.get(field)
        and update.get(field) not in (None, "", [], {})
    ]
    if changed_meaningful:
        from token_actions import record_qualifying_activity
        from datetime import datetime, timezone
        created_at = current_user.get("created_at")
        try:
            anchor = datetime.fromisoformat(created_at.replace("Z", "+00:00")) if isinstance(created_at, str) else datetime.now(timezone.utc)
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            anchor = datetime.now(timezone.utc)
        elapsed_days = max(0, (datetime.now(timezone.utc) - anchor).total_seconds() // 86400)
        period_index = int(elapsed_days // 30)
        await record_qualifying_activity(
            current_user["id"], "meaningful_profile_update",
            f"profileupdate:{current_user['id']}:{period_index}",
            metadata={"changed_fields": sorted(changed_meaningful), "period_index": period_index}
        )
    return user_to_response(updated)


@router.post("/location")
async def update_location(data: LocationUpdate, current_user: dict = Depends(get_current_user)):
    if not valid_coords(data.lat, data.lng):
        raise HTTPException(status_code=400, detail="Invalid latitude/longitude")
    prev = current_user.get("location") if isinstance(current_user.get("location"), dict) else {}
    location = {**prev, "lat": data.lat, "lng": data.lng, "city": data.city or prev.get("city", "")}
    await db.users.update_one(
        {"id": current_user["id"]},
        {"$set": {"location": location}}
    )
    # Update WS location
    try:
        from routes.ws_routes import manager
        manager.update_user_location(current_user["id"], data.lat, data.lng)
    except Exception:
        pass
    return {"message": "Location updated"}


@router.post("/upload-photo")
async def upload_photo(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    field = "logo" if current_user["role"] == "contractor" else "profile_photo"
    photo_url = await save_image(file, current_user["id"], "logos" if field == "logo" else "profile_photos")
    await db.users.update_one({"id": current_user["id"]}, {"$set": {field: photo_url}})
    return {"url": photo_url}


@router.post("/upload-portfolio")
async def upload_portfolio(files: list[UploadFile] = File(...), current_user: dict = Depends(get_current_user)):
    """Upload multiple portfolio images (max 8 total)."""
    user = await db.users.find_one({"id": current_user["id"]}, {"_id": 0, "portfolio": 1})
    existing = user.get("portfolio", []) if user else []
    if len(existing) + len(files) > 8:
        raise HTTPException(status_code=400, detail="Maximum 8 portfolio images allowed")

    urls = [await save_image(file, current_user["id"], "portfolio") for file in files]
    updated = existing + urls
    await db.users.update_one({"id": current_user["id"]}, {"$set": {"portfolio": updated}})
    return {"urls": urls, "portfolio": updated}


@router.post("/upload-job-images")
async def upload_job_images(files: list[UploadFile] = File(...), current_user: dict = Depends(get_current_user)):
    """Contractors attach up to 6 photos to a job before publishing."""
    if current_user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can attach job images")
    if len(files) > 6:
        raise HTTPException(status_code=400, detail="Maximum 6 job images")
    return {"urls": [await save_image(f, current_user["id"], "job_images") for f in files]}


@router.get("/geocode/search")
@limiter.limit("30/minute")
async def geocode_search(request: Request, q: str = Query(..., min_length=3, max_length=200),
                         current_user: dict = Depends(get_current_user)):
    """Address autocomplete through the server-side geocoder (no client keys)."""
    return await autocomplete_address(q)


def _build_crew_search_query(trade=None, name=None, skill=None, transportation=None,
                            state=None, min_rating=None, available_only=True):
    import re
    query = {"role": "crew", "is_active": True}
    if trade:
        query["trade"] = {"$regex": re.escape(trade), "$options": "i"}
    if name:
        query["name"] = {"$regex": re.escape(name), "$options": "i"}
    if skill:
        query["skills"] = {"$regex": re.escape(skill), "$options": "i"}
    if transportation:
        query["transportation"] = transportation
    if state:
        query["location.state"] = {"$regex": re.escape(state), "$options": "i"}
    if min_rating is not None:
        query["rating"] = {"$gte": min_rating}
    if available_only:
        query["availability"] = True
    return query


@router.get("/crew")
async def search_crew(
    trade: Optional[str] = None,
    name: Optional[str] = None,
    skill: Optional[str] = None,
    transportation: Optional[str] = None,
    state: Optional[str] = None,
    min_rating: Optional[float] = Query(None, ge=0, le=5),
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius: Optional[float] = 50,
    available_only: bool = True,
    current_user: dict = Depends(get_current_user)
):
    query = _build_crew_search_query(
        trade=trade, name=name, skill=skill, transportation=transportation,
        state=state, min_rating=min_rating, available_only=available_only)
    crew = await db.users.find(query, {"_id": 0, "password_hash": 0}).to_list(200)
    origin = valid_coords(lat, lng)
    radius = min(max(radius or 50, 1), 250)
    results = []
    for member in crew:
        dist = distance_between(origin, user_coords(member))
        if origin and (dist is None or dist > radius):
            continue
        view = await build_user_view(member, current_user, include_coords=True)
        view["distance_miles"] = round(dist, 1) if dist is not None else None
        boost_expiry = member.get("profile_boost_expires_at")
        try:
            expiry = datetime.fromisoformat(str(boost_expiry).replace("Z", "+00:00")) if boost_expiry else None
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expiry = None
        view["_token_featured_rank"] = bool(
            member.get("profile_featured") and expiry and expiry > datetime.now(timezone.utc)
        )
        results.append(view)

    # Token-paid profile boosts rank first; the existing small randomized spotlight
    # continues among search results but can never displace an active paid boost.
    count = min(2, max(1, len(results) // 3)) if len(results) > 1 else 0
    featured_ids = set(random.sample([member["id"] for member in results], count)) if count else set()
    for member in results:
        member["search_featured"] = member["id"] in featured_ids
    results.sort(key=lambda member: (
        bool(member.get("_token_featured_rank")), bool(member.get("search_featured"))
    ), reverse=True)
    for member in results:
        member.pop("_token_featured_rank", None)
    return results


@router.get("/crew/{user_id}")
async def get_crew_member(user_id: str, current_user: dict = Depends(get_current_user)):
    user = await db.users.find_one({"id": user_id, "role": "crew", "is_active": True}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Crew member not found")
    ratings = await db.ratings.find({"rated_id": user_id, "hidden": {"$ne": True}}, {"_id": 0}).sort("created_at", -1).to_list(5)
    return {**await build_user_view(user, current_user, include_coords=True), "recent_ratings": ratings}


@router.post("/favorites/{user_id}")
async def add_favorite(user_id: str, current_user: dict = Depends(get_current_user)):
    target = await db.users.find_one({"id": user_id, "role": "crew"})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id not in current_user.get("favorite_crew", []):
        await db.users.update_one(
            {"id": current_user["id"]},
            {"$push": {"favorite_crew": user_id}}
        )
    return {"message": "Added to favorites"}


@router.delete("/favorites/{user_id}")
async def remove_favorite(user_id: str, current_user: dict = Depends(get_current_user)):
    await db.users.update_one(
        {"id": current_user["id"]},
        {"$pull": {"favorite_crew": user_id}}
    )
    return {"message": "Removed from favorites"}


@router.get("/favorites")
async def get_favorites(current_user: dict = Depends(get_current_user)):
    fav_ids = current_user.get("favorite_crew", [])
    users = await db.users.find({"id": {"$in": fav_ids}, "is_active": True}, {"_id": 0, "password_hash": 0}).to_list(100)
    return [await build_user_view(u, current_user, include_coords=True) for u in users]


@router.get("/referral/info")
async def referral_info(current_user: dict = Depends(get_current_user)):
    referrals = await db.referrals.find({"referrer_id": current_user["id"]}, {"_id": 0}).to_list(100)
    return {
        "referral_code": current_user["referral_code"],
        "points": current_user["points"],
        "total_referrals": len(referrals),
        "referrals": referrals
    }


@router.post("/redeem-points")
async def redeem_points(points: int, current_user: dict = Depends(get_current_user)):
    if current_user["points"] < points:
        raise HTTPException(status_code=400, detail="Insufficient points")
    if points < 500:
        raise HTTPException(status_code=400, detail="Minimum 500 points to redeem")

    days = points // 500  # 500 points = 1 day subscription
    from datetime import datetime, timezone, timedelta
    sub_end = current_user.get("subscription_end")
    if sub_end:
        try:
            base = datetime.fromisoformat(sub_end)
        except Exception:
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)

    new_end = (base + timedelta(days=days)).isoformat()
    result = await db.users.update_one(
        {"id": current_user["id"], "points": {"$gte": points}},
        {
            "$inc": {"points": -points},
            "$set": {"subscription_end": new_end, "subscription_status": "active"}
        }
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Insufficient points")
    return {"message": f"Redeemed {points} points for {days} days", "new_subscription_end": new_end}


@router.get("/trial-status")
async def trial_status(current_user: dict = Depends(get_current_user)):
    from datetime import datetime, timezone
    trial_end = current_user.get("trial_end_date")
    if not trial_end:
        return {"is_trial": False, "days_remaining": 0}
    try:
        end = datetime.fromisoformat(trial_end)
        remaining = (end - datetime.now(timezone.utc)).days
        return {
            "is_trial": current_user.get("subscription_status") == "trial",
            "days_remaining": max(0, remaining),
            "trial_end": trial_end
        }
    except Exception:
        return {"is_trial": False, "days_remaining": 0}


@router.post("/availability/toggle")
async def toggle_availability(current_user: dict = Depends(get_current_user)):
    """Crew can manually toggle availability even when logged out (persists in DB)."""
    new_status = not current_user.get("availability", True)
    await db.users.update_one({"id": current_user["id"]}, {"$set": {"availability": new_status}})
    return {"availability": new_status, "message": f"You are now {'available' if new_status else 'unavailable'} for work"}


@router.put("/transportation")
async def update_transportation(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    transportation = body.get("transportation")
    valid = ["bike", "car", "suv_van", "truck", "bus", "rideshare", "other"]
    if transportation not in valid:
        raise HTTPException(status_code=400, detail=f"Valid options: {', '.join(valid)}")
    await db.users.update_one({"id": current_user["id"]}, {"$set": {"transportation": transportation}})
    return {"transportation": transportation}


# ─── User Messaging ────────────────────────────────────────────────────────────

@router.get("/messages")
async def get_my_messages(current_user: dict = Depends(get_current_user)):
    """Get all message threads for the current user."""
    messages = await db.messages.find(
        {"$or": [{"sender_id": current_user["id"]}, {"recipient_id": current_user["id"]}]},
        {"_id": 0}
    ).sort("created_at", -1).to_list(50)
    return messages


@router.get("/messages/{thread_id}")
async def get_message_thread(thread_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific message thread."""
    msg = await db.messages.find_one({"thread_id": thread_id}, {"_id": 0})
    if not msg or current_user["id"] not in (msg.get("sender_id"), msg.get("recipient_id")):
        raise HTTPException(status_code=404, detail="Thread not found")
    # Mark as read
    if msg.get("recipient_id") == current_user["id"] and not msg.get("read"):
        await db.messages.update_one({"thread_id": thread_id}, {"$set": {"read": True}})
    return msg


@router.post("/messages/{thread_id}/reply")
async def reply_to_message(thread_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """User replies to a message thread."""
    from datetime import datetime, timezone
    body = await request.json()
    reply_text = body.get("body", "").strip()
    if not reply_text:
        raise HTTPException(status_code=400, detail="Reply body required")

    msg = await db.messages.find_one({"thread_id": thread_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Ensure current user is part of this thread
    if current_user["id"] not in [msg["sender_id"], msg["recipient_id"]]:
        raise HTTPException(status_code=403, detail="Not part of this thread")

    now = datetime.now(timezone.utc).isoformat()
    reply = {
        "id": str(uuid.uuid4()),
        "sender_id": current_user["id"],
        "sender_name": current_user.get("name", "User"),
        "sender_role": current_user.get("role", "crew"),
        "body": reply_text,
        "created_at": now
    }
    await db.messages.update_one({"thread_id": thread_id}, {"$push": {"replies": reply}})

    # Notify the other party
    other_id = msg["recipient_id"] if msg["sender_id"] == current_user["id"] else msg["sender_id"]
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": other_id,
        "type": "admin_message",
        "title": f"Reply: {msg['subject']}",
        "message": reply_text[:100],
        "thread_id": thread_id,
        "read": False,
        "created_at": now
    })
    return {"message": "Reply sent"}



# ─── Delete / Archive message ─────────────────────────────────────────────────

@router.delete("/messages/{thread_id}")
async def delete_message_thread(thread_id: str, current_user: dict = Depends(get_current_user)):
    """Archive (soft-delete) a message thread."""
    msg = await db.messages.find_one({"thread_id": thread_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Thread not found")
    if current_user["id"] not in [msg["sender_id"], msg["recipient_id"]]:
        raise HTTPException(status_code=403, detail="Not part of this thread")

    now = datetime.now(timezone.utc).isoformat()
    # Move to archived collection
    archived = {**{k: v for k, v in msg.items() if k != "_id"}, "archived_at": now, "archived_by": current_user["id"]}
    await db.archived_messages.insert_one(archived)
    await db.messages.delete_one({"thread_id": thread_id})
    return {"message": "Thread archived"}


# ─── Delete notification ──────────────────────────────────────────────────────

@router.delete("/notifications/{notif_id}")
async def delete_notification(notif_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.notifications.delete_one({"id": notif_id, "user_id": current_user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"message": "Deleted"}


@router.delete("/notifications")
async def delete_all_notifications(current_user: dict = Depends(get_current_user)):
    await db.notifications.delete_many({"user_id": current_user["id"]})
    return {"message": "All notifications cleared"}


# ─── Public Profile ───────────────────────────────────────────────────────────

@router.get("/public/{user_id}")
async def get_public_profile(user_id: str, viewer: Optional[dict] = Depends(get_optional_user)):
    """Public profile view. Privacy controls applied."""
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=404, detail="Profile not found")

    from utils.profile_view import reputation
    view = await build_user_view(user, viewer, include_coords=False)
    view["reputation"] = await reputation(user)
    view["recent_ratings"] = await db.ratings.find({"rated_id": user_id, "hidden": {"$ne": True}},
                                                   {"_id": 0, "stars": 1, "review": 1, "created_at": 1}).sort("created_at", -1).to_list(5)
    return view


# ─── Update privacy settings ─────────────────────────────────────────────────

@router.put("/privacy")
async def update_privacy(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    valid = {"hide_phone", "hide_email", "hide_location"}
    privacy = {k: bool(v) for k, v in body.items() if k in valid}
    await db.users.update_one({"id": current_user["id"]}, {"$set": {"privacy": privacy}})
    return {"privacy": privacy}


# ─── App Preferences/Settings ─────────────────────────────────────────────────

@router.get("/preferences")
async def get_preferences(current_user: dict = Depends(get_current_user)):
    prefs = current_user.get("preferences", {})
    defaults = {
        "soundAlerts": True,
        "vibrationAlerts": True,
        "browserNotifications": False,
        "pushNotifications": False,
        "notificationsBlocked": False,
        "analyticsPrivacy": False,
        "notify": {
            "jobCompleted": True,
            "jobAccepted": True,
            "jobAcceptedContractor": True,
            "jobDeclined": False,
            "jobDeclinedContractor": False,
        }
    }
    return {**defaults, **prefs}


@router.put("/preferences")
async def update_preferences(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    existing = current_user.get("preferences", {})
    merged = {**existing, **body}
    await db.users.update_one({"id": current_user["id"]}, {"$set": {"preferences": merged}})
    return {"preferences": merged}


# ─── Push Notification Subscribe ─────────────────────────────────────────────

@router.post("/push/subscribe")
async def subscribe_push(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint required")
    await db.push_subscriptions.replace_one(
        {"user_id": current_user["id"], "endpoint": endpoint},
        {"user_id": current_user["id"], "endpoint": endpoint, "keys": body.get("keys", {}), "created_at": datetime.now(timezone.utc).isoformat()},
        upsert=True
    )
    return {"message": "Push subscription saved"}
