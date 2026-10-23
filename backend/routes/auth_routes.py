import uuid
import random
import string
import secrets
import time
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from database import db
from models import UserCreate, UserLogin, ForgotPasswordRequest, ResetPasswordRequest
from auth import (
    hash_password, verify_password, user_to_response, issue_session, clear_session,
    validate_password_strength, hash_secret, decode_token, revoke_token, bump_token_version,
    extract_token, bearer_scheme, get_current_user, limiter, log_security_event,
)
from utils.email_utils import send_welcome_email, send_password_reset_email
import os
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

PUBLIC_ROLES = ("crew", "contractor")
PRIVILEGED_ROLES = ("admin", "super_admin")
MAX_FAILED_LOGINS = 10
FAILED_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60
RESET_TOKEN_TTL = timedelta(hours=1)
OAUTH_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"


def generate_referral_code(length: int = 8) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def trial_end(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def _grant_initial_token_allowances(user: dict) -> None:
    """Grant the new account's one-time welcome and 14-day trial lots idempotently."""
    from token_actions import active_catalog
    from token_billing import TokenWalletService
    _, catalog = await active_catalog()
    wallet = TokenWalletService(db)
    uid = user["id"]
    welcome_expiry = datetime.fromisoformat(user["token_welcome_expiry_at"])
    trial_expiry = datetime.fromisoformat(user["token_trial_end_at"])
    try:
        amount = int(catalog.token_policy.welcome_tokens)
        if amount > 0:
            await wallet.grant_tokens(
                uid, amount, idempotency_key=f"welcome:{uid}:token-v1",
                reason="One-time starter token grant", source="welcome_grant",
                expires_at=welcome_expiry,
                metadata={"role": user["role"], "policy_version": 1}
            )
            await db.users.update_one({"id": uid}, {"$set": {"token_welcome_granted_at": datetime.now(timezone.utc).isoformat()}})
    except Exception as exc:
        logger.warning("Initial welcome token grant failed for new account: %s", type(exc).__name__)
    try:
        amount = int(catalog.token_policy.trial_crew_tokens if user["role"] == "crew"
                     else catalog.token_policy.trial_contractor_tokens)
        if amount > 0 and trial_expiry > datetime.now(timezone.utc):
            await wallet.grant_tokens(
                uid, amount, idempotency_key=f"trial:{uid}:token-v1",
                reason="14-day Pro trial token allowance", source="trial_grant",
                expires_at=trial_expiry,
                metadata={"role": user["role"], "trial_end_at": user["token_trial_end_at"], "policy_version": 1}
            )
            await db.users.update_one({"id": uid}, {"$set": {"token_trial_granted_at": datetime.now(timezone.utc).isoformat()}})
    except Exception as exc:
        logger.warning("Initial trial token grant failed for new account: %s", type(exc).__name__)


def oauth_signup_role(value) -> str:
    """Validate a role choice for new Google registrations only."""
    if not isinstance(value, str) or value not in PUBLIC_ROLES:
        raise HTTPException(status_code=400, detail="Role must be crew or contractor")
    return value


def session_body(token: str, user: dict) -> dict:
    from auth import session_csrf
    return {"access_token": token, "token_type": "bearer", "user": user_to_response(user), "csrf_token": session_csrf(token)}


async def unique_referral_code() -> str:
    code = generate_referral_code()
    while await db.users.find_one({"referral_code": code}):
        code = generate_referral_code()
    return code


# ─── Login lockout (per account, independent of IP) ─────────────────────────

async def ensure_not_locked(email: str, request: Request):
    doc = await db.login_attempts.find_one({"email": email})
    if doc and doc.get("locked_until", 0) > time.time():
        log_security_event("login_locked", request, email=email)
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")


async def record_failed_login(email: str):
    now = time.time()
    doc = await db.login_attempts.find_one({"email": email})
    if not doc or doc.get("first_at", 0) < now - FAILED_WINDOW_SECONDS:
        await db.login_attempts.update_one(
            {"email": email}, {"$set": {"count": 1, "first_at": now, "locked_until": 0}}, upsert=True
        )
        return
    count = doc.get("count", 0) + 1
    update = {"count": count}
    if count >= MAX_FAILED_LOGINS:
        update["locked_until"] = now + LOCKOUT_SECONDS
    await db.login_attempts.update_one({"email": email}, {"$set": update})


@router.post("/register", status_code=201)
@limiter.limit("5/minute")
async def register(data: UserCreate, request: Request, response: Response):
    if data.role not in PUBLIC_ROLES:
        log_security_event("register_role_rejected", request, role=data.role)
        raise HTTPException(status_code=400, detail="Role must be crew or contractor")
    pw_error = validate_password_strength(data.password)
    if pw_error:
        raise HTTPException(status_code=400, detail=pw_error)
    email = data.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    token_trial_end_at = (now_dt + timedelta(days=14)).isoformat()
    token_welcome_expiry_at = (now_dt + timedelta(days=30)).isoformat()

    user_doc = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(data.password),
        "role": data.role,
        "name": data.name,
        "phone": data.phone,
        "is_active": True,
        "is_verified": False,
        "agreed_to_terms": data.agreed_to_terms,
        "created_at": now,
        "trial_start_date": now,
        "trial_end_date": token_trial_end_at,
        "trial_type": data.role,
        "subscription_status": "trial",
        "subscription_plan": None,
        "subscription_end": None,
        "points": 50,
        "referral_code": await unique_referral_code(),
        "referred_by": None,
        "bio": "",
        "trade": "",
        "skills": [],
        "profile_photo": None,
        "availability": True,
        "transportation": None,
        "location": None,
        "rating": 0.0,
        "rating_count": 0,
        "jobs_completed": 0,
        "company_name": data.company_name or "",
        "logo": None,
        "hide_location": False,
        "favorite_crew": [],
        "auth_provider": "email",
        "token_version": 0,
        "billing_mode": "tokens_v1",
        "token_plan": "trial_crew" if data.role == "crew" else "trial_contractor",
        "billing_plan": "trial_crew" if data.role == "crew" else "trial_contractor",
        "billing_subscription_status": "trial",
        "token_trial_started_at": now,
        "token_trial_end_at": token_trial_end_at,
        "token_free_period_start": token_trial_end_at,
        "token_welcome_expiry_at": token_welcome_expiry_at,
    }

    referral_code = (data.referral_code_used or "").strip().upper()
    if referral_code:
        referrer = await db.users.find_one({"referral_code": referral_code, "is_active": True})
        if referrer and referrer.get("email") != user_doc["email"]:
            user_doc["referred_by"] = referrer["id"]
            await db.users.update_one({"id": referrer["id"]}, {"$inc": {"points": 100}})
            await db.referrals.insert_one({
                "id": str(uuid.uuid4()),
                "referrer_id": referrer["id"],
                "referred_id": user_doc["id"],
                "points_awarded": 100,
                "created_at": now
            })

    await db.users.insert_one(user_doc)
    await _grant_initial_token_allowances(user_doc)
    log_security_event("register", request, user=user_doc["id"], role=user_doc["role"])
    token = issue_session(response, user_doc)
    await send_welcome_email(data.name, email, data.role)
    return session_body(token, user_doc)


