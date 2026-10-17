# WorkFinder

WorkFinder is a local job-search workspace for internships, traineeships, apprenticeships, and student placements in **Kraków**. It collects public listings, preserves every job it has seen, explains each match score, and tracks applications from save to offer or rejection.

Search accepts Polish and English role names, including intern, internship, trainee,
stażysta, staż, praktykant, praktyki, apprentice, working student, and student programmes.
The title, employment type, or seniority must identify the role as a training opportunity;
a mention of interns somewhere in a regular job's description does not qualify.
Ordinary junior, graduate, and senior jobs are excluded from discovery. Existing records
remain in SQLite, saved/ignored views, and application history. Excel imports preserve
their original records, while the dashboard and discovery lists use the internship scope.
There is no language filter on the listing itself. Kraków and on-site/hybrid restrictions
still apply.

The interface is server rendered with FastAPI and Jinja2. Data stays in a local SQLite database. No portal account, browser automation, or CAPTCHA bypass is used.

## What is included

- Dashboard with recent jobs, watchlist activity, search health, application counts, and skill demand
- Kraków-only ingestion; accepted spellings are Kraków, Krakow, and Cracow, and remote-only jobs are excluded
- Search runs through independent providers, with per-provider error and duration records
- Canonical jobs plus source occurrences, so duplicate URLs remain auditable
- Duplicate checks using source IDs, canonical URLs, normalized company/title/location, description fingerprints, and fuzzy title comparison
- Distinct requisition IDs from one provider stay separate even when internship titles are similar
- Deterministic 0–100 matching with a visible point breakdown and profile gaps
- Job filters, sorting, pagination, manual import, save, and ignore actions
- Application Kanban, detailed statuses, notes, next actions, CV version, and append-only activity history
- Progress history across all real applications, follow-up reminders, and daily database backups
- Per-offer search decisions: new, seen again, excluded, or failed to import
- Candidate profile, company watchlist, saved-search data model, and skill/category extraction
- 24 clearly marked demo jobs across realistic Kraków employers
- Alembic migration, CLI, Windows launchers, Ruff, and pytest coverage

## Quick start on Windows

The launcher creates a virtual environment, installs dependencies, initializes the database, and starts the server:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./run.ps1
```

Open <http://127.0.0.1:8000>. You can also double-click `run.bat`.

### Manual setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app.cli init
uvicorn app.main:app --reload
```

Python 3.12 or newer is recommended. On the first server start, WorkFinder adds the default profile, watchlist, CV names, saved searches, and demo data. Demo records carry a `DEMO` badge and can be removed under **Settings**.

## Daily use

1. Open the dashboard and click **Find internships**.
2. Review **Recently found**, sorted by match score.
3. Open a job to see the score breakdown, extracted skills, gaps, and application-preparation notes.
4. Open the original offer in a new browser tab.
5. Save the job, then update it to **Applied** after submitting on the employer's site.
6. Track follow-ups and interviews on the Applications board.

WorkFinder never submits an application for you.

**Found today** means first discovered by WorkFinder today, even if the employer posted
the role weeks earlier. Cards and offer details show the publication date separately.
Workday uses the detail API's absolute `startDate`; careers pages use the matching
`JobPosting.datePosted` structured data, `datePosted` markup, or clearly labelled
publication dates. Job-board creation dates are labelled **Source posted**.

After searches, WorkFinder checks saved offers still missing dates (up to 100 per batch,
with failed/empty lookups cached until the next UTC day). Use **Settings > Find missing
posting dates**, a job's **Check posting date** button, or `python -m app.cli refresh-dates`
to retry immediately. Checks use supported official careers/ATS pages and preserve the
source evidence and lookup date. Rechecks retain the earliest verified publication date.

Relative labels such as "29 days ago" appear as estimates; "30+ days ago" stays an
open-ended source claim. Neither is stored as an exact date. If the source does not
share a date or the page cannot be accessed, the publication date remains unknown.
**Posted today** and **Posted in last 7 days** filter by exact publication dates;
undated and estimated offers are excluded. Discovery and posting date filters apply
independently. Spreadsheet-added dates stay in notes, separate from publication dates.

