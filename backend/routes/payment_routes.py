import os
import uuid
import httpx
import base64
import asyncio
import logging
import math
import stripe as stripe_sdk
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fastapi import APIRouter, HTTPException, Request, Depends
from database import db
from auth import get_current_user, limiter
from models import CheckoutRequest
from utils.email_utils import send_subscription_email
try:
    from emergentintegrations.payments.stripe.checkout import (
        StripeCheckout, CheckoutSessionRequest, CheckoutSessionResponse, CheckoutStatusResponse
    )
    STRIPE_CHECKOUT_AVAILABLE = True
except ImportError:
    # The vendor checkout adapter isn't available from public PyPI. Keep non-payment
    # routes/tests importable, but fail closed if a checkout endpoint is invoked.
    STRIPE_CHECKOUT_AVAILABLE = False

    class StripeCheckout:
        def __init__(self, *args, **kwargs):
            raise HTTPException(
                status_code=503,
                detail="Stripe checkout integration is unavailable; contact the deployment administrator."
            )

    CheckoutSessionRequest = dict
    CheckoutSessionResponse = dict
    CheckoutStatusResponse = dict

router = APIRouter()
logger = logging.getLogger(__name__)

STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "sk_test_emergent").strip()
STRIPE_MODE = os.environ.get("STRIPE_MODE", "test").strip().lower()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox").strip().lower()
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()
if PAYPAL_MODE not in {"sandbox", "live"}:
    logging.getLogger(__name__).error("Invalid PAYPAL_MODE; using sandbox until it is explicitly configured")
    PAYPAL_MODE = "sandbox"
PAYPAL_BASE_URL = "https://api-m.paypal.com" if PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"


def _validate_stripe_api_key(api_key: str, mode: str) -> str:
    """Fail closed if a live Stripe key is present while the application is in test mode."""
    prefixes = {"test": ("sk_test_", "rk_test_"), "live": ("sk_live_", "rk_live_")}
    if mode not in prefixes or not api_key.startswith(prefixes[mode]):
        raise HTTPException(status_code=503, detail="Stripe is not configured for the selected payment mode")
    return api_key


def _stripe_api_key() -> str:
    return _validate_stripe_api_key(STRIPE_API_KEY, STRIPE_MODE)