@router.post("/login")
@limiter.limit("20/minute")
async def login(data: UserLogin, request: Request, response: Response):
    email = data.email.lower()
    await ensure_not_locked(email, request)
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user or not verify_password(data.password, user.get("password_hash")):
        await record_failed_login(email)
        log_security_event("login_failed", request, email=email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.get("is_active", True):
        log_security_event("login_suspended", request, user=user["id"])
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    await db.login_attempts.delete_one({"email": email})
    log_security_event("login_success", request, user=user["id"], role=user["role"])
    token = issue_session(response, user)
    return session_body(token, user)


@router.get("/csrf")
async def get_csrf(request: Request):
    """Returns the caller's CSRF cookie value for frontends served from another (trusted) origin,
    where document.cookie can't read the API host's cookie. CORS keeps untrusted origins from reading it."""
    from auth import CSRF_COOKIE, SESSION_COOKIE, session_csrf
    return {"csrf_token": session_csrf(request.cookies.get(SESSION_COOKIE, "")) or request.cookies.get(CSRF_COOKIE)}


@router.post("/logout")
async def logout(request: Request, response: Response, credentials=Depends(bearer_scheme)):
    token, _ = extract_token(request, credentials)
    payload = decode_token(token) if token else None
    if payload:
        await revoke_token(payload)
        log_security_event("logout", request, user=payload["sub"])
    clear_session(response)
    return {"message": "Logged out"}


@router.post("/logout-all")
async def logout_all(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    await bump_token_version(current_user["id"])
    log_security_event("logout_all", request, user=current_user["id"])
    clear_session(response)
    return {"message": "Signed out of all devices"}


# ─── Forgot / Reset Password ─────────────────────────────────────────────────

GENERIC_RESET_MSG = {"message": "If that email is registered, a reset link has been sent."}


@router.post("/forgot-password")
@limiter.limit("5/15minutes")
async def forgot_password(data: ForgotPasswordRequest, request: Request):
    email = data.email.lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    log_security_event("password_reset_requested", request, found=bool(user))
    if not user or not user.get("is_active", True):
        return GENERIC_RESET_MSG

    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await db.password_resets.delete_many({"user_id": user["id"]})
    await db.password_resets.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "token_hash": hash_secret(raw_token),
        "expires_at": now + RESET_TOKEN_TTL,
        "used": False,
        "created_at": now,
    })
    await send_password_reset_email(user["email"], user.get("name", "User"), raw_token)
    return GENERIC_RESET_MSG


