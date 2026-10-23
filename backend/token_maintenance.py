"""Scheduled token maintenance: safe legacy migration, free grants, trial downgrade, reconciliation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from database import db
from token_actions import active_catalog, record_qualifying_activity
from token_billing import TokenWalletService, utc_iso, utc_now

logger = logging.getLogger(__name__)
wallet = TokenWalletService(db)
LEGACY_PAID_PLANS = {
    "crew_monthly", "crew_quarterly", "crew_annual",
    "contractor_basic", "contractor_pro", "contractor_business"
}


def _dt(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime):
            return None
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso_or_now(value: Any) -> datetime:
    return _dt(value) or utc_now()


def _legacy_paid_active(user: dict, now: datetime) -> bool:
    end = _dt(user.get("subscription_end"))
    return (
        user.get("subscription_status") == "active"
        and user.get("subscription_plan") in LEGACY_PAID_PLANS
        and end is not None and end > now
        and not user.get("billing_subscription_id")
    )


async def _convert_unused_legacy_job_credits(user: dict, *, cutover_at: datetime) -> int:
    """Convert only unused credits from the final paid contractor period, once.

    Credits are converted before billing_mode changes; a failed ledger write therefore leaves
    the account on legacy billing so the scheduler can safely retry.
    """
    if user.get("role") != "contractor" or user.get("legacy_credit_conversion_done"):
        return 0
    plan = user.get("subscription_plan")
    quotas = {"contractor_basic": 4, "contractor_pro": 10, "contractor_business": 20}
    quota = quotas.get(plan)
    if not quota:
        return 0
    end = _dt(user.get("subscription_end"))
    if not end or end > cutover_at:
        return 0
    # The legacy quota counter resets when the subscription-period start changes.
    period_days = 30
    expected_period = (end - timedelta(days=period_days)).date().isoformat()
    if str(user.get("job_post_period") or "")[:10] != expected_period:
        return 0
    used_value = user.get("job_posts_used", 0)
    try:
        used = max(0, int(used_value or 0))
    except (TypeError, ValueError):
        used = quota
    remaining = max(0, quota - min(quota, used))
    if remaining <= 0:
        return 0

    _, catalog = await active_catalog()
    token_amount = remaining * int(catalog.token_costs.contractor_post_job)
    if token_amount <= 0:
        return 0
    expiry = cutover_at + timedelta(days=int(catalog.token_policy.free_paid_token_expiry_days))
    period_key = f"legacy-post-credit:{user['id']}:{expected_period}"
    existing_entry = await db.token_ledger.find_one(
        {"user_id": user["id"], "idempotency_key": period_key}, {"_id": 0, "amount": 1}
    )
    if existing_entry:
        await db.users.update_one(
            {"id": user["id"]},
            {"$set": {
                "legacy_credit_conversion_done": True,
                "legacy_credit_conversion_period": expected_period,
                "legacy_credit_conversion_tokens": int(existing_entry.get("amount", token_amount)),
                "legacy_credit_conversion_at": utc_iso(cutover_at)
            }}
        )
        return 0
    result = await wallet.grant_tokens(
        user["id"], token_amount,
        idempotency_key=period_key,
        reason=f"Convert {remaining} unused legacy job-post credits from period {expected_period}",
        source="legacy_credit_conversion", expires_at=expiry,
        metadata={
            "legacy_plan": plan, "legacy_period_start": expected_period,
            "legacy_quota": quota, "legacy_used": used, "legacy_credits_converted": remaining,
            "tokens_per_credit": int(catalog.token_costs.contractor_post_job), "policy_version": 1
        },
        reference_id=f"{user['id']}:{expected_period}"
    )
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "legacy_credit_conversion_done": True,
            "legacy_credit_conversion_period": expected_period,
            "legacy_credit_conversion_tokens": token_amount,
            "legacy_credit_conversion_at": utc_iso(cutover_at)
        }}
    )
    return token_amount if not result.get("already_processed") else 0


async def _classify_legacy_users(limit: int) -> dict[str, int]:
    now = utc_now()
    _, catalog = await active_catalog()
    welcome_expiry_days = catalog.token_policy.welcome_grant_expiry_days
    migrated_token = 0
    grandfathered = 0
    cursor = db.users.find(
        {"role": {"$in": ["crew", "contractor"]},
         "token_billing_migration_v1": {"$ne": True},
         "billing_mode": {"$nin": ["tokens_v1", "legacy_compat"]}},
        {"_id": 0, "id": 1, "role": 1, "billing_mode": 1, "subscription_status": 1,
         "subscription_plan": 1, "subscription_end": 1, "trial_end_date": 1,
         "created_at": 1, "billing_subscription_id": 1, "subscription_plan": 1, "subscription_end": 1,
         "job_post_period": 1, "job_posts_used": 1, "legacy_credit_conversion_done": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    for user in users:
        uid = user.get("id")
        if not uid:
            continue
        if _legacy_paid_active(user, now):
            end = _dt(user.get("subscription_end"))
            await db.users.update_one(
                {"id": uid, "token_billing_migration_v1": {"$ne": True}},
                {"$set": {
                    "billing_mode": "legacy_compat",
                    "legacy_token_cutover_at": utc_iso(end),
                    "token_billing_migration_v1": True,
                    "token_billing_migrated_at": utc_iso(now)
                }}
            )
            grandfathered += 1
            continue
        if user.get("subscription_plan") in LEGACY_PAID_PLANS and _dt(user.get("subscription_end")) and _dt(user.get("subscription_end")) <= now:
            # Don't flip the billing mode until the unused final-period quota is either
            # converted or explicitly determined not convertible.
            await _convert_unused_legacy_job_credits(user, cutover_at=now)

        # Legacy trial accounts receive a bounded, no-auto-charge 14-day Pro trial if
        # their prior trial is still active. Everyone else transitions to Free.
        old_trial_end = _dt(user.get("trial_end_date"))
        trial_end = None
        if user.get("subscription_status") == "trial" and old_trial_end and old_trial_end > now:
            trial_end = min(old_trial_end, now + timedelta(days=catalog.trial_days))
        welcome_expiry = now + timedelta(days=welcome_expiry_days)
        fields = {
            "billing_mode": "tokens_v1",
            "token_plan": ("trial_crew" if user["role"] == "crew" else "trial_contractor") if trial_end else ("free_crew" if user["role"] == "crew" else "free_contractor"),
            "billing_plan": ("trial_crew" if user["role"] == "crew" else "trial_contractor") if trial_end else ("free_crew" if user["role"] == "crew" else "free_contractor"),
            "billing_subscription_status": "trial" if trial_end else "free",
            "token_free_period_start": utc_iso(now),
            "token_welcome_expiry_at": utc_iso(welcome_expiry),
            "token_billing_migration_v1": True,
            "token_billing_migrated_at": utc_iso(now)
        }
        if trial_end:
            fields.update({"token_trial_started_at": utc_iso(now), "token_trial_end_at": utc_iso(trial_end)})
        await db.users.update_one(
            {"id": uid, "token_billing_migration_v1": {"$ne": True}},
            {"$set": fields}
        )
        migrated_token += 1
    return {"legacy_paid_grandfathered": grandfathered, "users_migrated_to_tokens": migrated_token}


async def _cut_over_expired_legacy_users(limit: int) -> int:
    now = utc_now()
    _, catalog = await active_catalog()
    expiry_days = catalog.token_policy.welcome_grant_expiry_days
    cursor = db.users.find(
        {"billing_mode": "legacy_compat", "legacy_token_cutover_at": {"$lte": utc_iso(now)}},
        {"_id": 0, "id": 1, "role": 1, "subscription_plan": 1, "subscription_end": 1,
         "job_post_period": 1, "job_posts_used": 1, "legacy_credit_conversion_done": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    changed = 0
    for user in users:
        await _convert_unused_legacy_job_credits(user, cutover_at=now)
        now_text = utc_iso()
        expiry = utc_iso(now + timedelta(days=expiry_days))
        result = await db.users.update_one(
            {"id": user["id"], "billing_mode": "legacy_compat",
             "legacy_token_cutover_at": {"$lte": utc_iso(now)}},
            {"$set": {
                "billing_mode": "tokens_v1",
                "token_plan": "free_crew" if user["role"] == "crew" else "free_contractor",
                "billing_plan": "free_crew" if user["role"] == "crew" else "free_contractor",
                "billing_subscription_status": "free",
                "token_free_period_start": now_text,
                "token_welcome_expiry_at": expiry,
                "legacy_token_cutover_completed_at": now_text
            }}
        )
        changed += int(result.modified_count > 0)
    return changed


async def _ensure_starter_and_trial_grants(limit: int) -> dict[str, int]:
    now = utc_now()
    _, catalog = await active_catalog()
    cursor = db.users.find(
        {"role": {"$in": ["crew", "contractor"]}, "billing_mode": "tokens_v1"},
        {"_id": 0, "id": 1, "role": 1, "created_at": 1,
         "token_trial_started_at": 1, "token_trial_end_at": 1,
         "token_welcome_expiry_at": 1, "token_welcome_granted_at": 1,
         "token_trial_granted_at": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    welcome_grants = 0
    trial_grants = 0
    failures = 0
    for user in users:
        uid = user.get("id")
        if not uid:
            continue
        try:
            welcome_expiry = _dt(user.get("token_welcome_expiry_at"))
            if not user.get("token_welcome_expiry_at"):
                anchor = _dt(user.get("created_at")) or now
                welcome_expiry = anchor + timedelta(days=catalog.token_policy.welcome_grant_expiry_days)
                if welcome_expiry <= now:
                    welcome_expiry = now + timedelta(days=catalog.token_policy.welcome_grant_expiry_days)
                await db.users.update_one(
                    {"id": uid, "token_welcome_expiry_at": {"$exists": False}},
                    {"$set": {"token_welcome_expiry_at": utc_iso(welcome_expiry)}}
                )
            if welcome_expiry and welcome_expiry > now and int(catalog.token_policy.welcome_tokens) > 0:
                result = await wallet.grant_tokens(
                    uid, int(catalog.token_policy.welcome_tokens),
                    idempotency_key=f"welcome:{uid}:token-v1",
                    reason="One-time starter token grant", source="welcome_grant",
                    expires_at=welcome_expiry,
                    metadata={"role": user["role"], "policy_version": 1}
                )
                if not result.get("already_processed"):
                    welcome_grants += 1
                await db.users.update_one(
                    {"id": uid}, {"$set": {"token_welcome_granted_at": utc_iso()}}
                )
            trial_end = _dt(user.get("token_trial_end_at"))
            if user.get("token_trial_started_at") and trial_end and trial_end > now:
                amount = int(catalog.token_policy.trial_crew_tokens if user["role"] == "crew"
                             else catalog.token_policy.trial_contractor_tokens)
                if amount > 0:
                    result = await wallet.grant_tokens(
                        uid, amount, idempotency_key=f"trial:{uid}:token-v1",
                        reason="14-day Pro trial token allowance", source="trial_grant",
                        expires_at=trial_end,
                        metadata={"role": user["role"], "trial_end_at": utc_iso(trial_end), "policy_version": 1}
                    )
                    if not result.get("already_processed"):
                        trial_grants += 1
                    await db.users.update_one(
                        {"id": uid}, {"$set": {"token_trial_granted_at": utc_iso()}}
                    )
        except Exception as exc:
            failures += 1
            logger.warning("Token starter/trial grant failed for user %s: %s", uid, type(exc).__name__)
    return {"welcome_grants": welcome_grants, "trial_grants": trial_grants, "grant_failures": failures}


async def _downgrade_expired_trials(limit: int) -> int:
    now_text = utc_iso()
    cursor = db.users.find(
        {"billing_mode": "tokens_v1", "billing_subscription_status": "trial",
         "token_trial_end_at": {"$lte": now_text}, "billing_subscription_id": {"$exists": False}},
        {"_id": 0, "id": 1, "role": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    changed = 0
    for user in users:
        role_plan = "free_crew" if user["role"] == "crew" else "free_contractor"
        result = await db.users.update_one(
            {"id": user["id"], "billing_subscription_status": "trial",
             "token_trial_end_at": {"$lte": now_text}, "billing_subscription_id": {"$exists": False}},
            {"$set": {"billing_subscription_status": "free", "billing_plan": role_plan,
                      "token_plan": role_plan, "token_trial_downgraded_at": now_text}}
        )
        changed += int(result.modified_count > 0)
    return changed


async def _downgrade_expired_paid_subscriptions(limit: int) -> int:
    now_text = utc_iso()
    cursor = db.users.find(
        {"billing_mode": "tokens_v1",
         "billing_subscription_status": {"$in": ["cancelled", "canceled", "expired", "suspended", "past_due"]},
         "billing_subscription_period_end": {"$lte": now_text}},
        {"_id": 0, "id": 1, "role": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    changed = 0
    for user in users:
        plan = "free_crew" if user["role"] == "crew" else "free_contractor"
        result = await db.users.update_one(
            {"id": user["id"],
             "billing_subscription_status": {"$in": ["cancelled", "canceled", "expired", "suspended", "past_due"]},
             "billing_subscription_period_end": {"$lte": now_text}},
            {"$set": {"billing_subscription_status": "free", "billing_plan": plan, "token_plan": plan,
                      "token_subscription_downgraded_at": now_text, "token_free_period_start": now_text}}
        )
        changed += int(result.modified_count > 0)
    return changed


async def _expire_portfolio_videos(limit: int) -> int:
    now_text = utc_iso()
    cursor = db.users.find(
        {"portfolio_videos.expires_at": {"$lte": now_text}},
        {"_id": 0, "id": 1, "portfolio_videos": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    removed = 0
    for user in users:
        expired = [video for video in (user.get("portfolio_videos") or [])
                   if _dt(video.get("expires_at")) and _dt(video.get("expires_at")) <= utc_now()]
        for video in expired:
            url = video.get("url")
            prefix = "/api/public/files/"
            if isinstance(url, str) and url.startswith(prefix):
                storage_path = url[len(prefix):]
                await db.files.update_one(
                    {"storage_path": storage_path}, {"$set": {"is_deleted": True, "deleted_at": now_text}}
                )
        if expired:
            result = await db.users.update_one(
                {"id": user["id"]},
                {"$pull": {"portfolio_videos": {"expires_at": {"$lte": now_text}}}}
            )
            removed += len(expired) if result.modified_count else 0
    return removed


async def _issue_qualified_free_grants(limit: int) -> dict[str, int]:
    now = utc_now()
    now_text = utc_iso(now)
    _, catalog = await active_catalog()
    period_days = int(catalog.token_policy.active_free_period_days)
    required = int(catalog.token_policy.active_free_tasks_required)
    free_tokens = int(catalog.plans["free_crew"].monthly_tokens)
    grant_days = int(catalog.token_policy.free_grant_expiry_days)
    cursor = db.users.find(
        {"role": {"$in": ["crew", "contractor"]}, "billing_mode": "tokens_v1",
         "billing_subscription_status": {"$in": ["free", None]},
         "billing_plan": {"$in": ["free_crew", "free_contractor"]}},
        {"_id": 0, "id": 1, "role": 1, "created_at": 1, "token_free_period_start": 1,
         "last_free_token_grant_period": 1}
    ).limit(max(1, min(limit, 1000)))
    users = await cursor.to_list(length=max(1, min(limit, 1000)))
    grants = 0
    qualified = 0
    for user in users:
        uid = user.get("id")
        if not uid:
            continue
        period_start = _dt(user.get("token_free_period_start")) or _dt(user.get("created_at")) or now
        if period_start > now:
            period_start = now
        elapsed = max(0, int((now - period_start).total_seconds() // (period_days * 86400)))
        current_start = period_start + timedelta(days=elapsed * period_days)
        current_end = current_start + timedelta(days=period_days)
        current_start_text = utc_iso(current_start)
        if user.get("token_free_period_start") != current_start_text:
            await db.users.update_one({"id": uid}, {"$set": {"token_free_period_start": current_start_text}})
        if user.get("last_free_token_grant_period") == current_start_text:
            continue
        count = await db.token_activity_events.count_documents({
            "user_id": uid, "created_at": {"$gte": current_start_text, "$lt": utc_iso(current_end)},
            "activity": {"$in": catalog.token_policy.qualifying_activities}
        })
        if count < required:
            continue
        qualified += 1
        expiry = now + timedelta(days=grant_days)
        amount = int(catalog.plans["free_crew" if user["role"] == "crew" else "free_contractor"].monthly_tokens)
        try:
            result = await wallet.grant_tokens(
                uid, amount, idempotency_key=f"freegrant:{uid}:{current_start_text}",
                reason=f"Free-tier activity allowance for period starting {current_start_text}",
                source="free_activity_grant", expires_at=expiry,
                metadata={"qualifying_activity_count": count, "required": required,
                          "period_start": current_start_text, "period_end": utc_iso(current_end),
                          "policy_version": 1}
            )
            await db.users.update_one(
                {"id": uid}, {"$set": {"last_free_token_grant_period": current_start_text,
                                       "last_free_token_grant_at": now_text}}
            )
            if not result.get("already_processed"):
                grants += 1
        except Exception as exc:
            logger.warning("Free token grant failed for user %s: %s", uid, type(exc).__name__)
    return {"free_accounts_checked": len(users), "free_accounts_qualified": qualified, "free_grants": grants}


async def _record_verified_referral_activities(limit: int) -> int:
    cursor = db.users.find(
        {"referred_by": {"$type": "string"}, "is_verified": True, "is_active": True},
        {"_id": 0, "id": 1, "referred_by": 1}
    ).limit(max(1, min(limit, 1000)))
    rows = await cursor.to_list(length=max(1, min(limit, 1000)))
    recorded = 0
    for user in rows:
        if await record_qualifying_activity(
            user["referred_by"], "verified_activated_referral",
            f"referral-activation:{user['id']}",
            metadata={"referred_user_id": user["id"]}
        ):
            recorded += 1
    return recorded


async def _business_action_succeeded(reservation: dict) -> bool:
    reservation_id = reservation.get("reservation_id")
    user_id = reservation.get("user_id")
    action_type = (reservation.get("metadata") or {}).get("action_type")
    reference_id = reservation.get("reference_id")
    if not reservation_id or not user_id or not action_type:
        return False
    if action_type == "job_posting":
        found = await db.jobs.find_one({"id": reference_id, "token_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "job_application":
        found = await db.jobs.find_one(
            {"id": reference_id, "crew_acceptance_receipts": {"$elemMatch": {"crew_id": user_id, "reservation_id": reservation_id}}},
            {"_id": 1}
        )
    elif action_type == "job_boost":
        found = await db.jobs.find_one({"id": reference_id, "token_boost_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "profile_boost":
        found = await db.users.find_one({"id": user_id, "profile_boost_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "verification_renewal":
        found = await db.users.find_one({"id": user_id, "verification_token_reservation_id": reservation_id}, {"_id": 1})
    elif action_type == "portfolio_video":
        found = await db.users.find_one({"id": user_id, "portfolio_videos": {"$elemMatch": {"reservation_id": reservation_id}}}, {"_id": 1})
    else:
        return False
    return found is not None


async def _reconcile_reservations(limit: int) -> dict[str, int]:
    now = utc_now()
    cursor = db.token_reservations.find(
        {"status": "reserved"}, {"_id": 0}
    ).sort("created_at", 1).limit(max(1, min(limit, 500)))
    pending = await cursor.to_list(length=max(1, min(limit, 500)))
    committed = released = skipped = 0
    for reservation in pending:
        try:
            action_succeeded = await _business_action_succeeded(reservation)
            if action_succeeded:
                await wallet.commit_reservation(
                    reservation["user_id"], reservation["reservation_id"],
                    metadata={"action_succeeded": True, "reconciled": True}
                )
                committed += 1
                continue
            expiry = _dt(reservation.get("expires_at"))
            if expiry and expiry <= now:
                await wallet.release_reservation(
                    reservation["user_id"], reservation["reservation_id"],
                    reason="Reservation timed out before the business action completed",
                    metadata={"automatic": True, "reconciled": True}
                )
                released += 1
            else:
                skipped += 1
        except Exception as exc:
            logger.warning("Token reservation reconciliation failed: %s", type(exc).__name__)
            skipped += 1
    return {"reservations_checked": len(pending), "reservations_committed": committed,
            "reservations_released": released, "reservations_pending": skipped}


async def run_token_maintenance(limit: int = 250) -> dict[str, Any]:
    """Idempotent scheduler: migration, trial lifecycle, free rewards, referral activity, recovery."""
    bounded = max(1, min(int(limit), 1000))
    results: dict[str, Any] = {}
    results["migration"] = await _classify_legacy_users(bounded)
    results["legacy_cutovers"] = await _cut_over_expired_legacy_users(bounded)
    results["starter_grants"] = await _ensure_starter_and_trial_grants(bounded)
    results["trials_downgraded"] = await _downgrade_expired_trials(bounded)
    results["paid_subscriptions_downgraded"] = await _downgrade_expired_paid_subscriptions(bounded)
    results["expired_portfolio_videos_removed"] = await _expire_portfolio_videos(bounded)
    results["referral_activities_recorded"] = await _record_verified_referral_activities(bounded)
    results["free_grants"] = await _issue_qualified_free_grants(bounded)
    results["reservation_reconciliation"] = await _reconcile_reservations(min(bounded, 500))
    # Expirations are applied on wallet operations; this pass also catches dormant wallets.
    expiry_cursor = db.users.find(
        {"billing_mode": "tokens_v1", "role": {"$in": ["crew", "contractor"]}},
        {"_id": 0, "id": 1}
    ).limit(bounded)
    expiration_checks = 0
    async for row in expiry_cursor:
        try:
            await wallet.wallet_summary(row["id"])
            expiration_checks += 1
        except Exception as exc:
            logger.warning("Wallet expiration sweep failed: %s", type(exc).__name__)
    results["wallet_expiration_checks"] = expiration_checks
    results["ran_at"] = utc_iso()
    return results