def _transaction_amount_matches(tx: dict, amount: object, currency: object, *, minor_units: bool = False) -> bool:
    """Compare provider amounts using decimal currency arithmetic, never float tolerances."""
    try:
        actual = Decimal(str(amount))
        expected = Decimal(str(tx.get("amount")))
        if not actual.is_finite() or not expected.is_finite():
            return False
        if minor_units:
            actual /= Decimal("100")
        cent = Decimal("0.01")
        actual = actual.quantize(cent, rounding=ROUND_HALF_UP)
        expected = expected.quantize(cent, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return False
    expected_currency = str(tx.get("currency") or "usd").lower()
    return str(currency or "").lower() == expected_currency and actual == expected


def _stripe_amount(amount) -> float:
    """Prepare an exact cent value for a checkout adapter that uses int(amount * 100).

    Binary floats such as 19.99 can multiply to 1998.999..., so move only as far
    as needed for truncation to yield the recorded number of cents.
    """
    cents = int(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    value = cents / 100
    while int(value * 100) < cents:
        value = math.nextafter(value, math.inf)
    if int(value * 100) != cents:
        raise HTTPException(status_code=500, detail="Payment amount could not be prepared")
    return value


def _payment_matches_transaction(
    tx: dict,
    provider: str,
    amount: object,
    currency: object,
    *,
    minor_units: bool = False,
) -> bool:
    """Require the recorded processor and the exact expected amount/currency."""
    return tx.get("payment_method") == provider and _transaction_amount_matches(
        tx, amount, currency, minor_units=minor_units
    )


def _checkout_status_matches_transaction(tx: dict | None, status, provider: str) -> bool:
    """Validate a provider's checkout-status response against our local transaction."""
    if not tx or tx.get("payment_method") != provider:
        return False
    if getattr(status, "payment_status", None) != "paid":
        return False
    return _transaction_amount_matches(
        tx,
        getattr(status, "amount_total", None),
        getattr(status, "currency", None),
        minor_units=True,
    )


async def _status_response(session_id: str, status) -> dict:
    """Never report provider-confirmed payment as fulfilled until our transaction is paid."""
    tx = await db.payment_transactions.find_one(
        {"session_id": session_id}, {"_id": 0, "payment_status": 1}
    )
    fulfilled = bool(tx) and tx.get("payment_status") == "paid"
    payment_status = getattr(status, "payment_status", "unknown")
    if payment_status == "paid" and not fulfilled:
        payment_status = "verification_failed"
    return {
        "status": getattr(status, "status", "unknown"),
        "payment_status": payment_status,
        "fulfilled": fulfilled,
    }


def _verify_stripe_event(payload: bytes, signature: str, secret: str):
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")
    if not signature:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature")
    try:
        return stripe_sdk.Webhook.construct_event(payload, signature, secret)
    except Exception:
        # Never expose payloads, signatures, secrets, or provider exception details.
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature") from None


def _paypal_verification_payload(headers, event: dict, webhook_id: str) -> dict:
    if not webhook_id:
        raise HTTPException(status_code=503, detail="PayPal webhook is not configured")
    header_names = {
        "auth_algo": "paypal-auth-algo",
        "cert_url": "paypal-cert-url",
        "transmission_id": "paypal-transmission-id",
        "transmission_sig": "paypal-transmission-sig",
        "transmission_time": "paypal-transmission-time",
    }
    values = {key: headers.get(header) for key, header in header_names.items()}
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        raise HTTPException(status_code=400, detail="Invalid PayPal webhook headers")
    return {**values, "webhook_id": webhook_id, "webhook_event": event}


async def _verify_paypal_webhook(request: Request, event: dict) -> None:
    payload = _paypal_verification_payload(request.headers, event, PAYPAL_WEBHOOK_ID)
    access_token = await get_paypal_token()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{PAYPAL_BASE_URL}/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError as exc:
        logger.warning("PayPal webhook verification unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="PayPal webhook verification is temporarily unavailable") from None
    if result.get("verification_status") != "SUCCESS":
        raise HTTPException(status_code=400, detail="Invalid PayPal webhook signature")

# Subscription plans (prices defined server-side for security)
PLANS = {
    "crew_monthly": {"amount": 19.99, "days": 30, "label": "Crew Monthly", "role": "crew"},
    "crew_quarterly": {"amount": 39.99, "days": 90, "label": "Crew Quarterly", "role": "crew"},
    "crew_annual": {"amount": 99.00, "days": 365, "label": "Crew Annual", "role": "crew"},
    "contractor_basic": {"amount": 19.99, "days": 30, "label": "Contractor Basic", "role": "contractor", "job_posts": 4},
    "contractor_pro": {"amount": 39.99, "days": 30, "label": "Contractor Pro", "role": "contractor", "job_posts": 10},
    "contractor_business": {"amount": 59.00, "days": 30, "label": "Contractor Business", "role": "contractor", "job_posts": 20},
}
LEGACY_PLAN_MAP = {"crew": "crew_monthly", "contractor": "contractor_basic"}
TRIAL_JOB_POSTS = 4
CONTACT_UNLOCK_PRICE = 2.99
GUEST_JOB_PRICE = 9.99
STAFF_ROLES = ("admin", "super_admin")


def _parse_dt(value):
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def subscription_state(user: dict) -> dict:
    """Server-side source of truth for access: active paid plan or unexpired trial."""
    now = datetime.now(timezone.utc)
    if user.get("role") in STAFF_ROLES or user.get("subscription_plan") == "lifetime":
        return {"active": True, "plan": user.get("subscription_plan") or "staff", "period_start": None, "trial": False}
    end = _parse_dt(user.get("subscription_end"))
    plan = user.get("subscription_plan")
    if user.get("subscription_status") == "active" and end and end > now:
        if plan not in PLANS:
            plan = LEGACY_PLAN_MAP.get(user.get("role"), "crew_monthly")
        days = PLANS[plan]["days"]
        return {"active": True, "plan": plan, "period_start": (end - timedelta(days=days)).isoformat()[:10], "trial": False}
    trial_end = _parse_dt(user.get("trial_end_date"))
    if trial_end and trial_end > now and user.get("subscription_status") in (None, "trial"):
        return {"active": True, "plan": "trial", "period_start": "trial", "trial": True}
    return {"active": False, "plan": plan, "period_start": None, "trial": False}


def require_active_subscription(user: dict):
    if not subscription_state(user)["active"]:
        raise HTTPException(status_code=402, detail="An active subscription is required. Please choose a plan.")


def job_post_quota(user: dict) -> tuple[int, str]:
    state = subscription_state(user)
    if not state["active"]:
        raise HTTPException(status_code=402, detail="An active contractor plan is required to post jobs.")
    if state["plan"] == "trial":
        return TRIAL_JOB_POSTS, "trial"
    if state["plan"] == "lifetime" or state["plan"] == "staff":
        return 10**6, "unlimited"
    return PLANS.get(state["plan"], PLANS["contractor_basic"]).get("job_posts", 4), state["period_start"]


async def consume_job_post_credit(user: dict):
    """Atomically uses one job post from the current billing period."""
    quota, period = job_post_quota(user)
    uid = user["id"]
    res = await db.users.update_one({"id": uid, "job_post_period": period, "job_posts_used": {"$lt": quota}},
                                    {"$inc": {"job_posts_used": 1}})
    if res.modified_count:
        return
    res = await db.users.update_one({"id": uid, "job_post_period": {"$ne": period}},
                                    {"$set": {"job_post_period": period, "job_posts_used": 1}})
    if res.modified_count:
        return
    raise HTTPException(status_code=402, detail=f"You've used all {quota} job posts for this billing period. Upgrade your plan to post more.")


async def refund_job_post_credit(user: dict):
    await db.users.update_one({"id": user["id"], "job_posts_used": {"$gt": 0}}, {"$inc": {"job_posts_used": -1}})


async def migrate_legacy_plans():
    """Moves legacy daily/weekly/monthly subscribers onto the new plan names (end dates preserved)."""
    for role, plan in LEGACY_PLAN_MAP.items():
        await db.users.update_many({"role": role, "subscription_plan": {"$in": ["daily", "weekly", "monthly"]}},
                                   {"$set": {"subscription_plan": plan, "legacy_plan_migrated": True}})

# Default verified contractor fee (admin can override in settings)
DEFAULT_VERIFIED_FEE = 39.99


async def get_paypal_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise HTTPException(status_code=503, detail="PayPal is not configured")
    creds = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{PAYPAL_BASE_URL}/v1/oauth2/token",
                headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        logger.error("PayPal token request failed")
        raise HTTPException(status_code=502, detail="PayPal is temporarily unavailable")
    return resp.json()["access_token"]


async def get_owned_tx(session_id: str, user: dict) -> dict | None:
    """Payment transactions may only be read/finalised by the user who created them."""
    tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if tx and tx.get("user_id") != user["id"] and user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=404, detail="Payment not found")
    return tx


def now_str():
    return datetime.now(timezone.utc).isoformat()


async def resolve_plan_price(plan: str) -> dict:
    """Get plan with DB-overridden prices."""
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    info = {**PLANS[plan]}
    settings = await db.settings.find_one({}, {"_id": 0})
    if settings:
        info["amount"] = settings.get(f"{plan}_price", info["amount"])
    return info


async def apply_coupon(code: str, plan: str, amount: float) -> tuple[float, dict | None]:
    """Validate coupon and return (discounted_amount, coupon_doc). Raises on invalid."""
    coupon = await db.coupons.find_one({"code": code.upper()})
    if not coupon:
        raise HTTPException(status_code=400, detail="Invalid coupon code")
    if coupon.get("usage_count", 0) >= coupon.get("usage_limit", 1):
        raise HTTPException(status_code=400, detail="Coupon usage limit reached")
    if coupon.get("expires_at"):
        try:
            exp = datetime.fromisoformat(coupon["expires_at"])
            if exp < datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail="Coupon has expired")
        except ValueError:
            pass
    if coupon.get("plan_restriction") and coupon["plan_restriction"] != plan:
        raise HTTPException(status_code=400, detail=f"Coupon only valid for {coupon['plan_restriction']} plan")
    discount = float(coupon.get("discount_percent", 0))
    discounted = round(amount * (1 - discount / 100), 2)
    return max(discounted, 0.50), coupon  # min $0.50


@router.get("/coupon/{code}")
async def validate_coupon(code: str, plan: str = "crew_monthly", current_user: dict = Depends(get_current_user)):
    """Validate coupon and return discount info."""
    coupon = await db.coupons.find_one({"code": code.upper()}, {"_id": 0, "password_hash": 0})
    if not coupon:
        raise HTTPException(status_code=404, detail="Invalid coupon")
    if coupon.get("usage_count", 0) >= coupon.get("usage_limit", 1):
        raise HTTPException(status_code=400, detail="Coupon usage limit reached")
    if coupon.get("expires_at"):
        try:
            if datetime.fromisoformat(coupon["expires_at"]) < datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail="Coupon expired")
        except ValueError:
            pass
    plan_info = await resolve_plan_price(plan)
    discounted = round(plan_info["amount"] * (1 - float(coupon["discount_percent"]) / 100), 2)
    return {
        "valid": True,
        "code": coupon["code"],
        "discount_percent": coupon["discount_percent"],
        "original_price": plan_info["amount"],
        "final_price": max(discounted, 0.50),
        "uses_remaining": coupon["usage_limit"] - coupon.get("usage_count", 0)
    }