@router.post("/reset-password")
@limiter.limit("10/15minutes")
async def reset_password(data: ResetPasswordRequest, request: Request):
    pw_error = validate_password_strength(data.new_password)
    if pw_error:
        raise HTTPException(status_code=400, detail=pw_error)

    now = datetime.now(timezone.utc)
    reset_doc = await db.password_resets.find_one_and_update(
        {"token_hash": hash_secret(data.token), "used": False, "expires_at": {"$gt": now}},
        {"$set": {"used": True, "used_at": now}},
    )
    if not reset_doc:
        log_security_event("password_reset_invalid", request)
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user = await db.users.find_one({"id": reset_doc["user_id"]}, {"_id": 0, "id": 1, "email": 1})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_password(data.new_password)}})
    await bump_token_version(user["id"])
    await db.login_attempts.delete_one({"email": user["email"]})
    await db.password_resets.delete_many({"user_id": user["id"], "used": False})
    log_security_event("password_reset_success", request, user=user["id"])
    return {"message": "Password reset successfully. You can now log in."}


# ─── Google OAuth ─────────────────────────────────────────────────────────────

async def fetch_oauth_profile(session_id: str, request: Request) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(OAUTH_SESSION_URL, headers={"X-Session-ID": session_id})
    except httpx.RequestError as e:
        logger.error(f"OAuth session exchange failed: {type(e).__name__}")
        raise HTTPException(status_code=503, detail="Sign-in service unavailable. Try again.")
    if res.status_code != 200:
        log_security_event("oauth_failed", request, upstream_status=res.status_code)
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.")
    try:
        data = res.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("email"), str) or "@" not in data["email"]:
        log_security_event("oauth_failed", request, reason="bad_payload")
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.")
    return data


