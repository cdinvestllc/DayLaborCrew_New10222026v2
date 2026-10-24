"""Unit tests for the configurable token catalog and FEFO wallet allocation."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from token_billing import (  # noqa: E402
    BillingCatalog, DEFAULT_BILLING_CATALOG, InsufficientTokens, allocate_lots, utc_iso
)


def test_default_catalog_matches_role_specific_token_model():
    catalog = BillingCatalog.model_validate(DEFAULT_BILLING_CATALOG)
    assert catalog.plans["free_crew"].monthly_price_cents == 0
    assert catalog.plans["free_crew"].monthly_tokens == 10
    assert catalog.plans["free_contractor"].monthly_tokens == 10
    assert catalog.plans["pro_crew"].monthly_price_cents == 999
    assert catalog.plans["pro_crew"].annual_price_cents == 8032
    assert catalog.plans["pro_crew"].monthly_tokens == 20
    assert catalog.plans["pro_contractor"].monthly_price_cents == 1999
    assert catalog.plans["pro_contractor"].annual_price_cents == 16072
    assert catalog.plans["pro_contractor"].monthly_tokens == 40
    assert catalog.plans["enterprise"].enabled is False


def test_catalog_has_expected_token_costs_and_lifetime_rules():
    catalog = BillingCatalog.model_validate(DEFAULT_BILLING_CATALOG)
    assert catalog.token_costs.crew_apply_job == 5
    assert catalog.token_costs.contractor_post_job == 10
    assert catalog.token_costs.emergency_job_surcharge == 10
    assert catalog.token_pack.tokens == 20
    assert catalog.token_pack.price_cents == 1000
    assert catalog.token_policy.free_grant_expiry_days == 30
    assert catalog.token_policy.pro_grant_expiry_days == 60
    assert catalog.token_policy.active_free_tasks_required == 2
    assert catalog.token_policy.tokens_cash_redeemable is False
    assert catalog.token_policy.tokens_transferable is False
    assert catalog.token_policy.tokens_refundable is False
    assert catalog.guest_project_manager.anonymous_wallet is False


def test_catalog_rejects_free_plan_pricing_changes():
    catalog = deepcopy(DEFAULT_BILLING_CATALOG)
    catalog["plans"]["free_crew"]["monthly_price_cents"] = 100
    with pytest.raises(ValidationError, match="Free Crew must remain free"):
        BillingCatalog.model_validate(catalog)


def test_catalog_rejects_missing_plan_and_unknown_config_fields():
    catalog = deepcopy(DEFAULT_BILLING_CATALOG)
    del catalog["plans"]["pro_crew"]
    with pytest.raises(ValidationError, match="plans must contain exactly"):
        BillingCatalog.model_validate(catalog)
    catalog = deepcopy(DEFAULT_BILLING_CATALOG)
    catalog["unexpected_setting"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BillingCatalog.model_validate(catalog)


def test_catalog_rejects_negative_cost_and_provider_keys_missing():
    catalog = deepcopy(DEFAULT_BILLING_CATALOG)
    catalog["token_costs"]["crew_apply_job"] = -5
    with pytest.raises(ValidationError):
        BillingCatalog.model_validate(catalog)
    catalog = deepcopy(DEFAULT_BILLING_CATALOG)
    del catalog["payment_providers"]["crypto"]
    with pytest.raises(ValidationError, match="payment_providers must specify"):
        BillingCatalog.model_validate(catalog)


def test_token_allocation_spends_earliest_expiring_lots_first():
    lots = [
        {"lot_id": "long", "remaining": 20, "expires_at": "2026-12-01T00:00:00+00:00"},
        {"lot_id": "short", "remaining": 3, "expires_at": "2026-10-24T00:00:00+00:00"},
        {"lot_id": "middle", "remaining": 4, "expires_at": "2026-11-01T00:00:00+00:00"}
    ]
    assert allocate_lots(lots, 8) == [
        {"lot_id": "short", "amount": 3, "expires_at": "2026-10-24T00:00:00+00:00"},
        {"lot_id": "middle", "amount": 4, "expires_at": "2026-11-01T00:00:00+00:00"},
        {"lot_id": "long", "amount": 1, "expires_at": "2026-12-01T00:00:00+00:00"}
    ]


def test_token_allocation_fails_closed_on_insufficient_or_invalid_amount():
    with pytest.raises(InsufficientTokens):
        allocate_lots([{"lot_id": "one", "remaining": 2, "expires_at": "2026-12-01T00:00:00+00:00"}], 3)
    with pytest.raises(ValueError):
        allocate_lots([], 0)
    with pytest.raises(ValueError):
        allocate_lots([], True)


def test_utc_iso_rejects_timezone_naive_datetimes():
    with pytest.raises(ValueError, match="timezone"):
        utc_iso(datetime(2026, 10, 23))
    assert utc_iso(datetime(2026, 10, 23, tzinfo=timezone.utc)).endswith("+00:00")