async def update_subscription(
    user_id: str,
    plan: str,
    days: int,
    payment_method: str,
    amount: float,
    coupon_code: str = None,
    payment_session_id: str | None = None,
):
    """Apply a subscription payment once, with optimistic concurrency and retry-safe coupon usage.

    The payment-session marker is written atomically with the entitlement update. If a worker
    crashes after this write but before the transaction is marked paid, a provider retry can
    safely finish the transaction without extending the subscription twice.
    """
    subscription_applied = False
    sub_end = None

    for _ in range(8):
        user = await db.users.find_one({"id": user_id}, {"_id": 0})
        if not user:
            raise RuntimeError("Paid subscription references a missing user")

        applied_sessions = user.get("subscription_payment_sessions") or []
        if payment_session_id and payment_session_id in applied_sessions:
            sub_end = user.get("subscription_end")
            break

        now = datetime.now(timezone.utc)
        current_end = _parse_dt(user.get("subscription_end"))
        base = current_end if (
            user.get("subscription_plan") == plan and current_end and current_end > now
        ) else now
        sub_end = (base + timedelta(days=days)).isoformat()
        update = {
            "subscription_status": "active",
            "subscription_plan": plan,
            "subscription_end": sub_end,
        }
        if user.get("subscription_plan") != plan:
            update.update({"job_post_period": None, "job_posts_used": 0})

        # Compare-and-set the old end date so two different paid sessions cannot overwrite
        # each other's renewals. The session marker makes each individual payment idempotent.
        query = {"id": user_id}
        if "subscription_end" in user:
            query["subscription_end"] = user["subscription_end"]
        else:
            query["subscription_end"] = {"$exists": False}
        update_doc = {"$set": update}
        if payment_session_id:
            query["subscription_payment_sessions"] = {"$ne": payment_session_id}
            update_doc["$addToSet"] = {"subscription_payment_sessions": payment_session_id}

        result = await db.users.update_one(query, update_doc)
        if result.modified_count == 1:
            subscription_applied = True
            break
    else:
        raise RuntimeError("Subscription update remained contended; payment can be retried")

    if coupon_code:
        if payment_session_id:
            # The usage increment and redemption marker live on the same coupon document,
            # so an ambiguous network error cannot cause the same session to count twice.
            await db.coupons.update_one(
                {"code": coupon_code, "applied_payment_sessions": {"$ne": payment_session_id}},
                {
                    "$inc": {"usage_count": 1},
                    "$addToSet": {"applied_payment_sessions": payment_session_id},
                },
            )
        else:
            await db.coupons.update_one({"code": coupon_code}, {"$inc": {"usage_count": 1}})

    # Mail is a secondary side effect. A mail-provider outage must not strand a paid
    # transaction in a retry loop after its entitlement has already been granted.
    if subscription_applied:
        user = await db.users.find_one({"id": user_id}, {"_id": 0})
        if user:
            try:
                await send_subscription_email(user["email"], user["name"], plan, sub_end)
            except Exception as exc:
                logger.warning("Subscription confirmation email failed: %s", type(exc).__name__)
    return sub_end


@router.get("/plans")
async def get_plans(role: str = None):
    plans = {k: await resolve_plan_price(k) for k in PLANS}
    if role in ("crew", "contractor"):
        plans = {k: v for k, v in plans.items() if v["role"] == role}
    return plans


async def _validate_plan_for_user(plan: str, user: dict):
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if PLANS[plan]["role"] != user.get("role"):
        raise HTTPException(status_code=400, detail="This plan is not available for your account type")


# ─── Stripe ──────────────────────────────────────────────────────────────────

@router.post("/stripe/create-session")
async def stripe_create_session(data: CheckoutRequest, current_user: dict = Depends(get_current_user)):
    await _validate_plan_for_user(data.plan, current_user)

    plan_info = await resolve_plan_price(data.plan)
    final_amount = plan_info["amount"]
    coupon_code = None

    if data.coupon_code:
        final_amount, coupon_doc = await apply_coupon(data.coupon_code, data.plan, final_amount)
        coupon_code = data.coupon_code.upper()

    origin = _origin(None, {"origin_url": data.origin_url})
    success_url = f"{origin}/subscription?session_id={{CHECKOUT_SESSION_ID}}&method=stripe"
    cancel_url = f"{origin}/subscription"

    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{origin}/api/payments/stripe/webhook")
    req = CheckoutSessionRequest(
        amount=_stripe_amount(final_amount),
        currency="usd",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"user_id": current_user["id"], "plan": data.plan, "payment_method": "stripe", "coupon": coupon_code or ""}
    )
    session = await stripe.create_checkout_session(req)

    tx = {
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "session_id": session.session_id,
        "amount": float(final_amount),
        "original_amount": float(plan_info["amount"]),
        "coupon_code": coupon_code,
        "currency": "usd",
        "plan": data.plan,
        "payment_method": "stripe",
        "payment_status": "pending",
        "created_at": now_str()
    }
    await db.payment_transactions.insert_one(tx)
    return {"url": session.url, "session_id": session.session_id, "final_amount": final_amount}


@router.get("/stripe/status/{session_id}")
async def stripe_payment_status(session_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{str(request.base_url)}api/payments/stripe/webhook")

    tx = await get_owned_tx(session_id, current_user)
    if not tx or tx.get("payment_method") not in (None, "stripe"):
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"status": "complete", "payment_status": "paid", "already_processed": True, "fulfilled": True}
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        raise HTTPException(status_code=409, detail="This payment can no longer be fulfilled")

    status = await stripe.get_checkout_status(session_id)
    if status.metadata.get("user_id") not in (None, tx.get("user_id")):
        raise HTTPException(status_code=404, detail="Payment not found")

    if status.payment_status == "paid":
        if tx.get("plan") not in PLANS:
            raise HTTPException(status_code=409, detail="This payment does not match a subscription transaction")
        if not _checkout_status_matches_transaction(tx, status, "stripe"):
            logger.error("Stripe status amount/currency did not match its local transaction")
            raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")

        await fulfill_subscription_tx(session_id, expected_provider="stripe")
        updated_tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        if not updated_tx or updated_tx.get("payment_status") != "paid":
            raise HTTPException(status_code=409, detail="Payment is confirmed but fulfillment is not complete; retry status shortly")
        return {"status": "complete", "payment_status": "paid", "fulfilled": True}

    return await _status_response(session_id, status)


async def fulfill_subscription_tx(session_id: str, expected_provider: str | None = None) -> bool:
    """Apply a verified subscription payment, then mark it paid only after entitlement succeeds.

    Failures during entitlement writes leave the transaction retryable. The user-level payment
    marker prevents retries and concurrent webhook/poll requests from extending the same plan twice.
    """
    tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if not tx:
        raise RuntimeError("Subscription transaction was not found")
    if tx.get("plan") not in PLANS:
        logger.warning("Ignoring subscription fulfillment for an unsupported transaction type")
        return False
    if expected_provider and tx.get("payment_method") not in (None, expected_provider):
        logger.error("Subscription fulfillment provider did not match its recorded transaction")
        raise HTTPException(status_code=409, detail="Payment provider does not match the recorded transaction")
    if tx.get("payment_status") == "paid":
        return False
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        return False

    await update_subscription(
        tx["user_id"],
        tx["plan"],
        PLANS[tx["plan"]]["days"],
        tx.get("payment_method", expected_provider or "stripe"),
        tx.get("amount", 0),
        tx.get("coupon_code"),
        payment_session_id=session_id,
    )

    result = await db.payment_transactions.update_one(
        {
            "session_id": session_id,
            "payment_status": {"$nin": ["paid", "cancelled", "canceled", "refunded"]},
        },
        {"$set": {"payment_status": "paid", "updated_at": now_str(), "fulfillment_completed_at": now_str()}},
    )
    if result.modified_count == 1:
        return True

    # Another webhook/polling request may have completed it first. Treat that as success,
    # but do not overwrite or revive a cancelled/refunded transaction.
    current = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if current and current.get("payment_status") == "paid":
        return False
    if current and current.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        return False
    raise RuntimeError("Subscription transaction could not be finalized; provider may retry")


