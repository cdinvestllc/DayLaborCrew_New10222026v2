from fastapi import FastAPI, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
import uuid
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from auth import hash_password, limiter, validate_password_strength, log_security_event  # noqa: E402
from database import db  # noqa: E402
from utils.storage import init_storage, migrate_legacy_uploads  # noqa: E402

for _var in ("MONGO_URL", "DB_NAME", "JWT_SECRET_KEY", "JWT_EXPIRE_HOURS", "CORS_ORIGINS", "EMERGENT_LLM_KEY", "TRUSTED_PROXY_HOPS"):
    if not os.environ.get(_var):
        raise RuntimeError(f"Missing required environment variable: {_var}")

app = FastAPI(title="TheDayLaborers API", version="2.0.0", redirect_slashes=False)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    log_security_event("rate_limited", request, path=request.url.path)
    return JSONResponse(status_code=429, content={"detail": "Too many requests. Please slow down and try again."})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    path = request.url.path
    if path.startswith("/api/auth") or path.startswith("/api/users/me"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/api/") and not path.startswith("/api/public/files") and not path.startswith("/api/uploads"):
        # Live data (jobs, notifications...) must never be served from a browser/HTTP cache
        response.headers.setdefault("Cache-Control", "no-store")
    return response

api_router = APIRouter(prefix="/api")

# Import routers
from routes.auth_routes import router as auth_router
from routes.job_routes import router as job_router
from routes.user_routes import router as user_router
from routes.admin_routes import router as admin_router
from routes.payment_routes import router as payment_router
from routes.ws_routes import router as ws_router
from routes.public_routes import router as public_router
from routes.advertiser_routes import router as advertiser_router
from routes.token_billing_routes import router as token_billing_router
from routes.token_payment_routes import router as token_payment_router

# Register routes
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(job_router, prefix="/jobs", tags=["jobs"])
api_router.include_router(user_router, prefix="/users", tags=["users"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(payment_router, prefix="/payments", tags=["payments"])
api_router.include_router(public_router, prefix="/public", tags=["public"])
api_router.include_router(advertiser_router, prefix="/advertisers", tags=["advertisers"])
api_router.include_router(token_billing_router, prefix="/billing", tags=["billing"])
api_router.include_router(token_payment_router, prefix="/billing", tags=["billing-payments"])
api_router.include_router(ws_router)

@api_router.get("/")
async def root():
    return {"message": "TheDayLaborers API v2", "status": "operational"}

app.include_router(api_router)

# Static files
uploads_dir = ROOT_DIR / "uploads"
uploads_dir.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
app.mount("/api/uploads", StaticFiles(directory=str(uploads_dir)), name="api_uploads")  # legacy image paths

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# CORS: explicit trusted origins only; a wildcard is refused (fail closed)
_origins = [o.strip().strip('"').rstrip("/") for o in os.environ["CORS_ORIGINS"].split(",") if o.strip()]
if "*" in _origins:
    logger.error("CORS_ORIGINS='*' is not allowed with credentialed requests; cross-origin access disabled")
    _origins = []
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)


@app.middleware("http")
async def log_rejected_origins(request: Request, call_next):
    """Outermost: records cross-origin callers that CORS_ORIGINS does not trust (helps diagnose login failures)."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin not in _origins and request.method == "OPTIONS":
        logger.warning(f"CORS preflight rejected for untrusted origin: {origin} path={request.url.path}")
    return await call_next(request)


async def bootstrap_privileged(role: str, email_var: str, password_var: str, name: str):
    """Creates the first admin account only from explicit, strong env credentials."""
    from datetime import datetime, timezone, timedelta
    import random, string
    if await db.users.find_one({"role": role}):
        return
    email = (os.environ.get(email_var) or "").strip().lower()
    password = os.environ.get(password_var) or ""
    if not email or not password:
        logger.warning(f"No {role} exists and {email_var}/{password_var} are not set; skipping bootstrap")
        return
    if validate_password_strength(password):
        logger.error(f"{password_var} does not meet password policy; {role} not created")
        return
    if await db.users.find_one({"email": email}):
        logger.error(f"{email_var} belongs to an existing non-{role} account; {role} not created")
        return
    now = datetime.now(timezone.utc)
    far = (now + timedelta(days=36500)).isoformat()
    await db.users.insert_one({
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(password),
        "role": role,
        "name": name,
        "phone": None,
        "is_active": True,
        "is_verified": True,
        "created_at": now.isoformat(),
        "trial_start_date": now.isoformat(),
        "trial_end_date": far,
        "subscription_status": "active",
        "subscription_plan": "lifetime",
        "subscription_end": far,
        "points": 0,
        "referral_code": ''.join(random.choices(string.ascii_uppercase + string.digits, k=8)),
        "referred_by": None,
        "bio": "", "trade": "", "skills": [], "profile_photo": None,
        "availability": True, "location": None,
        "rating": 0.0, "rating_count": 0, "jobs_completed": 0,
        "company_name": "", "logo": None, "hide_location": False, "favorite_crew": [],
        "auth_provider": "email", "token_version": 0,
    })
    logger.info(f"{role} bootstrapped from environment")


@app.on_event("startup")
async def startup_event():
    # Indexes
    try:
        await db.users.create_index("email", unique=True)
        await db.users.create_index("referral_code", sparse=True)
        await db.users.create_index("role")
        await db.jobs.create_index("status")
        await db.jobs.create_index("contractor_id")
        await db.jobs.create_index("created_at")
        await db.admin_activity_log.create_index("timestamp")
        await db.admin_activity_log.create_index("admin_id")
        logger.info("Database indexes created")
    except Exception as e:
        logger.warning(f"Index creation: {e}")

    # Security collections: TTL cleanup + single-use lookups
    try:
        await db.revoked_tokens.create_index("jti", unique=True)
        await db.revoked_tokens.create_index("expires_at", expireAfterSeconds=0)
        await db.password_resets.delete_many({"token": {"$exists": True}})
        await db.password_resets.create_index("token_hash", unique=True, sparse=True)
        await db.password_resets.create_index("expires_at", expireAfterSeconds=0)
        await db.ws_tickets.create_index("ticket_hash", unique=True)
        await db.ws_tickets.create_index("expires_at", expireAfterSeconds=0)
        await db.login_attempts.create_index("email", unique=True)
        await db.files.create_index("storage_path")
        await db.ratings.create_index([("job_id", 1), ("rater_id", 1), ("rated_id", 1)], unique=True)
        await db.referrals.create_index("referred_id", unique=True)
        await db.contact_unlocks.create_index([("buyer_id", 1), ("target_id", 1)], unique=True)
        await db.guest_jobs.create_index("session_id")
        await db.payment_transactions.create_index("session_id")
    except Exception as e:
        logger.warning(f"Security index creation: {e}")

    try:
        init_storage()
        logger.info("Object storage initialized")
        moved = await migrate_legacy_uploads(uploads_dir)
        if moved:
            logger.info(f"Migrated legacy uploads for {moved} users")
    except Exception as e:
        logger.error(f"Object storage init failed: {type(e).__name__}")

    from datetime import datetime, timezone, timedelta
    try:
        from routes.payment_routes import migrate_legacy_plans
        await migrate_legacy_plans()
        async for c in db.users.find({"is_verified_contractor": True, "verified_expires_at": {"$exists": False}}, {"_id": 0, "id": 1, "verified_at": 1}):
            try:
                start = datetime.fromisoformat(c.get("verified_at") or "")
                start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            except ValueError:
                start = datetime.now(timezone.utc)
            await db.users.update_one({"id": c["id"]}, {"$set": {"verified_expires_at": (start + timedelta(days=365)).isoformat()}})
    except Exception as e:
        logger.error(f"Data migration failed: {e}")
    await bootstrap_privileged("super_admin", "SUPER_ADMIN_EMAIL", "SUPER_ADMIN_PASSWORD", "Super Administrator")
    await bootstrap_privileged("admin", "ADMIN_EMAIL", "ADMIN_PASSWORD", "Platform Admin")

    if os.environ.get("SEED_DEMO_ACCOUNTS", "").lower() == "true":
        try:
            from seed_demo import seed_demo_accounts
            await seed_demo_accounts()
        except Exception as e:
            logger.error(f"Demo seed failed: {e}")

    # Init default settings
    existing_settings = await db.settings.find_one({})
    if not existing_settings:
        await db.settings.insert_one({
            "daily_price": 4.99,
            "weekly_price": 24.99,
            "monthly_price": 79.99,
            "trial_days": 30,
            "job_visibility_hours": 12,
            "site_name": "TheDayLaborers",
            "site_tagline": "A Blue Collar ME Company",
            "site_logo": None,
            "site_favicon": None,
            "emergency_job_price": 2.99,
        })
        logger.info("Default settings created")
    else:
        await db.settings.update_one({"emergency_job_price": {"$exists": False}}, {"$set": {"emergency_job_price": 2.99}})

    # Init default trades
    existing_trades = await db.trades.count_documents({})
    if existing_trades == 0:
        default_trades = [
            "General Labor", "Carpentry", "Electrical", "Plumbing", "HVAC",
            "Painting", "Roofing", "Landscaping", "Concrete/Masonry", "Welding",
            "Flooring", "Drywall", "Demolition", "Moving/Hauling", "Cleaning",
            "Fencing", "Tile Work", "Insulation", "Glass/Windows", "Other"
        ]
        now = datetime.now(timezone.utc).isoformat()
        trade_docs = [
            {"id": str(uuid.uuid4()), "name": t, "is_active": True, "created_at": now}
            for t in default_trades
        ]
        await db.trades.insert_many(trade_docs)
        logger.info(f"Default trades created: {len(trade_docs)}")

    # Init CMS content
    cms_pages = ["terms", "privacy", "guidelines"]
    for page in cms_pages:
        existing = await db.cms.find_one({"page": page})
        if not existing:
            await db.cms.insert_one({
                "page": page,
                "title": {"terms": "Terms & Conditions", "privacy": "Privacy Policy", "guidelines": "Community Guidelines"}[page],
                "content": f"Default {page} content. Edit this in the Admin CMS.",
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "published": True
            })

    logger.info("TheDayLaborers API v2 started successfully")


@app.on_event("shutdown")
async def shutdown_db_client():
    from database import client
    client.close()
