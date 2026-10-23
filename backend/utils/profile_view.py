"""Field-whitelisted user views; contact details only for authorized viewers."""
from datetime import datetime, timezone
from database import db
from utils.geocoding import valid_coords

PUBLIC_FIELDS = (
    "id", "name", "role", "bio", "trade", "skills", "rating", "rating_count", "jobs_completed",
    "availability", "portfolio", "portfolio_videos", "company_name", "created_at", "profile_photo", "logo",
    "transportation", "certifications", "is_verified_contractor", "verified_contractor_at", "is_insured",
)
STAFF_ROLES = ("admin", "super_admin")


def user_coords(user: dict):
    """Returns (lat, lng) from location or legacy top-level fields, or None."""
    loc = user.get("location") if isinstance(user.get("location"), dict) else {}
    return valid_coords(loc.get("lat"), loc.get("lng")) or valid_coords(user.get("lat"), user.get("lng"))


def mask_phone(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"•••-•••-{digits[-4:]}" if len(digits) >= 4 else None


def mask_email(email):
    if not email or "@" not in email:
        return None
    name, domain = email.split("@", 1)
    return f"{name[:1]}•••@{domain}"


async def has_contact_access(viewer: dict | None, target: dict) -> bool:
    """Self, staff, a shared accepted job, or a paid contact unlock."""
    if not viewer:
        return False
    if viewer["id"] == target["id"] or viewer.get("role") in STAFF_ROLES:
        return True
    pair = [
        {"contractor_id": viewer["id"], "crew_accepted": target["id"]},
        {"contractor_id": target["id"], "crew_accepted": viewer["id"]},
    ]
    if await db.jobs.find_one({"$or": pair}, {"_id": 1}):
        return True
    return bool(await db.contact_unlocks.find_one({"buyer_id": viewer["id"], "target_id": target["id"]}, {"_id": 1}))


async def build_user_view(target: dict, viewer: dict | None, include_coords: bool = False) -> dict:
    view = {k: target.get(k) for k in PUBLIC_FIELDS if k in target}
    view.setdefault("portfolio", [])
    # Filter expired Pro videos on every public/self view, even if the daily cleanup
    # scheduler is delayed or has temporarily failed.
    now = datetime.now(timezone.utc)
    videos = []
    for video in view.get("portfolio_videos") or []:
        expires = video.get("expires_at")
        try:
            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00")) if isinstance(expires, str) else expires
            if expiry is None:
                continue
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry > now:
                videos.append({
                    key: video[key] for key in ("id", "url", "duration_seconds", "created_at", "expires_at")
                    if key in video
                })
        except (TypeError, ValueError):
            continue
    view["portfolio_videos"] = videos
    privacy = target.get("privacy") or {}
    is_self = bool(viewer) and viewer["id"] == target["id"]
    hide_location = (privacy.get("hide_location") or target.get("hide_location")) and not is_self

    loc = target.get("location") if isinstance(target.get("location"), dict) else {}
    coords = user_coords(target)
    view["location"] = {"city": loc.get("city") or "", "state": loc.get("state") or ""}
    if coords and include_coords and not hide_location:
        view["location"].update({"lat": coords[0], "lng": coords[1]})

    access = await has_contact_access(viewer, target)
    view["contact_access"] = access
    if access:
        view["phone"] = None if privacy.get("hide_phone") and not is_self else target.get("phone")
        view["email"] = None if privacy.get("hide_email") and not is_self else target.get("email")
        if not hide_location:
            view["address"] = target.get("address") or loc.get("address")
    else:
        view["phone_masked"] = mask_phone(target.get("phone"))
        view["email_masked"] = mask_email(target.get("email"))
    return view


async def reputation(user: dict) -> dict:
    """Trust metrics from real job/rating history; components without data are left out, never invented."""
    uid = user["id"]
    as_crew = user.get("role") == "crew"
    match = {"crew_accepted": uid} if as_crew else {"contractor_id": uid}
    jobs = await db.jobs.find(match, {"_id": 0, "status": 1, "contractor_id": 1}).to_list(2000)
    completed = [j for j in jobs if j["status"] == "completed"]
    cancelled = [j for j in jobs if j["status"] == "cancelled"]
    finished = len(completed) + len(cancelled) + sum(1 for j in jobs if j["status"] == "closed")
    rating, count = float(user.get("rating") or 0), int(user.get("rating_count") or 0)
    repeat = 0
    if as_crew:
        per = {}
        for j in completed:
            per[j["contractor_id"]] = per.get(j["contractor_id"], 0) + 1
        repeat = sum(1 for n in per.values() if n >= 2)
    completion_rate = len(completed) / finished if finished else None
    parts = []
    if count:
        parts.append((40, rating / 5))
    if completion_rate is not None:
        parts.append((30, completion_rate))
    if completed:
        parts.append((20, min(len(completed), 20) / 20))
    if as_crew and completed:
        parts.append((10, min(repeat, 3) / 3))
    score = round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts)) if parts else None
    badges = []
    if count >= 5 and rating >= 4.5:
        badges.append("top_rated")
    if finished >= 5 and (completion_rate or 0) >= 0.9:
        badges.append("reliable")
    if repeat >= 1:
        badges.append("repeat_hire")
    if len(completed) >= 25:
        badges.append("experienced")
    return {
        "trust_score": score,
        "completed_jobs": len(completed),
        "completion_rate": round(completion_rate * 100) if completion_rate is not None else None,
        "cancellation_rate": round(100 * len(cancelled) / len(jobs)) if (jobs and not as_crew) else None,
        "repeat_hires": repeat if as_crew else None,
        "response_time": None,
        "badges": badges,
    }
