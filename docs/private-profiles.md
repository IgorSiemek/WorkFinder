# Private Telegram profiles

The original shared workspace remains at `data/workfinder.db` and `data/config.json`.
The two existing Telegram accounts continue sharing it. No database migration, invitation,
JSON change or new Telegram bot is required for those accounts.

Each new profile has a separate directory under `data/profiles/KEY/`, containing its own
SQLite database, JSON configuration, backups, CV and employer browser sessions. Job IDs,
application statuses, ignored offers, applicant details and notifications are private to that
workspace. The same public offer may exist independently in several profiles.

One Telegram poller routes updates by the authenticated private-chat account ID. New accounts
cannot read the original workspace. A single-use invitation binds one Telegram account to one
new profile; existing shared-workspace accounts cannot consume those invitations. Invitations
expire after 24 hours and only their SHA-256 digests are stored.

## Requirements JSON

Start with [`config.profile.example.json`](../config.profile.example.json). It is an illustrative
developer profile, not a real person's requirements. Change the name, skills, sources and
`job_scope` before importing it. Do not replace the original `data/config.json`.

`job_scope` supports:

| Field | Meaning |
| --- | --- |
| `role_type` | `internships`, `regular` (excludes internships), or `all` |
| `titles` | Accept a title containing any of these case-insensitive phrases; empty means all |
| `excluded_titles` | Reject a title containing any of these phrases |
| `locations` | Accepted cities; Kraków/Krakow/Cracow and Warszawa/Warsaw are aliases |
| `remote_allowed` | Also accept remote-only roles; remote searches may include other countries |

The personal scorer uses the configured roles and candidate skills instead of the original
finance-internship scoring rules. `minimum_score` controls lists and notifications as before.
`searches[].negative_keywords` can additionally hide unwanted terms from descriptions.
Experience requirements and salary are not hard filters; use explicit title exclusions and
review job descriptions.

Personal profiles currently support the existing **Workday employer feeds and Arbeitnow**.
Provide a nonempty `sources` list. Other adapters remain available to the original profile;
they intentionally reject use in a new configurable profile until their broader search has
been implemented and verified. Workday scans have a 200-result limit per search term and mark
incomplete scans accordingly. Arbeitnow remains a limited first-page feed. These sources do
not cover the entire job market or guarantee every matching vacancy.

## Create a profile

Local example:

```powershell
.\.venv\Scripts\python.exe -m app.cli profile-create --profile friend --json friend.json --bot-username WorkFinder3000BOT
```

On the production VM, place the supplied JSON in
`/opt/workfinder/data/profile-friend.json`, owned by UID/GID `10001:10001`, mode `600`.
From `/opt/workfinder`, run:

```sh
export APP_BUILD_REF="$(git rev-parse HEAD)"
docker compose exec -T workfinder python -m app.cli profile-create --profile friend --json /app/data/profile-friend.json --bot-username WorkFinder3000BOT
```

The command prints a private Telegram link and `/start profile_CODE`. Send the link to the
intended person. They open it in a private chat with the existing bot. No access is granted
until they redeem the code. An existing profile or data directory is never overwritten.

After creating the **first** private profile, restart the bot once to enable the router:

```sh
docker compose restart workfinder
```

Subsequent profiles are discovered automatically. The original profile's data and settings
stay in place. In the new profile, `/profile` shows its name; `/new`, `/search`, `/setup`,
`/uploadcv`, saved offers and application forms work within that person's workspace.
The original `/invite` command still means shared access to the original profile; use
`profile-create` for a separate person. Private profiles do not offer shared invitations.

To replace an expired or unredeemed invitation:

```sh
docker compose exec -T workfinder python -m app.cli profile-invite --profile friend --bot-username WorkFinder3000BOT
```

This invalidates previous unredeemed codes. A bound profile cannot be rebound through a new
invitation. List the profile bindings with `python -m app.cli profile-list`.

## Update requirements and backups

Edit only `data/profiles/KEY/config.json`, keep mode `600`, and ask that person to use `/reload`.
Invalid JSON or unsupported sources are rejected before applying changes. Changing requirements
does not delete application history. Pending job alerts are checked against the latest scope.
Personal profiles cannot write to the original Excel workbook.

Each profile's `/backup` creates a consistent snapshot in its own `backups` directory. Include
the entire `data/profiles` directory in host backups, including the private registry and JSON
files. For a restore, stop the bot, restore the matching database/configuration and restart.
The original backup process and database remain unchanged.

Routing is opt-in: when `data/profiles/registry.sqlite3` does not exist at startup, the original
single-workspace runtime is used. Existing accounts also bypass private-registry lookups when
routing is enabled. A private worker or registry failure cannot grant access to another profile.