async def _fulfill_boost_tx(session_id: str) -> bool:
    """Activate the recorded job before marking its payment fulfilled, so retries can recover failures."""
    tx = await db.payment_transactions.find_one(
        {"session_id": session_id, "plan": "boost"}, {"_id": 0})
    if not tx or tx.get("payment_status") == "paid":
        return False
    job_id = tx.get("job_id")
    if not job_id:
        logger.error("Paid boost transaction has no job reference")
        raise RuntimeError("Boost transaction is missing its job reference")
    owner_id = tx.get("job_owner_id") or tx["user_id"]
    job = await db.jobs.find_one({"id": job_id, "contractor_id": owner_id}, {"_id": 0, "id": 1})
    if not job:
        logger.error("Paid boost transaction could not find its contractor-owned job")
        raise RuntimeError("Boost job could not be confirmed")
    boost_until = (datetime.now(timezone.utc) + timedelta(days=BOOST_DURATION_DAYS)).isoformat()
    await db.jobs.update_one(
        {"id": job_id, "contractor_id": owner_id},
        {"$set": {"is_featured": True, "boost_expires_at": boost_until}})
    await db.payment_transactions.update_one(
        {"session_id": session_id, "payment_status": {"$ne": "paid"}},
        {"$set": {"payment_status": "paid", "updated_at": now_str()}})
    return True


async def _fulfill_verified_contractor_tx(session_id: str) -> bool:
    paid_tx = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "plan": "verified_contractor", "payment_status": {"$ne": "paid"}},
        {"$set": {"payment_status": "paid", "updated_at": now_str()}})
    if not paid_tx:
        return False
    user_id = paid_tx["user_id"]
    target = await db.users.find_one({"id": user_id}, {"_id": 0, "verified_expires_at": 1}) or {}
    now = datetime.now(timezone.utc)
    current = _parse_dt(target.get("verified_expires_at"))
    new_expiry = (max(current, now) if current else now) + timedelta(days=365)
    await db.users.update_one(
        {"id": user_id},
        {"$set": {"is_verified_contractor": True, "verified_at": now_str(),
                  "verified_expires_at": new_expiry.isoformat(), "verification_expired": False}})
    try:
        from utils.email_utils import send_verified_contractor_email
        paid_user = await db.users.find_one(
            {"id": user_id}, {"_id": 0, "email": 1, "name": 1, "company_name": 1})
        if paid_user:
            settings = await db.settings.find_one({}, {"_id": 0})
            fee = float((settings or {}).get("verified_contractor_fee", DEFAULT_VERIFIED_FEE))
            await send_verified_contractor_email(
                email=paid_user["email"], name=paid_user["name"],
                company_name=paid_user.get("company_name", ""), fee=fee)
    except Exception as exc:
        logger.warning("Verified contractor confirmation email failed: %s", type(exc).__name__)
    return True


async def _fulfill_contact_unlock_tx(session_id: str) -> bool:
    """Grant the idempotent unlock before marking payment fulfilled."""
    tx = await db.payment_transactions.find_one(
        {"session_id": session_id, "plan": "contact_unlock"}, {"_id": 0})
    if not tx:
        return False
    await db.contact_unlocks.update_one(
        {"buyer_id": tx["user_id"], "target_id": tx["target_id"]},
        {"$setOnInsert": {"buyer_id": tx["user_id"], "target_id": tx["target_id"],
                          "session_id": session_id, "paid_at": now_str()}},
        upsert=True)
    await db.payment_transactions.update_one(
        {"session_id": session_id, "payment_status": {"$ne": "paid"}},
        {"$set": {"payment_status": "paid", "updated_at": now_str()}})
    return True


async def _refund_cancelled_emergency_payment(session_id: str, session: dict) -> None:
    """Refund a deleted unpaid emergency job if its checkout succeeds after cancellation."""
    payment_intent = session.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    if not payment_intent:
        raise RuntimeError("Cancelled emergency payment is missing its payment intent")
    try:
        refund = await asyncio.to_thread(
            stripe_sdk.Refund.create,
            payment_intent=payment_intent,
            reason="requested_by_customer",
            api_key=_stripe_api_key(),
            idempotency_key=f"deleted-emergency-job-{session_id}",
        )
    except Exception as exc:
        logger.error("Cancelled emergency payment refund failed: %s", type(exc).__name__)
        raise RuntimeError("Cancelled emergency payment refund failed") from None
    await db.payment_transactions.update_one(
        {"session_id": session_id, "payment_status": "cancelled"},
        {"$set": {"payment_status": "refunded", "provider_refund_id": refund.id,
                  "refunded_at": now_str(), "updated_at": now_str()}})


async def _fulfill_stripe_checkout_session(session: dict) -> None:
    session_id = session.get("id")
    if not session_id:
        raise RuntimeError("Stripe checkout session is missing its ID")
    tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if not tx:
        # Transient transaction-write races should cause the provider to retry the webhook.
        raise RuntimeError("Stripe checkout session has no local transaction")
    if tx.get("payment_method") not in (None, "stripe"):
        logger.error("Stripe event did not match its recorded payment provider")
        raise HTTPException(status_code=400, detail="Payment provider does not match the recorded transaction")
    if not _transaction_amount_matches(
        tx, session.get("amount_total"), session.get("currency"), minor_units=True
    ):
        logger.error("Stripe checkout amount/currency did not match its local transaction")
        raise HTTPException(status_code=400, detail="Payment does not match the recorded transaction")
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        if tx.get("payment_status") == "cancelled" and tx.get("plan") == "emergency_job":
            await _refund_cancelled_emergency_payment(session_id, session)
        return
    if tx.get("payment_status") == "paid":
        return
    plan = tx.get("plan")
    if plan in PLANS:
        await fulfill_subscription_tx(session_id, expected_provider="stripe")
    elif plan == "emergency_job":
        fulfilled_job = await fulfill_emergency_tx(session_id)
        if not fulfilled_job:
            # The delete flow may have cancelled the transaction after our first read.
            current_tx = await db.payment_transactions.find_one(
                {"session_id": session_id}, {"_id": 0, "payment_status": 1, "plan": 1})
            if current_tx and current_tx.get("payment_status") == "cancelled":
                await _refund_cancelled_emergency_payment(session_id, session)
    elif plan == "boost":
        await _fulfill_boost_tx(session_id)
    elif plan == "verified_contractor":
        await _fulfill_verified_contractor_tx(session_id)
    elif plan == "contact_unlock":
        await _fulfill_contact_unlock_tx(session_id)
    elif plan == "guest_job":
        await _publish_paid_guest_job(session_id, (session.get("metadata") or {}).get("guest_job_id"))
    else:
        logger.warning("Ignoring Stripe checkout for an unsupported transaction type")


@router.post("/stripe/webhook")
@limiter.limit("60/minute")
async def stripe_webhook(request: Request):
    body = await request.body()
    event = _verify_stripe_event(
        body, request.headers.get("Stripe-Signature", ""), STRIPE_WEBHOOK_SECRET)
    event_type = event.get("type")
    supported = {
        "checkout.session.completed", "checkout.session.async_payment_succeeded",
        "invoice.paid", "invoice.payment_failed", "customer.subscription.updated",\n        "customer.subscription.deleted", "checkout.session.expired",
    }
    if event_type not in supported:
        return {"status": "ignored"}
    obj = (event.get("data", {}).get("object") or {})
    try:
        _stripe_api_key()  # Enforce test/live key-mode consistency for webhook deployments too.
        if event_type in {"invoice.paid", "invoice.payment_failed", "customer.subscription.updated", "customer.subscription.deleted"}:
            from routes.token_payment_routes import handle_stripe_event
            handled = await handle_stripe_event(event)
            return {"status": "ok" if handled else "ignored"}
        metadata = obj.get("metadata") or {}
        if metadata.get("billing_system") == "token_wallet_v1":
            from routes.token_payment_routes import handle_stripe_event
            await handle_stripe_event(event)
            return {"status": "ok"}
        if obj.get("object") != "checkout.session" or obj.get("payment_status") != "paid":
            return {"status": "ignored"}
        await _fulfill_stripe_checkout_session(obj)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Stripe webhook processing failed: %s", type(exc).__name__)
        # A non-2xx response asks Stripe to retry transient fulfillment failures.
        raise HTTPException(status_code=500, detail="Payment event could not be processed; retry delivery") from None
    return {"status": "ok"}


