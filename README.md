# KOL Desk

A private creator CRM for a Bangladesh outreach team. It keeps creator lists, outreach, offers, collaborations, payments, attendance, blockers, and reports in one place.

## Run locally

Use Python 3.12. The local demo uses SQLite. Render uses PostgreSQL.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export CRM_DEBUG=1
python manage.py migrate
python manage.py seed_demo --password 'Choose-Your-Local-Password'
python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in as `demo-staff` with the password you chose. To test administration, create a superuser with `python manage.py createsuperuser` and issue their authenticator key with `python manage.py setup_mfa YOUR_USERNAME`. Add that key to a TOTP authenticator before signing in. Keep it private. The app reads environment variables directly. Load a local .env into your shell if you use one.

Run `CRM_DEBUG=1 python manage.py test` to verify the application. Set a test PostgreSQL URL in `DATABASE_URL` to run the suite on PostgreSQL. Django would need permission to create a separate test database. Use a development PostgreSQL instance for that test.

## Deploy from a private GitHub repository to Render

1. Extract this project and create an empty **private** GitHub repository. Initialize Git in the extracted project, add and commit the source, set your private repository as origin, and push.
2. In Render, create a Blueprint from that repository and branch. Review the selected region and paid service/database tiers before deploying. The supplied Blueprint links PostgreSQL using its internal URL, generates a secret key, and disables public database networking.
3. Render builds dependencies and static assets, runs migrations before deploying, and probes `/health`. Use the web service shell to run `python manage.py createsuperuser` and `python manage.py setup_mfa YOUR_USERNAME`. Sign in on your Render HTTPS URL. For a custom domain, add it to `ALLOWED_HOSTS` (comma-separated).
4. Create staff accounts in **Users**. Provide initial credentials securely and enroll managers and finance staff with the displayed authenticator key. Admins can reset passwords and deactivate staff. Use the private service shell to rotate an authenticator key.
5. Have an admin open **Imports**, upload the workbook over HTTPS, and review the preview before committing. Assign imported creators to staff as appropriate. Import against a staging database first to see how the workbook maps.

Example Git commands, from the extracted project:

```sh
git init -b main
git add .
git commit -m "Build KOL Desk CRM"
git remote add origin YOUR_PRIVATE_GITHUB_REPOSITORY_URL
git push -u origin main
```

The Django folder is `crm`. The Render config and requirements belong at the repository root. Keep `CRM_DEBUG` disabled on Render. Leave the original Excel outside the repository.

## Excel mapping and review

| Source | Destination |
| --- | --- |
| Master Database | Creator, social profile metrics, attribution, raw contact values, and dated historical contact records |
| Outreach Pipeline | Historical activities, quotes, and confirmed outcomes where they link to a unique profile |
| TOP KOL Confirmation, INFO | Historical activities and outcomes when they can be matched, with all scanned source rows retained |
| Daily Update Tracker, Weekly Friday Report | Historical source rows stored separately from live CRM totals |
| Setup, WhatsApp Test | Reference lists and WhatsApp message template |
| Dashboard, KOL Negotiation Rates, Rate Card Comparison | Recomputed in the CRM from structured data |
| Confirmed KOL Rate Card | Recomputed ranking, with mapped visible and cached workbook values stored for reconciliation |

A dry run creates an admin-only reconciliation batch. It does not create creator, outreach, quote, or deal records. On commit, safe new records are imported and review items remain visible. Some pending rows have a linked imported record when only one field requires verification. Review statuses and the raw workbook snapshot are available from the batch. Reviewers can correct linked creator data in the normal editor and record a resolution. Unmatched rows remain discoverable in the reconciliation queue. Record reversals are intentionally conservative where data is edited or has downstream dependencies. Take a database backup before large imports or batch reversals.

Source workbook formulas can contain cached spreadsheet-specific function results and fixed ranges. The CRM recomputes metrics from inputs, compares quotes by explicit deliverable basis, and ranks using database cohorts. Historical formula outputs are retained as references within the mapped ranges. Converted quotes and deal counts can differ from Excel when multiple rows describe the same profile, offer, or outcome. Confirm which creator or profile a reference link belongs to before merging it. All non-formula rows in the mapped source ranges are accounted for.

## Sharing and channel connections

The app records the opening action and displays a launch link for the channel. Staff record sent messages, calls, and replies from the outcome form. The initial build uses dialer, WhatsApp, Messenger, and saved-profile links. Provider-specific call and message syncing requires an authorized integration. The built-in group-safe report can be copied for WhatsApp or Telegram. For optional direct Telegram sharing, put a bot authorized to post in the team group, and set TELEGRAM_BOT_TOKEN and TELEGRAM_REPORT_CHAT_ID in Render. A staff member must confirm each send. A failed request can be retried. If delivery is unconfirmed, check the group before posting manually. The detailed report is separate from the group summary.

## Backup and recovery

Keep encrypted database snapshots in a restricted backup destination. Run `pg_dump --format=custom --no-owner --file=kol-desk.dump "$DATABASE_URL"` from a trusted host that can reach the database, such as an authorized Render shell. The dump contains personal records, authenticator enrollment data, and payment receipts. Keep it out of the Git repository. Use `pg_restore --no-owner --exit-on-error --dbname="$RESTORE_DATABASE_URL" kol-desk.dump` against a fresh staging database. Verify sign-in, record counts, receipts, and a sample report before switching an application to a restored database. Keep the source workbook in its secure original location.