## Commands

```powershell
python -m app.cli init
python -m app.cli search
python -m app.cli import-excel
python -m app.cli seed-demo
python -m app.cli remove-demo
python -m app.cli backup
python -m app.cli retry-excel
python -m app.cli refresh-dates
pytest
ruff check .
```

`search` uses the same import, normalization, duplicate, and matching pipeline as the web button.

## Excel connection

Set `EXCEL_JOBS_PATH` in `.env` to the workbook that contains your job list. Then use
**Settings → Import Excel now** or run `python -m app.cli import-excel`.

The importer reads the workbook without modifying it. It maps the Polish columns in
`Prace Iza.xlsx`, limits imported jobs to Kraków, creates application records for rows
marked `tak`, and safely deduplicates repeated imports. SQLite remains WorkFinder's
database because it stores application history, source occurrences, scores, and search
runs that do not fit safely in one spreadsheet.

Using **Mark applied** first commits the application, activity history, and optional Excel
sync task in one database transaction. It works without Excel configured. If a workbook
is configured, WorkFinder updates its row by canonical job link or adds a new row.
A locked, missing, or invalid workbook does not undo the application status: the task
stays in the database and can be retried under **Settings** or with `retry-excel`.

## Tracking progress

The **Applications** page shows real application totals, interview counts, and follow-ups
due today or overdue. Demo data is excluded from these totals. Marking a new application
as Applied suggests a follow-up seven days later; change or clear the date in the job form.
Closed processes are excluded from reminders.

**Progress history** records status transitions, notes (including edits and removal), CV
version changes, next-action dates, and successful Excel synchronization. Re-saving
unchanged fields does not add duplicate events. Each job also keeps its own timeline.

**Search runs → View offers and import decisions** preserves offer-level decisions and
links back to the matching job. A malformed offer is recorded and does not stop the rest
of the source. A search run is committed before network work begins, so an interrupted
search remains visible. Older searches retain their original provider totals.

## Architecture

```text
app/
  main.py              FastAPI setup and local security headers
  config.py            environment configuration
  database.py          SQLAlchemy engine and sessions
  models/              persistent domain model
  routes/              server-rendered pages and form actions
  scrapers/            common provider interface and public-feed adapters
  services/            matching, deduplication, search, seed, assistant
  templates/           Jinja2 product UI
  static/              responsive CSS and small Kanban behavior
alembic/                database migration
tests/                  focused domain and provider tests
data/                   local SQLite database (gitignored)
```

`Job` represents the canonical offer. `JobOccurrence` records each source URL and its first/last seen dates. Removing duplicates therefore never erases provenance. Search and provider-run rows retain market and reliability history. Applications and their activity rows preserve recruitment history.

## Database and migrations

Startup automatically applies versioned Alembic migrations to the configured database.
Existing databases from the earlier `create_all` startup are adopted without deleting
jobs, applications, or history. You can also apply migrations manually:

```powershell
alembic upgrade head
```

The default database is `data/workfinder.db`. Override it in `.env` using the format shown
in `.env.example`. SQLite uses foreign-key checks, full commit synchronization, and a
30-second lock timeout. Excel is an optional export, not the primary progress store.

A consistent online backup is attempted before startup migrations and once per UTC day
on progress saves and completed searches. Use **Settings → Create backup now** or
`python -m app.cli backup` for an additional snapshot. Backups are integrity-checked and
atomically saved in `data/backups/` (override with `BACKUP_DIR`). Existing snapshots are
kept. To restore, stop the server, preserve the current database, and copy the selected
snapshot to the database path. The next launch applies any missing migrations.

## Providers and limitations

