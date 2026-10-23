# Running legacy backend integration tests safely

These suites call a running API and some create/delete test jobs, edit seeded contractor verification, update admin fee settings, and write directly to MongoDB. Run them only against a dedicated staging deployment and its isolated database. Do not use the shared `*.preview.emergentagent.com` API.

## GitHub Actions setup

Create/configure the GitHub Actions environment named `staging` under repository Settings → Environments. Protect it with approval if appropriate. Populate the following environment variables and secrets. Use disposable seeded test users and test-only payment credentials; do not use production users, databases, or payment keys.

Required environment variables:
- `STAGING_API_URL`: HTTPS origin for the dedicated API, with a hostname containing `staging`, `stage`, `stg`, or `test`; no path.
- `STAGING_WEB_ORIGIN`: HTTPS origin allowed by staging CORS; hostname marked as staging/test; no path.
- `STAGING_MONGO_HOST`: exact hostname of the isolated staging MongoDB deployment.
- `STAGING_DB_NAME`: dedicated database name containing `staging`, `stage`, `stg`, or `test`.
- `STAGING_STRIPE_MODE`: exactly `test`.
- `STAGING_SUPERADMIN_EMAIL`, `STAGING_ADMIN_EMAIL`, `STAGING_CONTRACTOR1_EMAIL`, `STAGING_CONTRACTOR2_EMAIL`, `STAGING_CONTRACTOR3_EMAIL`, `STAGING_CREW_EMAIL`, `STAGING_CREW2_EMAIL`: seeded non-production account emails.

Required environment secrets:
- `STAGING_MONGO_URL`: restricted-access URI to the same isolated database the staging API uses.
- `STAGING_JWT_SECRET_KEY`: staging API JWT signing secret (at least 32 characters).
- `STAGING_SUPERADMIN_PASSWORD`, `STAGING_ADMIN_PASSWORD`, `STAGING_CONTRACTOR1_PASSWORD`, `STAGING_CONTRACTOR2_PASSWORD`, `STAGING_CONTRACTOR3_PASSWORD`, `STAGING_CREW_PASSWORD`, `STAGING_CREW2_PASSWORD`: credentials for those seeded accounts.

Also configure the staging deployment itself to use that database, the same JWT secret, and sandbox Stripe settings. The workflow validates the declared target, pinned Mongo host, database name and Stripe mode before attempting a test. The suites must not be run concurrently against one staging database because the security suite uses a cleanup prefix and other tests modify shared seeded-account state.

## One-command seeding (`backend/scripts/seed_staging.py`)

The script reads the same `STAGING_*` names as the workflow and creates everything listed in the next section, then prints a pass/fail checklist:

```bash
export STAGING_MONGO_URL="<staging Mongo URI>"  STAGING_MONGO_HOST="<its host>"  STAGING_DB_NAME=daylaborcrew_staging
export STAGING_SUPERADMIN_EMAIL=... STAGING_SUPERADMIN_PASSWORD=... STAGING_ADMIN_EMAIL=... STAGING_ADMIN_PASSWORD=...
# optional: STAGING_CONTRACTOR{1,2,3}_EMAIL/_PASSWORD, STAGING_CREW_EMAIL/_PASSWORD, STAGING_CREW2_EMAIL/_PASSWORD
# (defaults: contractorN@test.com / Test@1234, testcrew[2]@thedaylaborers.com / Crew@1234)
python backend/scripts/seed_staging.py --reset   # fresh DB; omit --reset to re-assert idempotently
python backend/scripts/seed_staging.py --check   # verify only
```

It refuses to run unless the DB name and Mongo host carry a staging/stage/stg/test marker (use `--allow-localhost` only for a local rehearsal), rejects emails the login API would refuse (e.g. `*.test` domains) and passwords that fail the password policy. Staff passwords must contain upper/lower case, a number and a symbol. If you use non-default contractor/crew emails, set `SEED_DEMO_ACCOUNTS=false` on the staging API so its startup doesn't add the default demo accounts too.

## Seed-data prerequisites

Seed the staging database with the seven accounts above and the minimum fixtures expected by the legacy tests:
- At least three demo jobs for each contractor test account, including Atlanta-area coordinates.
- At least three Atlanta-area jobs available to the crew account.
- An in-progress `Drywall Hanging` job assigned to the `CREW2` test account.
- The `TEST20` coupon and plan settings used by the legacy subscription/coupon assertions.
- A disposable `CONTRACTOR1` account whose verified-contractor flag can be revoked and restored by the integration test.

Use a fresh or resettable staging dataset. The legacy integration suite intentionally writes job records, updates admin fee settings, revokes/restores contractor verification, and creates pending payment/session records. Before that suite, the manual workflow now runs `scripts/validate_staging_token_billing.py` and `tests/test_token_billing_staging.py`. The token suite uses generated, uniquely namespaced fixture IDs and cleans its wallet, reservation, ledger, activity, user, and billing-payment records in `finally` blocks; it tests concurrent reservation spending, commit/release reconciliation after interruption, monthly Free grant deduplication, legacy credit conversion idempotency, signed Stripe webhook redelivery, and PayPal capture fulfillment idempotency. The additional payment regression suites (`test_iteration8_payments_e2e.py` and `test_iteration9_payment_fixes.py`) also create unpaid test checkouts. They now read API/web origins, seeded account credentials, and Mongo settings only from the guarded `STAGING_*` configuration; run them only from the manual staging workflow, never against the shared preview. Do not overlap these suites with manual testers or other runs.

## Run

After the environment and seed data are configured, open Actions → **Legacy Backend Integration Tests (Staging)** → Run workflow, choose the branch `feat/token-wallet-foundation`, check `confirm_staging`, and start the run. The workflow runs the token-specific transaction/webhook checks first, then the legacy API integration suite, serially in the protected `staging` environment. The workflow is manual-only so normal pushes do not attempt destructive integration tests before staging has been provisioned.
