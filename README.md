# EGLE Air Emissions Tracker — PTI 183-15

Replaces the monthly Excel process for VOC/HAP compliance reporting. Operators log
material use as it happens (in pounds off the scale or in gallons); the app holds the
product EDS library and generates the complete Aggregate Emissions Report workbook —
all 8 tabs in the existing v10 layout — on demand.

## Verified calculation methodology

Reverse-engineered from the filed reports and verified against January 2024:

| Quantity | Formula | Parity vs filed report |
|---|---|---|
| Gallons | pounds logged ÷ product density (lbs/gal) | exact, row-for-row |
| VOC tons per line | Σ gal × VOC lbs/gal ÷ 2000 | exact to 7 decimals |
| Per-CAS lbs | Σ gal × EDS content lbs/gal (= density × wt fraction unless EDS-revised) | exact once post-filing EDS revisions accounted for (DBE matched to 5 decimals) |
| Aggregate HAP tons | Σ gal × EDS HAP lbs/gal ÷ 2000 | 0.04% (EDS drift) |
| Isobutyl acetate | gal × IBA lbs/gal × (8 ÷ shift hours), vs 153.6 lbs/8-hr | exact |
| 12-mo rolling | true trailing 12-month sum | (v10's early-year formulas had range quirks; this app always uses the true window) |

History Jan 2019 – Jun 2026 is imported verbatim from Aggregate Emissions Report v10
and reported unchanged ("frozen"). Months after June 2026 are computed from logged usage.

## Running locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000
```

First run creates `voc_tracker.db` (SQLite) and seeds the product library, chemical
breakdowns, EDS content values, and all frozen history from `seed_data/seed.json`.

## Deploying to Azure

1. **App Service** (Linux, Python 3.11+). Startup command:
   `gunicorn -w 2 -k uvicorn.workers.UvicornWorker app.main:app`
2. **Database** — Azure SQL. Set app setting `DATABASE_URL` to
   `mssql+pyodbc://<user>:<pass>@<server>.database.windows.net/<db>?driver=ODBC+Driver+18+for+SQL+Server`
   and uncomment `pyodbc` in requirements.txt. (SQLite works for a single-instance
   pilot but App Service local storage is not durable — use Azure SQL for production.)
3. **Sign-in** — enable App Service Authentication (Easy Auth) with Microsoft Entra ID,
   "Require authentication". No code changes needed: the app reads the signed-in user
   from the `X-MS-CLIENT-PRINCIPAL-NAME` header and stamps it on every entry and void
   for the audit trail.
4. Upload `seed_data/seed.json` with the code (it ships in the repo/zip).

## Month-end procedure (new)

1. Month review tab → check entries; void any duplicates/errors with a reason
   (replaces the old "delete redundant rows" cleanup — nothing is ever deleted).
2. Reports tab → pick the month → Generate workbook.
3. Attach/submit per the existing EGLE work instruction. MAERS annual entry,
   SEMCO gas readings, and stack testing are unchanged.

## Notes for the environmental consultant

- The v10 workbook's CAS-specific **Cumene monthly** column appears to divide by 2000
  twice (it disagrees with the individual-HAP cumene column by exactly ×2000). This app
  reports cumene monthly in tons consistently in both places.
- Early-2019 rolling formulas in v10 summed ranges including header rows; harmless in
  Excel (text ignored) but the first 11 months' "rolling" values were year-to-date.
  This app uses a true trailing 12-month window everywhere.
- Product EDS revisions: per-CAS content for cumene, dibasic esters, and ethylbenzene
  follows the hand-entered Material Content values (EDS authority). When a supplier
  issues a revised EDS, update the product record; already-filed frozen months are
  never recomputed.

## API surface

- `GET/POST/PUT /api/products` — EDS library with chemical breakdowns
- `POST /api/usage`, `GET /api/usage?year&month`, `PUT /api/usage/{id}`,
  `POST /api/usage/{id}/void` — usage logging with full audit trail
- `GET /api/summary?year&month` — monthly + rolling position vs permit limits
- `GET /api/report/aggregate?year&month` — the 8-tab workbook download