@router.post("/paypal/webhook")
@limiter.limit("60/minute")
async def paypal_webhook(request: Request):
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid PayPal webhook payload") from None
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid PayPal webhook payload")
    await _verify_paypal_webhook(request, event)
    try:
        from routes.token_payment_routes import handle_paypal_event
        if await handle_paypal_event(event):
            return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("PayPal token billing webhook failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Payment event could not be processed; retry delivery") from None

    if event.get("event_type") != "PAYMENT.CAPTURE.COMPLETED":
        return {"status": "ignored"}

    capture = event.get("resource") or {}
    related = ((capture.get("supplementary_data") or {}).get("related_ids") or {})
    order_id = related.get("order_id") or capture.get("order_id")
    if not order_id:
        logger.error("Verified PayPal capture event is missing its order reference")
        raise HTTPException(status_code=400, detail="Payment order reference is missing")
    tx = await db.payment_transactions.find_one({"session_id": order_id}, {"_id": 0})
    if not tx:
        logger.error("Verified PayPal capture has no local transaction")
        raise HTTPException(status_code=500, detail="Payment event could not be matched; retry delivery")
    if tx.get("payment_status") == "paid":
        return {"status": "ok", "already_processed": True}
    amount = capture.get("amount") or {}
    if not _payment_matches_transaction(tx, "paypal", amount.get("value"), amount.get("currency_code")):
        logger.error("PayPal capture did not match its recorded provider, amount, or currency")
        return {"status": "rejected"}
    if tx.get("plan") in PLANS:
        await fulfill_subscription_tx(order_id, expected_provider="paypal")
    elif tx.get("plan") == "boost":
        await _fulfill_boost_tx(order_id)
    else:
        logger.warning("Ignoring PayPal capture for an unsupported transaction type")
    return {"status": "ok"}




# ─── PayPal ──────────────────────────────────────────────────────────────────

@router.post("/paypal/create-order")
async def paypal_create_order(data: CheckoutRequest, current_user: dict = Depends(get_current_user)):
    await _validate_plan_for_user(data.plan, current_user)
    origin = _origin(None, {"origin_url": data.origin_url})

    plan_info = await resolve_plan_price(data.plan)
    final_amount = plan_info["amount"]
    coupon_code = None

    if data.coupon_code:
        final_amount, coupon_doc = await apply_coupon(data.coupon_code, data.plan, final_amount)
        coupon_code = data.coupon_code.upper()

    access_token = await get_paypal_token()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE_URL}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": f"{current_user['id']}_{data.plan}",
                    "description": f"TheDayLaborers {data.plan.title()} Subscription{' (Coupon: ' + coupon_code + ')' if coupon_code else ''}",
                    "amount": {"currency_code": "USD", "value": f"{final_amount:.2f}"}
                }],
                "application_context": {
                    "return_url": f"{origin}/subscription?method=paypal&plan={data.plan}",
                    "cancel_url": f"{origin}/subscription"
                }
            }
        )
        _raise_for_paypal(resp)
        order = resp.json()

    order_id = order["id"]
    tx = {
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "session_id": order_id,
        "amount": float(final_amount),
        "original_amount": float(plan_info["amount"]),
        "coupon_code": coupon_code,
        "currency": "usd",
        "plan": data.plan,
        "payment_method": "paypal",
        "payment_status": "pending",
        "created_at": now_str()
    }
    await db.payment_transactions.insert_one(tx)

    approve_url = next((l["href"] for l in order["links"] if l["rel"] == "approve"), None)
    return {"order_id": order_id, "approve_url": approve_url, "final_amount": final_amount}


@router.post("/paypal/capture/{order_id}")
async def paypal_capture(order_id: str, plan: str, current_user: dict = Depends(get_current_user)):
    tx = await get_owned_tx(order_id, current_user)
    if not tx or tx.get("user_id") != current_user["id"] or tx.get("payment_method") != "paypal":
        raise HTTPException(status_code=404, detail="Payment not found")
    plan = tx.get("plan", plan)
    if plan not in PLANS:
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"status": "COMPLETED", "already_processed": True}
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        raise HTTPException(status_code=409, detail="This payment can no longer be captured")

    access_token = await get_paypal_token()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )
        _raise_for_paypal(resp)
        result = resp.json()

    status = result.get("status")
    purchase_units = result.get("purchase_units") or []
    payments = (purchase_units[0].get("payments") or {}) if purchase_units else {}
    captures = payments.get("captures") or []
    captured = (captures[0].get("amount") or {}) if captures else {}
    if status == "COMPLETED":
        if not _payment_matches_transaction(tx, "paypal", captured.get("value"), captured.get("currency_code")):
            logger.error("PayPal capture amount/currency did not match its local transaction")
            raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")
        await fulfill_subscription_tx(order_id, expected_provider="paypal")

    return {"status": status, "order_id": order_id}


# ─── Subscription Status ─────────────────────────────────────────────────────

@router.get("/subscription/status")
async def subscription_status(current_user: dict = Depends(get_current_user)):
    state = subscription_state(current_user)
    now = datetime.now(timezone.utc)
    end = _parse_dt(current_user.get("subscription_end")) if not state["trial"] else _parse_dt(current_user.get("trial_end_date"))
    status = "trial" if state["trial"] else ("active" if state["active"] else "expired")
    if not state["active"] and current_user.get("subscription_status") in ("active", "trial"):
        await db.users.update_one({"id": current_user["id"]}, {"$set": {"subscription_status": "expired"}})
    result = {
        "status": status,
        "plan": state["plan"],
        "days_remaining": max(0, (end - now).days) if end and state["active"] else 0,
        "subscription_end": end.isoformat() if end else None,
    }
    if current_user.get("role") == "contractor" and state["active"]:
        quota, period = job_post_quota(current_user)
        used = current_user.get("job_posts_used", 0) if current_user.get("job_post_period") == period else 0
        result.update({"job_posts_quota": quota, "job_posts_used": used, "job_posts_remaining": max(0, quota - used)})
    return result


# ─── Featured / Boost Job ─────────────────────────────────────────────────────

BOOST_PRICE = float(os.environ.get("BOOST_JOB_PRICE", "9.99"))
BOOST_DURATION_DAYS = 7


@router.post("/boost/stripe/{job_id}")
async def boost_job_stripe(job_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not your job")

    body = await _json_body(request)
    origin = _origin(request, body)
    success_url = f"{origin}/contractor/dashboard?boost_success=1&job_id={job_id}"
    cancel_url = f"{origin}/contractor/dashboard"

    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{origin}/api/payments/stripe/webhook")
    from emergentintegrations.payments.stripe.checkout import CheckoutSessionRequest
    req = CheckoutSessionRequest(
        amount=_stripe_amount(BOOST_PRICE),
        currency="usd",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"user_id": current_user["id"], "job_id": job_id, "type": "boost", "payment_method": "stripe"}
    )
    session = await stripe.create_checkout_session(req)

    tx = {
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "session_id": session.session_id,
        "job_id": job_id,
        "job_owner_id": job["contractor_id"],
        "amount": BOOST_PRICE,
        "currency": "usd",
        "plan": "boost",
        "payment_method": "stripe",
        "payment_status": "pending",
        "created_at": now_str()
    }
    await db.payment_transactions.insert_one(tx)
    return {"url": session.url, "session_id": session.session_id}