| Provider | State | Method and limitation |
|---|---|---|
| Arbeitnow | Enabled | Public job-board API, filtered locally for job relevance and Kraków. Coverage depends on its feed. |
| BBH, Aptiv, State Street and Motorola Solutions | Enabled | Their official public Workday search endpoints, queried for Kraków and restricted to internship/training roles. |
| Shell, ABB and Aon | Manual watchlist | Official careers links are available on Companies; no automatic provider is claimed. |
| Hitachi, UBS, HID, HSBC, Alior, Capgemini, PMI, HEINEKEN and Cisco | Enabled | Conservative monitors of each official public careers result page. Dynamic sites can return zero parseable jobs while still recording a successful health check. |
| LinkedIn Jobs | Stub | No login scraping. LinkedIn can change markup and restrict automated access. Add jobs manually. |
| Pracuj.pl / Talent Days | Stub | No stable public API has been assumed. A dedicated adapter should be added only after confirming permitted public access. |
| EY, PwC, Deloitte, KPMG, Accenture | Stub | Career platforms differ by region and change vendors. Manual import remains available. |
| Banks and institutions in the requested watchlist | Stub | Stored as watched companies, without brittle HTML scrapers. |

A provider failure never stops other providers. Providers are fetched concurrently, so a
slow careers site does not hold up every other source. Settings and Search Runs show each
result and error. Empty responses from conservative monitors are marked **Unverified**
and cannot expire old jobs. Only a complete structured Workday scan can expire a job
after seven days without being seen; truncated scans and first-page feeds cannot do so.
All jobs and application history remain stored.

### Adding a provider

1. Add a class in `app/scrapers/` implementing `JobSource.search_jobs(SearchCriteria)`.
2. Return normalized `JobPosting` objects and plain text descriptions. Use only an official API, RSS feed, documented public endpoint, or stable public structured data.
3. Apply `relevant_to_krakow` so the source cannot broaden the location scope.
4. Register the class in `app/scrapers/registry.py`.
5. Add a parsing test with a saved, minimal response fixture or mocked request.

Do not add credentials for job portals to `.env`. WorkFinder deliberately avoids login-protected acquisition.

## Matching and configuration

The matcher starts from a small base score and applies explicit weights for internship/entry level, finance, analytical work, reporting/planning, Excel, SQL, Python/Power BI, and Kraków availability. It subtracts points for seniority, extensive experience, sales/support focus, and unrelated engineering roles. Every applied rule is stored with the job.

Edit your skills, technologies, roles, industries, schedule, and work model under **Profile**. Saving recalculates every job. Kraków is intentionally fixed as the only location. Saved-search keywords, negative keywords, and ignore rules are editable under **Settings**. Matching weights live in `app/services/matching.py` so each point remains easy to audit.

The Application Assistant is deterministic and implements a separate interface. It can later be replaced by a local or hosted language-model adapter without changing job tracking.

## Windows Task Scheduler

1. Open **Task Scheduler** and choose **Create Basic Task**.
2. Set a daily trigger.
3. Choose **Start a program**.
4. Program: `C:\Projects\WorkFinder\.venv\Scripts\python.exe`
5. Arguments: `-m app.cli search`
6. Start in: `C:\Projects\WorkFinder`

Run the command manually once before scheduling it. The web server does not need to be open for CLI searches.

## Security and local operation

- URLs are limited to HTTP/HTTPS and tracking parameters are removed from canonical URLs.
- Provider HTML is converted to plain text. Jinja2 escapes rendered text.
- SQLAlchemy uses bound parameters rather than string-built SQL.
- External links use `noopener noreferrer`.
- Job portal credentials are never requested or stored.
- The app binds to Uvicorn's local default unless you explicitly change it.

This is a single-user local tool and has no login screen. Do not expose it directly to the public internet.

## Current boundaries

Advanced market charts, custom application status definitions, distance-radius geocoding, provider editing in the UI, and an LLM adapter are prepared for by the data/service structure but are not part of this first release. Salary filtering becomes meaningful only when feeds provide structured salary values. The core daily workflow, history, matching, duplicate handling, application tracking, and skill demand view are fully usable now.