@router.post("/google/callback")
@limiter.limit("10/minute")
async def google_oauth_callback(request: Request, response: Response):
    try:
        body = await request.json()
    except ValueError:
        body = None
    session_id = body.get("session_id") if isinstance(body, dict) else None
    requested_role = oauth_signup_role(body.get("role", "crew") if isinstance(body, dict) else "crew")
    if not isinstance(session_id, str) or not 8 <= len(session_id) <= 512:
        raise HTTPException(status_code=400, detail="session_id required")

    oauth_data = await fetch_oauth_profile(session_id, request)
    email = oauth_data["email"].strip().lower()
    name = str(oauth_data.get("name") or email.split("@")[0])[:120]
    picture = oauth_data.get("picture") if isinstance(oauth_data.get("picture"), str) else ""

    user = await db.users.find_one({"email": email}, {"_id": 0})
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    token_trial_end_at = (now_dt + timedelta(days=14)).isoformat()
    token_welcome_expiry_at = (now_dt + timedelta(days=30)).isoformat()

    if not user:
        user = {
            "id": str(uuid.uuid4()),
            "email": email,
            "password_hash": None,
            "role": requested_role,
            "name": name,
            "phone": None,
            "is_active": True,
            "is_verified": True,
            "agreed_to_terms": True,
            "created_at": now,
            "trial_start_date": now,
            "trial_end_date": token_trial_end_at,
            "subscription_status": "trial",
            "subscription_plan": None,
            "subscription_end": None,
            "points": 50,
            "referral_code": await unique_referral_code(),
            "referred_by": None,
            "bio": "",
            "trade": "",
            "skills": [],
            "profile_photo": picture,
            "availability": True,
            "transportation": None,
            "location": None,
            "rating": 0.0,
            "rating_count": 0,
            "jobs_completed": 0,
            "company_name": "",
            "logo": None,
            "hide_location": False,
            "favorite_crew": [],
            "auth_provider": "google",
            "token_version": 0,
            "billing_mode": "tokens_v1",
            "token_plan": "trial_crew" if requested_role == "crew" else "trial_contractor",
            "billing_plan": "trial_crew" if requested_role == "crew" else "trial_contractor",
            "billing_subscription_status": "trial",
            "token_trial_started_at": now,
            "token_trial_end_at": token_trial_end_at,
            "token_free_period_start": token_trial_end_at,
            "token_welcome_expiry_at": token_welcome_expiry_at,
        }
        await db.users.insert_one(user)
        await _grant_initial_token_allowances(user)
        log_security_event("register", request, user=user["id"], role=requested_role, provider="google")
        await send_welcome_email(name, email, requested_role)
    else:
        if user.get("role") in PRIVILEGED_ROLES:
            log_security_event("oauth_privileged_blocked", request, user=user["id"])
            raise HTTPException(status_code=403, detail="Admin accounts must sign in with email and password")
        if not user.get("is_active", True):
            log_security_event("login_suspended", request, user=user["id"], provider="google")
            raise HTTPException(status_code=403, detail="Account suspended")
        if picture and not user.get("profile_photo"):
            await db.users.update_one({"id": user["id"]}, {"$set": {"profile_photo": picture}})
            user["profile_photo"] = picture

    log_security_event("login_success", request, user=user["id"], role=user["role"], provider="google")
    token = issue_session(response, user)
    return session_body(token, user)


@router.post("/convert-to-contractor")
async def convert_to_contractor(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    """Crew member upgrades to a contractor account; history and ratings are kept."""
    if current_user["role"] != "crew":
        raise HTTPException(status_code=400, detail="Only crew accounts can convert to contractor")
    body = await request.json()
    company = str((body or {}).get("company_name", "")).strip()
    if not 2 <= len(company) <= 120:
        raise HTTPException(status_code=400, detail="Company name is required")
    active_jobs = await db.jobs.count_documents({"crew_accepted": current_user["id"], "status": {"$in": ["open", "fulfilled", "in_progress", "completed_pending_review"]}})
    if active_jobs:
        raise HTTPException(status_code=400, detail="Finish or leave your active jobs before converting")
    await db.users.update_one({"id": current_user["id"], "role": "crew"}, {
        "$set": {"role": "contractor", "company_name": company, "converted_from_crew_at": datetime.now(timezone.utc).isoformat()},
        "$inc": {"token_version": 1}})
    user = await db.users.find_one({"id": current_user["id"]}, {"_id": 0})
    log_security_event("role_converted", request, user=user["id"], role="contractor")
    token = issue_session(response, user)
    return session_body(token, user)



@router.post("/convert-to-crew")
async def convert_to_crew(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    """Switch a contractor to a crew-only account after active contractor jobs are closed."""
    if current_user.get("role") != "contractor":
        raise HTTPException(status_code=400, detail="Only contractor accounts can convert to crew")
    active_jobs = await db.jobs.count_documents({
        "contractor_id": current_user["id"],
        "status": {"$in": ["pending_payment", "open", "fulfilled", "suspended", "in_progress", "completed_pending_review"]},
        "archived_at": {"$exists": False},
    })
    if active_jobs:
        raise HTTPException(status_code=400, detail="Close or cancel and archive your active jobs before converting")
    now = datetime.now(timezone.utc).isoformat()
    result = await db.users.update_one(
        {"id": current_user["id"], "role": "contractor"},
        {"$set": {"role": "crew", "company_name": "", "converted_from_contractor_at": now},
         "$inc": {"token_version": 1}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Account role changed. Refresh and try again.")
    user = await db.users.find_one({"id": current_user["id"]}, {"_id": 0})
    log_security_event("role_converted", request, user=user["id"], role="crew")
    token = issue_session(response, user)
    return session_body(token, user)