@router.get("/boost/stripe/status/{session_id}")
async def boost_stripe_status(session_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{str(request.base_url)}api/payments/stripe/webhook")
    tx = await get_owned_tx(session_id, current_user)
    if not tx or tx.get("plan") != "boost" or tx.get("payment_method") != "stripe":
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"status": "complete", "payment_status": "paid", "already_processed": True, "fulfilled": True}
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        raise HTTPException(status_code=409, detail="This payment can no longer be fulfilled")
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        raise HTTPException(status_code=409, detail="This payment can no longer be fulfilled")

    status = await stripe.get_checkout_status(session_id)
    metadata = getattr(status, "metadata", {}) or {}
    if metadata.get("user_id") != tx.get("user_id") or metadata.get("type") != "boost":
        raise HTTPException(status_code=404, detail="Payment not found")
    if metadata.get("job_id") != tx.get("job_id"):
        raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")
    if status.payment_status == "paid":
        if not _checkout_status_matches_transaction(tx, status, "stripe"):
            logger.error("Stripe boost status did not match its recorded provider, amount, or currency")
            raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")
        await _fulfill_boost_tx(session_id)
        updated_tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        if not updated_tx or updated_tx.get("payment_status") != "paid":
            raise HTTPException(status_code=409, detail="Payment is confirmed but fulfillment is not complete; retry status shortly")
        return {"status": "complete", "payment_status": "paid", "fulfilled": True}
    return await _status_response(session_id, status)


@router.post("/boost/paypal/{job_id}")
async def boost_job_paypal(job_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["contractor_id"] != current_user["id"] and current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not your job")

    body = await request.json()
    origin = body.get("origin_url", str(request.base_url)).rstrip("/")

    access_token = await get_paypal_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE_URL}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": f"{current_user['id']}_boost_{job_id}",
                    "description": f"Boost Job: {job.get('title', job_id)[:50]}",
                    "amount": {"currency_code": "USD", "value": f"{BOOST_PRICE:.2f}"}
                }],
                "application_context": {
                    "return_url": f"{origin}/contractor/dashboard?boost_success=1&job_id={job_id}&method=paypal",
                    "cancel_url": f"{origin}/contractor/dashboard"
                }
            }
        )
        _raise_for_paypal(resp)
        order = resp.json()

    order_id = order["id"]
    await db.payment_transactions.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "session_id": order_id,
        "job_id": job_id,
        "amount": BOOST_PRICE,
        "currency": "usd",
        "plan": "boost",
        "payment_method": "paypal",
        "payment_status": "pending",
        "created_at": now_str()
    })
    approve_url = next((l["href"] for l in order["links"] if l["rel"] == "approve"), None)
    return {"order_id": order_id, "approve_url": approve_url}


