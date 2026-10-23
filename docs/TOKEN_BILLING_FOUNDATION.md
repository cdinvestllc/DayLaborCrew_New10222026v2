# Token billing foundation (phase 1)

This change adds a versioned billing catalog and spendable token wallet, and connects new Stripe/PayPal subscription and token-pack checkout to verified token grants. It intentionally does **not** migrate the legacy subscription routes or replace job-post credits; those cutovers need separate migration/reconciliation work.

## API

- `GET /api/billing/catalog`: public, read-only current catalog plus version.
- `PUT /api/billing/catalog`: admin-only full replacement. Send `expected_version` and the complete `catalog`. A stale version returns HTTP 409. Unknown fields and invalid costs are rejected.
- `GET /api/billing/catalog/history`: admin-only last 100 catalog versions.
- `GET /api/billing/wallet`: authenticated user's token balance and active expiry lots.
- `GET /api/billing/wallet/ledger?limit=50`: authenticated user's recent ledger entries.
- `POST /api/billing/admin/wallets/{user_id}/grant`: admin-only token grant; requires a positive amount, reason, future timezone-aware expiry, and idempotency key.
- `POST /api/billing/payments/stripe/subscription-session`: create Stripe monthly/annual Pro checkout for the signed-in user's role.
- `POST /api/billing/payments/paypal/subscription`: create PayPal recurring subscription, after validating the configured PayPal plan against the catalog price and cadence.
- `POST /api/billing/payments/stripe/token-pack-session` and `POST /api/billing/payments/paypal/token-pack-order`: create one-time token pack checkout.
- `POST /api/billing/payments/paypal/token-pack-capture-payment/{payment_id}`: capture a signed-in user's returned PayPal token-pack order.
- `GET /api/billing/payments/stripe/status/{session_id}` and `GET /api/billing/payments/paypal/subscription-status/{subscription_id}`: verified status/fulfillment checks.
- `POST /api/billing/payments/release-due-grants`: protected scheduler endpoint for monthly allowance releases within annual subscriptions.

The wallet endpoints only operate on spendable `tokens`. The existing `points` field remains separate and is not converted or silently migrated.

## Default catalog

- Free Crew: $0, 10 tokens per grant period.
- Free Contractor: $0, 10 tokens per grant period.
- Pro Crew: $9.99/month or $80.32/year, 20 monthly tokens.
- Pro Contractor: $19.99/month or $160.72/year, 40 monthly tokens.
- Enterprise is present but disabled until priced/configured.
- 20-token pack: $10.
- Crew job application: 5 tokens.
- Contractor job post: 10 tokens; emergency surcharge: 10 additional tokens.
- Profile/job boost: 10 tokens; 30-second portfolio video for 30 days: 2 tokens.
- Free grants expire in 30 days; Pro grants in 60 days. Free-tier purchased tokens default to 60 days. Pro purchased-token expiry is configured as billing-cycle end.
- Guest Project Manager checkout is configured as a one-off $10 / 20-token equivalent. No anonymous reusable wallet is allowed.
- Stripe and PayPal are enabled in the catalog; Square, Cash App Pay, and crypto are disabled pending provider adapters and deployment readiness.

All amounts are integer cents. Stripe subscription invoices and PayPal subscription sale events are verified before a monthly token grant is applied. One-time packs are credited only after a verified Stripe payment or successful server-side PayPal capture/webhook. Payment amounts and currencies must exactly match the saved checkout/catalog snapshot; mismatches fail closed.

## Storage and consistency

Wallet state uses:
- `token_wallets`: cached balance, keyed by user id.
- `token_lots`: grants/purchases with remaining amount, source, and expiry.
- `token_ledger`: append-only grants, spends, and expirations with unique per-user idempotency keys.
- `billing_catalog`: active catalog and bounded version history.
- `billing_payments`: idempotent token-pack/checkout records with provider reference, catalog price snapshot, and fulfillment state.
- `billing_subscriptions`: provider subscription IDs, plan/price/token snapshots, renewal status, paid invoice/sale references, and annual monthly-release schedule.

Spending consumes the earliest-expiring unexpired lots first (FEFO). Balance, lot, and ledger changes run in a MongoDB transaction. Do not disable transactions or silently fall back to non-atomic updates: the deployment database must support MongoDB multi-document transactions (for example, a replica set or supported managed cluster). Otherwise wallet operations fail closed with HTTP 503.

Retries must reuse the same idempotency key and the same operation parameters. Reusing a key for different parameters returns a conflict. Refunds should be represented by compensating ledger operations in a later phase; never edit historical entries.

## Provider configuration and operational release gates

- Stripe must have a valid `STRIPE_API_KEY`, explicit `STRIPE_MODE`, and `STRIPE_WEBHOOK_SECRET`. Subscribe the webhook to `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated`, and `customer.subscription.deleted`. Keep the endpoint at `/api/payments/stripe/webhook`.
- PayPal requires the existing `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, `PAYPAL_MODE`, and `PAYPAL_WEBHOOK_ID`. Subscribe it to `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.SALE.COMPLETED`, `BILLING.SUBSCRIPTION.CANCELLED`, `BILLING.SUBSCRIPTION.SUSPENDED`, and `BILLING.SUBSCRIPTION.EXPIRED`.
- Create/activate PayPal billing plans whose price/cadence match the catalog and configure `PAYPAL_PLAN_ID_PRO_CREW_MONTH`, `PAYPAL_PLAN_ID_PRO_CREW_YEAR`, `PAYPAL_PLAN_ID_PRO_CONTRACTOR_MONTH`, and `PAYPAL_PLAN_ID_PRO_CONTRACTOR_YEAR`. If a plan is missing or doesn't match the catalog, checkout fails closed.
- Configure `BILLING_SCHEDULER_SECRET` and call `POST /api/billing/payments/release-due-grants` daily with `Authorization: Bearer <secret>`. The endpoint is protected and grants each annual subscriber's monthly allowance using the same subscription-period idempotency key as invoice processing. Until this scheduler is running, an annual purchaser receives only their first month's token grant.
- MongoDB transactions must be enabled before exposing token wallet operations. Test webhook delivery, scheduled runs, and database failover in staging before launch.

## Recommended next migration steps

1. Enforce catalog-backed token spending on job applications, job posting, emergency surcharge, profile/job boosts, 30-second portfolio video, and contractor verification renewal. Use a reserve/action-commit/release pattern so a failed job action doesn't consume tokens.
2. Add recurring Free-tier and 14-day trial grants, qualifying-activity counting, expiration notifications, and downgrade handling.
3. Reconcile existing job-post credits/subscriptions and choose a tested, audited migration before deprecating old fields.
4. Add compensating ledger entries for administrative corrections and provider refunds/chargebacks.
5. Add Square/Cash App Pay adapter and crypto only after payment-provider operations and compliance review are complete.