@router.post("/boost/paypal/capture/{order_id}")
async def boost_paypal_capture(order_id: str, job_id: str, current_user: dict = Depends(get_current_user)):
    tx = await get_owned_tx(order_id, current_user)
    if not tx or tx.get("user_id") != current_user["id"] or tx.get("plan") != "boost" or tx.get("payment_method") != "paypal":
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"status": "COMPLETED", "already_processed": True}

    access_token = await get_paypal_token()
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )
        _raise_for_paypal(resp)
        result = resp.json()

    if result.get("status") == "COMPLETED":
        captures = (((result.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [])
        captured = (captures[0] if captures else {}).get("amount") or {}
        if _payment_matches_transaction(tx, "paypal", captured.get("value"), captured.get("currency_code")):
            await _fulfill_boost_tx(order_id)
        else:
            logger.error("PayPal boost capture did not match its recorded provider, amount, or currency")
            raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")
    return {"status": result.get("status"), "order_id": order_id}


# ─── Verified Contractor Fee (Stripe) ─────────────────────────────────────────

@router.post("/verified-contractor/create-session")
async def create_verified_contractor_session(request: Request, current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can purchase verification")
    expires = _parse_dt(current_user.get("verified_expires_at"))
    if current_user.get("is_verified_contractor") and expires and expires - datetime.now(timezone.utc) > timedelta(days=30):
        raise HTTPException(status_code=400, detail="Your verification is active. Renewal opens 30 days before it expires.")

    # Check for existing pending transaction (prevent duplicate sessions)
    existing = await db.payment_transactions.find_one({
        "user_id": current_user["id"],
        "plan": "verified_contractor",
        "payment_status": "pending"
    })

    # Fetch admin-configured fee
    settings = await db.settings.find_one({}, {"_id": 0})
    fee = float((settings or {}).get("verified_contractor_fee", DEFAULT_VERIFIED_FEE))

    body = await _json_body(request)
    origin = _origin(request, body)
    success_url = f"{origin}/contractor/dashboard?verified_session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/contractor/dashboard"

    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{origin}/api/payments/stripe/webhook")
    req = CheckoutSessionRequest(
        amount=_stripe_amount(fee),
        currency="usd",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"user_id": current_user["id"], "type": "verified_contractor", "payment_method": "stripe"}
    )
    session = await stripe.create_checkout_session(req)

    tx = {
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "session_id": session.session_id,
        "amount": float(fee),
        "currency": "usd",
        "plan": "verified_contractor",
        "payment_method": "stripe",
        "payment_status": "pending",
        "created_at": now_str()
    }
    await db.payment_transactions.insert_one(tx)
    return {"url": session.url, "session_id": session.session_id, "fee": fee}


@router.get("/verified-contractor/status/{session_id}")
async def verified_contractor_status(session_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{str(request.base_url)}api/payments/stripe/webhook")
    tx = await get_owned_tx(session_id, current_user)
    if not tx or tx.get("plan") != "verified_contractor" or tx.get("payment_method") != "stripe":
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"status": "complete", "payment_status": "paid", "already_processed": True, "fulfilled": True}
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        raise HTTPException(status_code=409, detail="This payment can no longer be fulfilled")

    status = await stripe.get_checkout_status(session_id)
    metadata = getattr(status, "metadata", {}) or {}
    if metadata.get("user_id") != tx.get("user_id") or metadata.get("type") != "verified_contractor":
        raise HTTPException(status_code=404, detail="Payment not found")
    if status.payment_status == "paid":
        if not _checkout_status_matches_transaction(tx, status, "stripe"):
            logger.error("Stripe verification status did not match its recorded provider, amount, or currency")
            raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")
        await _fulfill_verified_contractor_tx(session_id)
        updated_tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        if updated_tx and updated_tx.get("payment_status") == "paid":
            return {"status": "complete", "payment_status": "paid", "fulfilled": True}
    return await _status_response(session_id, status)


@router.get("/verified-contractor/fee")
async def get_verified_fee():
    """Public endpoint to get the current verification fee."""
    settings = await db.settings.find_one({}, {"_id": 0})
    return {"fee": float((settings or {}).get("verified_contractor_fee", DEFAULT_VERIFIED_FEE))}


# ─── Contact Unlock ($2.99, Stripe) ──────────────────────────────────────────

async def _stripe_checkout(request: Request, origin: str, amount: float, success_url: str, cancel_url: str, metadata: dict):
    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url=f"{origin}/api/payments/stripe/webhook")
    return await stripe.create_checkout_session(CheckoutSessionRequest(
        amount=float(amount), currency="usd", success_url=success_url, cancel_url=cancel_url, metadata=metadata))


async def _json_body(request: Request) -> dict:
    """Return {} for optional or malformed JSON rather than a server error."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _raise_for_paypal(resp: httpx.Response) -> None:
    """Convert PayPal HTTP error responses into a safe 502 response."""
    if resp.is_error:
        logger.error("PayPal API request failed with HTTP %s", resp.status_code)
        raise HTTPException(status_code=502, detail="PayPal could not process this request. Please try again.")


def _origin(request: Request | None, body: dict) -> str:
    from urllib.parse import urlparse
    origin = (body or {}).get("origin_url") or ""
    allowed = [o.strip().strip('"').rstrip("/") for o in os.environ["CORS_ORIGINS"].split(",")]
    parsed = urlparse(origin)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in ("http", "https") and parsed.netloc else ""
    if origin not in allowed:
        origin = allowed[0]
    return origin


@router.post("/contact-unlock/{target_id}")
async def create_contact_unlock(target_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    from utils.profile_view import has_contact_access
    target = await db.users.find_one({"id": target_id, "is_active": True, "role": {"$in": ["crew", "contractor"]}}, {"_id": 0})
    if not target or target_id == current_user["id"]:
        raise HTTPException(status_code=404, detail="Profile not found")
    if await has_contact_access(current_user, target):
        raise HTTPException(status_code=400, detail="You already have access to this contact")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    origin = _origin(request, body)
    session = await _stripe_checkout(
        request, origin, CONTACT_UNLOCK_PRICE,
        f"{origin}/profile/{target_id}?unlock_session_id={{CHECKOUT_SESSION_ID}}", f"{origin}/profile/{target_id}",
        {"user_id": current_user["id"], "type": "contact_unlock", "target_id": target_id})
    await db.payment_transactions.insert_one({
        "id": str(uuid.uuid4()), "user_id": current_user["id"], "session_id": session.session_id,
        "amount": CONTACT_UNLOCK_PRICE, "currency": "usd", "plan": "contact_unlock", "target_id": target_id,
        "payment_method": "stripe", "payment_status": "pending", "created_at": now_str()})
    return {"url": session.url, "session_id": session.session_id, "amount": CONTACT_UNLOCK_PRICE}


@router.get("/contact-unlock/status/{session_id}")
async def contact_unlock_status(session_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    tx = await get_owned_tx(session_id, current_user)
    if not tx or tx.get("plan") != "contact_unlock" or tx.get("payment_method") != "stripe":
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"payment_status": "paid", "target_id": tx["target_id"], "fulfilled": True}
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded"}:
        raise HTTPException(status_code=409, detail="This payment can no longer be fulfilled")

    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url="")
    status = await stripe.get_checkout_status(session_id)
    metadata = getattr(status, "metadata", {}) or {}
    if metadata.get("user_id") != tx.get("user_id") or metadata.get("target_id") != tx.get("target_id"):
        raise HTTPException(status_code=404, detail="Payment not found")
    if status.payment_status != "paid":
        return await _status_response(session_id, status)
    if not _checkout_status_matches_transaction(tx, status, "stripe"):
        logger.error("Stripe contact-unlock status did not match its recorded provider, amount, or currency")
        raise HTTPException(status_code=409, detail="Payment does not match the recorded transaction")
    await _fulfill_contact_unlock_tx(session_id)
    updated_tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    if not updated_tx or updated_tx.get("payment_status") != "paid":
        return {"payment_status": "verification_failed", "target_id": tx["target_id"], "fulfilled": False}
    return {"payment_status": "paid", "target_id": tx["target_id"], "fulfilled": True}


# ─── Guest Job Post ($9.99, no account) ──────────────────────────────────────

from pydantic import BaseModel, Field, EmailStr  # noqa: E402
from auth import limiter  # noqa: E402


class GuestJobRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=120)
    description: str = Field(..., min_length=20, max_length=4000)
    trade: str = Field(..., min_length=1, max_length=60)
    crew_needed: int = Field(..., ge=1, le=20)
    start_time: str = Field(..., min_length=8, max_length=40)
    end_time: str | None = Field(None, max_length=40)
    pay_rate: float = Field(..., gt=0, le=1000)
    address: str = Field(..., min_length=5, max_length=300)
    lat: float | None = None
    lng: float | None = None
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=60)
    poster_name: str = Field(..., min_length=2, max_length=100)
    poster_email: EmailStr
    poster_phone: str = Field(..., min_length=7, max_length=30)
    origin_url: str | None = None


@router.post("/guest-job/checkout")
@limiter.limit("5/minute")
async def guest_job_checkout(data: GuestJobRequest, request: Request):
    from utils.geocoding import valid_coords, geocode_address
    coords = valid_coords(data.lat, data.lng)
    location = ({"address": data.address, "lat": coords[0], "lng": coords[1], "city": data.city or "", "state": data.state or ""}
                if coords else await geocode_address(data.address))
    if not location:
        raise HTTPException(status_code=400, detail="We couldn't locate that address. Please pick a suggested address.")
    guest_id = str(uuid.uuid4())
    origin = _origin(request, {"origin_url": data.origin_url})
    session = await _stripe_checkout(
        request, origin, GUEST_JOB_PRICE, f"{origin}/post-job?guest_session_id={{CHECKOUT_SESSION_ID}}", f"{origin}/post-job",
        {"type": "guest_job", "guest_job_id": guest_id})
    await db.guest_jobs.insert_one({
        "id": guest_id, "status": "pending_payment", "session_id": session.session_id,
        "job": data.model_dump(exclude={"origin_url", "poster_name", "poster_email", "poster_phone", "lat", "lng", "city", "state"}),
        "location": location,
        "contact": {"name": data.poster_name, "email": data.poster_email, "phone": data.poster_phone},
        "created_at": now_str()})
    await db.payment_transactions.insert_one({
        "id": str(uuid.uuid4()), "user_id": None, "guest_job_id": guest_id, "session_id": session.session_id,
        "amount": GUEST_JOB_PRICE, "currency": "usd", "plan": "guest_job", "payment_method": "stripe",
        "payment_status": "pending", "created_at": now_str()})
    return {"url": session.url, "session_id": session.session_id, "amount": GUEST_JOB_PRICE}


async def _publish_paid_guest_job(session_id: str, expected_guest_id: str | None = None) -> str | None:
    """Publish with a resumable state so webhook retries cannot mark a missing job as paid."""
    guest = await db.guest_jobs.find_one({"session_id": session_id}, {"_id": 0})
    if not guest or (expected_guest_id and guest["id"] != expected_guest_id):
        return None
    job_id = guest.get("job_id")
    created_at = guest.get("created_at") or now_str()

    if guest.get("status") == "pending_payment":
        job_id = job_id or str(uuid.uuid4())
        claimed = await db.guest_jobs.find_one_and_update(
            {"id": guest["id"], "status": "pending_payment"},
            {"$set": {"status": "publishing", "job_id": job_id}})
        if not claimed:
            guest = await db.guest_jobs.find_one({"id": guest["id"]}, {"_id": 0})
            if not guest or guest.get("status") not in {"publishing", "published"}:
                return None
            job_id = guest.get("job_id")
    elif guest.get("status") not in {"publishing", "published"}:
        return None

    if not job_id:
        logger.error("Guest job fulfillment state is missing its job reference")
        raise RuntimeError("Guest job fulfillment state is incomplete")

    job = guest["job"]
    job_doc = {
        "id": job_id, "contractor_id": f"guest:{guest['id']}", "contractor_name": guest["contact"]["name"],
        "contractor_photo": None, "is_guest_post": True, "guest_job_id": guest["id"],
        "title": job["title"], "description": job["description"], "trade": job["trade"],
        "crew_needed": job["crew_needed"], "crew_accepted": [], "start_time": job["start_time"],
        "end_time": job.get("end_time"), "pay_rate": job["pay_rate"], "location": guest["location"],
        "status": "open", "is_emergency": False, "created_at": created_at, "completed_at": None,
        "rated_crew": [], "rated_by_crew": [], "invited_crew": [], "images": [],
    }
    result = await db.jobs.update_one({"id": job_id}, {"$setOnInsert": job_doc}, upsert=True)
    saved = await db.jobs.find_one({"id": job_id}, {"_id": 0, "guest_job_id": 1})
    if not saved or saved.get("guest_job_id") != guest["id"]:
        logger.error("Guest job fulfillment ID did not match the saved job")
        raise RuntimeError("Guest job could not be confirmed")

    await db.guest_jobs.update_one(
        {"id": guest["id"], "status": {"$in": ["publishing", "published"]}, "job_id": job_id},
        {"$set": {"status": "published", "paid_at": guest.get("paid_at") or now_str()}})
    await db.payment_transactions.update_one(
        {"session_id": session_id, "plan": "guest_job"},
        {"$set": {"payment_status": "paid", "updated_at": now_str()}})
    if result.upserted_id:
        try:
            from routes.ws_routes import manager
            await manager.broadcast_new_job({k: v for k, v in job_doc.items() if k != "_id"})
        except Exception:
            pass
    return job_id


@router.get("/guest-job/status/{session_id}")
@limiter.limit("30/minute")
async def guest_job_status(session_id: str, request: Request):
    guest = await db.guest_jobs.find_one({"session_id": session_id}, {"_id": 0})
    tx = await db.payment_transactions.find_one(
        {"session_id": session_id, "plan": "guest_job", "payment_method": "stripe"}, {"_id": 0}
    )
    if not guest or not tx:
        raise HTTPException(status_code=404, detail="Payment not found")
    if guest["status"] == "published" and tx.get("payment_status") == "paid":
        return {"payment_status": "paid", "published": True, "job_id": guest["job_id"], "fulfilled": True}
    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url="")
    status = await stripe.get_checkout_status(session_id)
    metadata = getattr(status, "metadata", {}) or {}
    if metadata.get("guest_job_id") != guest["id"]:
        raise HTTPException(status_code=404, detail="Payment not found")
    if status.payment_status != "paid":
        return {"payment_status": status.payment_status, "published": False, "fulfilled": False}
    if not _checkout_status_matches_transaction(tx, status, "stripe"):
        logger.error("Stripe guest-job status did not match its recorded provider, amount, or currency")
        return {"payment_status": "verification_failed", "published": False, "fulfilled": False}
    job_id = await _publish_paid_guest_job(session_id, guest["id"])
    updated_tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
    fulfilled = bool(job_id) and bool(updated_tx) and updated_tx.get("payment_status") == "paid"
    if not fulfilled:
        return {"payment_status": "verification_failed", "published": False, "fulfilled": False}
    return {"payment_status": "paid", "published": True, "job_id": job_id, "fulfilled": True}


# ─── Emergency Job Start fee (one-time, admin-configurable, Stripe) ──────────

DEFAULT_EMERGENCY_FEE = 2.99


async def get_emergency_price() -> float:
    settings = await db.settings.find_one({}, {"_id": 0, "emergency_job_price": 1}) or {}
    try:
        price = float(settings.get("emergency_job_price", DEFAULT_EMERGENCY_FEE))
    except (TypeError, ValueError):
        price = DEFAULT_EMERGENCY_FEE
    return round(price, 2) if price >= 0.5 else DEFAULT_EMERGENCY_FEE


@router.get("/emergency/fee")
async def emergency_fee():
    """Public: current one-time fee to start an emergency job."""
    return {"fee": await get_emergency_price()}


async def fulfill_emergency_tx(session_id: str) -> dict | None:
    """Publishes the emergency job exactly once for a paid, server-verified transaction."""
    tx = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "plan": "emergency_job", "payment_status": "pending"},
        {"$set": {"payment_status": "paid", "updated_at": now_str()}})
    if not tx:
        return None
    job = await db.jobs.find_one_and_update(
        {"id": tx["job_id"], "status": "pending_payment"},
        {"$set": {"status": "open", "emergency_paid": True, "emergency_paid_at": now_str(),
                  "emergency_fee_paid": tx.get("amount"), "published_at": now_str()}},
        projection={"_id": 0}, return_document=True)
    if job:
        from routes.job_routes import publish_job
        await publish_job(job)
    return job


@router.post("/emergency/stripe/{job_id}")
async def emergency_checkout(job_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job or job["contractor_id"] != current_user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("is_emergency") or job.get("status") != "pending_payment":
        raise HTTPException(status_code=400, detail="This job doesn't need an emergency payment")
    try:
        body = await request.json()
    except Exception:
        body = {}
    origin = _origin(request, body)
    fee = await get_emergency_price()
    session = await _stripe_checkout(
        request, origin, fee,
        success_url=f"{origin}/contractor/dashboard?emergency_session={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{origin}/contractor/dashboard?emergency_cancelled={job_id}",
        metadata={"user_id": current_user["id"], "job_id": job_id, "type": "emergency_job", "payment_method": "stripe"})
    await db.payment_transactions.insert_one({
        "id": str(uuid.uuid4()), "user_id": current_user["id"], "session_id": session.session_id,
        "job_id": job_id, "amount": fee, "currency": "usd", "plan": "emergency_job",
        "payment_method": "stripe", "payment_status": "pending", "created_at": now_str()})
    return {"url": session.url, "session_id": session.session_id, "fee": fee}


@router.get("/emergency/status/{session_id}")
async def emergency_status(session_id: str, current_user: dict = Depends(get_current_user)):
    tx = await get_owned_tx(session_id, current_user)
    if not tx or tx.get("plan") != "emergency_job":
        raise HTTPException(status_code=404, detail="Payment not found")
    if tx.get("payment_status") == "paid":
        return {"payment_status": "paid", "published": True, "job_id": tx["job_id"]}
    if tx.get("payment_status") in {"cancelled", "canceled", "refunded", "expired"}:
        return {"payment_status": tx["payment_status"], "published": False, "job_id": tx.get("job_id")}
    stripe = StripeCheckout(api_key=_stripe_api_key(), webhook_url="")
    status = await stripe.get_checkout_status(session_id)
    metadata = getattr(status, "metadata", {}) or {}
    if metadata.get("user_id") != tx.get("user_id") or metadata.get("job_id") != tx["job_id"]:
        raise HTTPException(status_code=404, detail="Payment not found")
    if status.payment_status == "paid":
        if not _checkout_status_matches_transaction(tx, status, "stripe"):
            logger.error("Stripe emergency status did not match its recorded provider, amount, or currency")
            return {"payment_status": "verification_failed", "published": False, "job_id": tx["job_id"], "fulfilled": False}
        await fulfill_emergency_tx(session_id)
        updated_tx = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        fulfilled = bool(updated_tx) and updated_tx.get("payment_status") == "paid"
        return {"payment_status": "paid" if fulfilled else "verification_failed",
                "published": fulfilled, "job_id": tx["job_id"], "fulfilled": fulfilled}
    if status.status == "expired":
        await db.payment_transactions.update_one({"session_id": session_id}, {"$set": {"payment_status": "expired"}})
    return {"payment_status": status.payment_status, "status": status.status, "published": False,
            "job_id": tx["job_id"], "fulfilled": False}
