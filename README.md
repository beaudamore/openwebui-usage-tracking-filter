# Usage Tracking Filter for Open WebUI

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![OpenWebUI](https://img.shields.io/badge/Open_WebUI-Filter-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Backend-blue?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Zero PII](https://img.shields.io/badge/Zero_PII-Storage-brightgreen)

Per-user token quotas for [Open WebUI](https://github.com/open-webui/open-webui). Every user belongs to a tier (freemium, pro, enterprise, or your own). Each tier has a daily and a monthly token budget. The filter records every response's token usage in PostgreSQL, shows users where they stand, warns them as they approach a limit, and blocks new requests once a limit is reached.

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [What the Filter Creates (and What It Does Not)](#what-the-filter-creates-and-what-it-does-not)
- [Installation](#installation)
- [Configuration (Valves)](#configuration-valves)
- [Tiers and Limits](#tiers-and-limits)
- [Managing Users](#managing-users)
- [What Users See](#what-users-see)
- [Token Counting](#token-counting)
- [Database Reference](#database-reference)
- [Admin SQL Cookbook](#admin-sql-cookbook)
- [Running PostgreSQL for This Filter](#running-postgresql-for-this-filter)
- [Upgrading](#upgrading)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Compatibility](#compatibility)
- [Privacy](#privacy)
- [Project Structure](#project-structure)
- [Changelog](#changelog)
- [License](#license)

## Features

- **Tier-based quotas**: daily and monthly token limits per tier, with `-1` meaning unlimited.
- **Hard blocking**: when a user is over a limit, the request is stopped before it reaches the model and the user sees a clear explanation.
- **Live usage status**: each request shows a status line such as `📊 Usage: 12.3K/1000.0K today (1%) • 240.1K/10000.0K month (2%)`. Users can hide it for themselves.
- **Approach warnings**: when a user crosses a configurable percentage of a limit, a warning is appended to the assistant's reply.
- **Admin bypass**: admins are tracked but never blocked (configurable).
- **Configurable default tier**: the `default_group` valve decides which tier unassigned users get and are enrolled into.
- **Zero PII**: the database holds Open WebUI user UUIDs, token counts, model ids, chat ids, and timestamps. No names, emails, or message content.
- **Self-installing schema**: tables, views, and helper functions are created on the first request if they are missing. Views and functions are refreshed on every start, so upgrading the filter needs no manual migration.
- **Fails open**: if PostgreSQL is unreachable, requests are allowed through and the problem is logged. Users are never locked out by an outage of the tracking database.
- **Non-blocking**: database calls run in a worker thread so they never stall Open WebUI's event loop.
- **Provider agnostic**: works with any model Open WebUI can talk to, as long as the provider reports token usage.

## How It Works

The filter is a standard Open WebUI **Function** of type *filter*. Open WebUI calls it twice per chat turn.

### Inlet (before the model is called)

1. Reads the user id and role from the request context.
2. Connects to PostgreSQL on first use, creates the schema if it is missing, and refreshes the views and functions.
3. Looks up the user's tier, today's usage, and this month's usage with one database call.
4. Emits a status line showing current usage.
5. If the user is over the daily or monthly limit, and blocking is enabled, and the user is not a bypassed admin, the filter raises an exception. Open WebUI stops the request and shows the exception text to the user as the chat error. The model is never called and no tokens are consumed.

### Outlet (after the model has responded)

1. Reads the token usage that Open WebUI attached to the assistant message.
2. Inserts one row into `usage_records` and, if the user has no tier yet, enrols them in the `default_group` tier.
3. Re-reads the user's status. If they are now over a limit or past the warning threshold, appends a short usage notice to the end of the assistant's reply. Open WebUI persists the edited reply and pushes it to the browser.

Both phases wrap their work in try/except and fail open. A database error never blocks a request.

## Requirements

| Requirement | Notes |
| --- | --- |
| Open WebUI | Validated against **0.11.4**. See [Compatibility](#compatibility). |
| PostgreSQL | 12 or newer. An existing server **and** an existing database that the filter can connect to. |
| Database privileges | The connection user needs `CREATE` on the database (to create tables, views, and functions) and `SELECT`, `INSERT`, `UPDATE` afterwards. |
| Python packages | `psycopg[binary]` and `psycopg-pool`. Declared in the filter's frontmatter and installed automatically by Open WebUI on first load. |
| Open WebUI setting | `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` must be left at its default of `true`, and the instance must not be in `OFFLINE_MODE`, for automatic package installation. Otherwise install the two packages into the Open WebUI Python environment yourself. |
| Network | The Open WebUI backend must be able to reach the PostgreSQL host and port. In Docker, put both containers on the same network and use the container name as the host. |

## What the Filter Creates (and What It Does Not)

### Created automatically, inside your existing database

On the first request, if the `usage_limits` table is missing:

- Tables: `usage_limits`, `user_groups`, `usage_records`, with indexes
- Seed rows: the three default tiers in `usage_limits`

On every initialization (first request after Open WebUI starts or the function is re-enabled):

- Views: `usage_daily`, `usage_monthly`, `usage_summary`, `users_near_limit`
- Functions: `get_user_usage_status`, `record_usage`, `cleanup_old_usage_records`

These are applied with `CREATE OR REPLACE`, so they are safe to re-run and always match the filter version you have installed. Tables and seed rows are never touched again, so your edits to tiers survive restarts.

### Not created by the filter

- The PostgreSQL server.
- The database itself. If `postgres_database` does not exist, the connection fails and the filter logs `Failed to initialize` and fails open.
- The database user or its privileges.
- Any user-to-tier assignments other than the automatic enrolment into `default_group`.

The schema check is by table name: if a table called `usage_limits` is visible in `information_schema.tables`, the filter assumes the tables are present. If you drop only some tables, drop `usage_limits` as well so the filter recreates them all.

The filter can share a database with other tools. It only touches the objects listed above.

## Installation

1. Make sure PostgreSQL is running and the target database exists. See [Running PostgreSQL for This Filter](#running-postgresql-for-this-filter) if you need one.
2. In Open WebUI open **Admin Panel → Functions**.
3. Click **+** to add a new function.
4. Paste the full contents of [`filter/usage_tracking_filter.py`](filter/usage_tracking_filter.py) into the editor. Give it a name and description, then **Save**. Open WebUI installs the two Python packages at this point. Watch the backend log if it seems slow.
5. Enable the function with its toggle.
6. Open the function's **Valves** (gear icon) and set the PostgreSQL connection details. See [Configuration](#configuration-valves).
7. Decide where the filter applies:
   - **Global**: open the function's menu and turn on **Global**. Every model is tracked.
   - **Per model**: **Admin Panel → Models → (model) → Filters**, tick this filter.
8. Send a chat message. The backend log should show:

   ```text
   [Usage Tracking] [INFO] Connecting to PostgreSQL at <host>:<port>
   [Usage Tracking] [INFO] Creating usage tracking tables...
   [Usage Tracking] [INFO] ✅ Usage tracking schema created successfully
   [Usage Tracking] [INFO] Usage tracking initialized successfully
   ```

   and, after the response finishes, `Recorded <n> tokens for user <uuid-prefix>...`.

### Filter order

Open WebUI runs filters in ascending `priority`. This filter defaults to `5` so that the quota check runs before filters that add context, run RAG, or call other services. If you have filters that cost tokens or make external calls, give them a higher number than this one so a blocked user does not trigger them.

## Configuration (Valves)

All settings live in the function's Valves panel. Behaviour settings take effect on the next request. Connection settings are read when the filter first connects; after changing them, disable and re-enable the function (or restart the backend) so a new connection pool is created.

| Valve | Default | Description |
| --- | --- | --- |
| `priority` | `5` | Filter execution order. Lower runs first. Keep this below your other filters. |
| `postgres_host` | `langgraph-postgres` | PostgreSQL hostname or container name. |
| `postgres_port` | `5432` | PostgreSQL port. |
| `postgres_database` | `langgraph_memory` | Database to use. **Must already exist.** |
| `postgres_user` | `langgraph` | Database user. |
| `postgres_password` | `langgraph_password_change_me` | Database password. Change it. Stored in Open WebUI's database like any other valve. |
| `default_group` | `freemium` | Tier for users with no row in `user_groups`. New users are enrolled into it on their first recorded response. Must match a `group_name` in `usage_limits`; if it does not, the filter logs a warning, unassigned users get fallback limits of 1M/day and 10M/month, and nobody is auto-enrolled. |
| `enable_blocking` | `true` | When `true`, users over a limit are blocked. When `false`, the filter only logs a warning and appends the usage notice to replies. Useful for a dry run. |
| `show_usage_status` | `true` | Show the per-request usage status line. Users can additionally hide it for themselves (see below). |
| `warn_at_percent` | `80` | Percentage of a daily or monthly limit at which the status icon switches to ⚠️ and a warning is appended to the reply. |
| `admin_bypass` | `true` | Users with the Open WebUI `admin` role are tracked and see their usage but are never blocked. |
| `debug_mode` | `false` | Log every inlet and outlet step. Verbose. |

### Per-user settings (UserValves)

Users can open the function from the chat's filter menu and set:

| UserValve | Default | Description |
| --- | --- | --- |
| `show_usage_status` | `true` | Hide the usage status line on their own messages. Limit warnings appended to replies and blocking are unaffected. |

Nothing a user can set weakens enforcement.

## Tiers and Limits

Tiers are **rows in the `usage_limits` table**. The filter seeds three on first run:

| Tier | Daily limit | Monthly limit | Notes |
| --- | --- | --- | --- |
| `freemium` | 1,000,000 tokens | 10,000,000 tokens | Default `default_group` |
| `pro` | 5,000,000 tokens | 100,000,000 tokens | |
| `enterprise` | unlimited (`-1`) | unlimited (`-1`) | |

Rules:

- A limit of `-1` means no limit for that period.
- A user is blocked when `tokens_used >= limit` for either period.
- Both prompt and completion tokens count toward the limit.
- Seeding uses `ON CONFLICT DO NOTHING` and runs only when the tables are first created, so editing or deleting tiers in the database is permanent.
- Any user not present in `user_groups` is treated as belonging to `default_group` and is inserted there the first time a response is recorded for them.
- You can add as many tiers as you like. A tier name is up to 50 characters.

Limits and tier membership are changed with SQL. There is no admin UI for this. See the [Admin SQL Cookbook](#admin-sql-cookbook).

### When limits reset

- **Daily**: `recorded_at` is compared to `CURRENT_DATE` **in the PostgreSQL server's session timezone**. If your database runs in UTC (the Docker image default), the day rolls over at midnight UTC and the message shown to users is accurate. If your database uses another timezone, the day rolls over at local midnight and you may want to edit the `reset_info` text in the filter.
- **Monthly**: the first of the month, same timezone rule.

## Managing Users

### Finding a user's UUID

- **Admin Panel → Users**, click a user. The id is shown in the user details. It is also the value that appears in this filter's log lines (first eight characters).
- Or query Open WebUI's own database:

  ```sql
  SELECT id, name, email FROM "user" ORDER BY created_at DESC;
  ```

  This query runs against Open WebUI's database, not the usage database, unless the two are the same.

### Assign or move a user

```sql
INSERT INTO user_groups (user_id, group_name, assigned_by, notes)
VALUES ('user-uuid-here', 'pro', 'admin', 'Upgraded 2026-10-07')
ON CONFLICT (user_id) DO UPDATE
  SET group_name  = EXCLUDED.group_name,
      assigned_at = NOW(),
      assigned_by = EXCLUDED.assigned_by,
      notes       = EXCLUDED.notes;
```

The change applies on the user's next request.

### Change the tier new users get

Set the `default_group` valve to any tier name that exists in `usage_limits`, for example `enterprise` to make everyone unlimited unless assigned otherwise. Users already enrolled keep their current row; move them with the SQL above if needed.

## What Users See

**Normal request**, status line above the reply:

```text
📊 Usage: 12.3K/1000.0K today (1%) • 240.1K/10000.0K month (2%)
```

**Approaching a limit** (at or past `warn_at_percent`): the icon becomes ⚠️ and the reply ends with:

> ---
> ⚠️ **Approaching Usage Limit**
>
> - Today: 850.0K/1000.0K (85%)
> - This month: 2100.0K/10000.0K (21%)

**Just crossed a limit**: the reply that pushed them over still completes, and ends with a notice that the next request will be blocked.

**Blocked request**: the model is not called. The chat shows an error message:

> ⚠️ **Usage Limit Reached**
>
> You've reached your daily token limit for the **freemium** tier.
>
> - **Used:** 1,003,412 tokens
> - **Limit:** 1,000,000 tokens
> - **Resets:** midnight UTC
>
> Contact your administrator to upgrade your plan.

**Admins** see the status line and warnings but are never blocked while `admin_bypass` is on.

## Token Counting

The filter reads the `usage` object that Open WebUI attaches to each assistant message. Open WebUI normalizes usage from OpenAI, Ollama, llama.cpp, and Anthropic-style providers into `input_tokens`, `output_tokens`, and `total_tokens`. The filter prefers these normalized fields and falls back to `prompt_tokens` / `completion_tokens` (OpenAI) and `prompt_eval_count` / `eval_count` (Ollama).

Points worth knowing:

- **Tool calls**: when a single reply involves several model round trips (native function calling, tool loops), Open WebUI sums the normalized fields across all of them. The filter records the sum, so tool-heavy conversations are charged fully.
- **No usage, no record**: if the provider does not return usage, nothing is recorded and the user is effectively untracked for that turn. Most providers return usage for non-streaming requests. For streaming, Open WebUI asks OpenAI-compatible providers for usage via `stream_options`; some third-party proxies ignore this. Check `debug_mode` logs for `No usage data in response`.
- **What is stored per record**: user id, prompt tokens, completion tokens, total, model id, chat id, timestamp. Message content is never read or stored.
- **Task model calls** (title generation, tag generation, autocomplete) are not run through chat filters by Open WebUI and are not counted.

## Database Reference

### Tables

#### `usage_limits`

One row per tier.

| Column | Type | Notes |
| --- | --- | --- |
| `group_name` | `VARCHAR(50)` PK | Tier name |
| `daily_token_limit` | `BIGINT` | `-1` = unlimited |
| `monthly_token_limit` | `BIGINT` | `-1` = unlimited |
| `rate_limit_rpm` | `INT` | Reserved. Not enforced by the filter. |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | `updated_at` is not auto-maintained; set it yourself when editing. |
| `description` | `TEXT` | |

#### `user_groups`

One row per known user.

| Column | Type | Notes |
| --- | --- | --- |
| `user_id` | `VARCHAR(255)` PK | Open WebUI user UUID |
| `group_name` | `VARCHAR(50)` FK → `usage_limits` | |
| `assigned_at` | `TIMESTAMPTZ` | |
| `assigned_by` | `VARCHAR(255)` | Free text for your records |
| `notes` | `TEXT` | |

#### `usage_records`

One row per recorded response.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `BIGSERIAL` PK | |
| `user_id` | `VARCHAR(255)` | Indexed |
| `recorded_at` | `TIMESTAMPTZ` | Indexed, default `NOW()` |
| `prompt_tokens`, `completion_tokens`, `total_tokens` | `INT` | |
| `model_id` | `VARCHAR(255)` | Open WebUI model id |
| `pipeline_id` | `VARCHAR(255)` | Always `NULL` in the current version |
| `chat_id` | `VARCHAR(255)` | |
| `request_type` | `VARCHAR(50)` | Always `chat` in the current version |

### Views

| View | Purpose |
| --- | --- |
| `usage_daily` | Tokens and request count per user per day |
| `usage_monthly` | Tokens and request count per user per month |
| `usage_summary` | Each user in `user_groups` with tier, limits, today's and this month's usage, and percent used |
| `users_near_limit` | Rows of `usage_summary` above 80% on either period (the 80 is fixed in the view, independent of `warn_at_percent`) |

### Functions

| Function | Used by | Purpose |
| --- | --- | --- |
| `get_user_usage_status(user_id, default_group DEFAULT 'freemium')` | inlet, outlet | Returns tier, limits, usage, and over-limit flags in one row. Users with no `user_groups` row are evaluated against `default_group`. |
| `record_usage(user_id, prompt, completion, model_id, pipeline_id, chat_id, default_group DEFAULT 'freemium')` | outlet | Inserts a usage row. Enrols the user into `default_group` if they have no row and that tier exists. |
| `cleanup_old_usage_records(days_to_keep DEFAULT 90)` | you | Deletes records older than N days and returns the count. Not scheduled automatically. |

## Admin SQL Cookbook

Connect with `psql` or any client, for example:

```bash
docker exec -it <postgres-container> psql -U langgraph -d langgraph_memory
```

### Change a tier's limits

```sql
UPDATE usage_limits
SET daily_token_limit = 200000,
    monthly_token_limit = 3000000,
    updated_at = NOW()
WHERE group_name = 'freemium';
```

### Add a tier

```sql
INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description)
VALUES ('team', 20000000, 400000000, 'Team tier - 20M/day, 400M/month');
```

### Make a tier unlimited

```sql
UPDATE usage_limits SET daily_token_limit = -1, monthly_token_limit = -1 WHERE group_name = 'pro';
```

### Delete a tier

Move its users first; the foreign key will otherwise block the delete. Do not delete the tier named in `default_group`.

```sql
UPDATE user_groups SET group_name = 'freemium' WHERE group_name = 'team';
DELETE FROM usage_limits WHERE group_name = 'team';
```

### Who is in which tier

```sql
SELECT group_name, COUNT(*) FROM user_groups GROUP BY group_name ORDER BY 2 DESC;
```

### One user's current standing

```sql
SELECT * FROM get_user_usage_status('user-uuid-here', 'freemium');
```

### Everyone's standing

```sql
SELECT * FROM usage_summary ORDER BY monthly_percent_used DESC;
```

### Users near a limit

```sql
SELECT * FROM users_near_limit;
```

### Reset one user's usage for today

Deletes their records for the day.

```sql
DELETE FROM usage_records
WHERE user_id = 'user-uuid-here' AND DATE(recorded_at) = CURRENT_DATE;
```

### Usage by model this month

```sql
SELECT model_id, SUM(total_tokens) AS tokens, COUNT(*) AS requests
FROM usage_records
WHERE recorded_at >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY model_id ORDER BY tokens DESC;
```

### Daily totals for the last 30 days

```sql
SELECT usage_date, SUM(total_tokens) AS tokens, SUM(request_count) AS requests
FROM usage_daily
WHERE usage_date >= CURRENT_DATE - 30
GROUP BY usage_date ORDER BY usage_date;
```

### Purge records older than 90 days

```sql
SELECT cleanup_old_usage_records(90);
```

Schedule this with `pg_cron`, a host cron job, or run it by hand. The filter never deletes data on its own.

### Tear down everything the filter created

```sql
DROP VIEW IF EXISTS users_near_limit, usage_summary, usage_monthly, usage_daily;
DROP FUNCTION IF EXISTS get_user_usage_status(VARCHAR, VARCHAR);
DROP FUNCTION IF EXISTS record_usage(VARCHAR, INT, INT, VARCHAR, VARCHAR, VARCHAR, VARCHAR);
DROP FUNCTION IF EXISTS cleanup_old_usage_records(INT);
DROP TABLE IF EXISTS usage_records, user_groups, usage_limits;
```

The schema will be recreated on the next request.

## Running PostgreSQL for This Filter

If you do not already have PostgreSQL, this `docker-compose.yml` starts one with the filter's default credentials. **Change the password** here and in the valves.

```yaml
services:
  langgraph-postgres:
    image: postgres:16-alpine
    container_name: langgraph-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: langgraph
      POSTGRES_PASSWORD: langgraph_password_change_me
      POSTGRES_DB: langgraph_memory
      TZ: UTC
      PGTZ: UTC
    volumes:
      - usage_pgdata:/var/lib/postgresql/data
    networks:
      - openwebui

volumes:
  usage_pgdata:

networks:
  openwebui:
    external: true   # the network your Open WebUI container is on
```

`POSTGRES_DB` creates the database on first start, which is the one thing the filter cannot do for itself. Setting the timezone to UTC makes the "resets at midnight UTC" message accurate.

If Open WebUI runs on the host rather than in Docker, publish the port (`ports: ["5432:5432"]`) and set `postgres_host` to `localhost` or the host's IP.

## Upgrading

Paste the new filter code over the old one in **Admin Panel → Functions** and save. On the next request the filter re-applies its views and functions, so the database is upgraded automatically. Your tables, tiers, assignments, and usage history are untouched.

Upgrading from 1.0.0: the old one-argument `get_user_usage_status` and six-argument `record_usage` functions are dropped and replaced with versions that accept a default-group argument. If you wrote your own SQL against the old signatures, add the extra argument or rely on its default.

## Troubleshooting

Turn on `debug_mode` and watch the Open WebUI backend log. All filter lines are prefixed `[Usage Tracking]`.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Failed to initialize: connection failed` | Wrong host, port, or credentials. Database does not exist. Network not shared. | Verify with `psql` from inside the Open WebUI container. Create the database if missing. |
| `Failed to initialize: permission denied for schema public` | Database user cannot create objects | Grant `CREATE` on the database, or create the schema by hand as a superuser. |
| `default_group '...' is not defined in usage_limits` | The valve names a tier that does not exist | Fix the valve or add the tier. Until then unassigned users get 1M/day, 10M/month and are not enrolled. |
| Function fails to save or load, log mentions `psycopg` | Automatic pip install is disabled or offline | Install `psycopg[binary] psycopg-pool` into the Open WebUI environment, or enable `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`. |
| No status line appears | Filter not enabled, not global, and not attached to the model. Or the user turned off their `show_usage_status` UserValve. | Check the Functions toggle, the model's Filters list, and the user's valve. |
| `No usage data in response` | The provider did not return usage | Try a non-streaming request. Check the provider or proxy supports usage reporting. |
| Users are never blocked | `enable_blocking` is off, or the user is an admin with `admin_bypass` on, or their tier is `-1` | Check valves and `SELECT * FROM get_user_usage_status('uuid', 'freemium')`. |
| Blocked at the wrong hour | Database timezone is not UTC | Set `TZ`/`PGTZ` on the database, or accept local midnight and edit the message text. |
| Changed connection valves but still connecting to the old host | The pool is created once per load | Disable and re-enable the function, or restart Open WebUI. |
| Edited the table SQL in the filter but the database did not change | Tables are only created when `usage_limits` is absent | Apply the change with `ALTER TABLE`, or drop the tables and let the filter recreate them. Views and functions, by contrast, are refreshed automatically. |

## Known Limitations

- **Tiers and assignments are SQL only.** There is no admin UI and no valve for limits. Only the default tier is a valve.
- **`rate_limit_rpm`** exists in the schema but no requests-per-minute enforcement is implemented.
- **Blocking resolution is one request.** The request that crosses a limit completes. Blocking starts with the next one.
- **Schema detection is by table name only.** Any table named `usage_limits` in any schema of the database makes the filter assume the tables exist.
- **Daily reset follows the database timezone**, not the user's.
- **Usage is only as good as the provider's reporting.** Responses without a usage object are not counted.
- **Only chat completions are tracked.** Title, tag, and autocomplete task calls are not run through chat filters by Open WebUI.
- **Connection settings need a reload.** The pool is created once; changing host or credentials requires disabling and re-enabling the function.

## Compatibility

Validated against Open WebUI **0.11.4** (September 2026) by reading the filter dispatch, chat middleware, and usage normalization code. Specifically confirmed:

- Filters receive `__user__`, `__event_emitter__`, and `__request__` in both inlet and outlet, and `__user__["valves"]` carries the UserValves.
- Raising an exception from inlet stops the request and surfaces the message to the user.
- The outlet body carries the assistant message with Open WebUI's normalized `usage` object, `model`, and `chat_id`.
- Outlet filters are run once per response by the backend. The browser no longer triggers them, so there is no double counting.
- Content edits made in outlet are persisted and pushed to the browser.
- Frontmatter `requirements` are installed with pip, comma separated.

The frontmatter declares `required_open_webui_version: >= 0.5.0`. The exception-based blocking and the `prompt_tokens` / `eval_count` fallbacks work on older versions too, but only 0.11.4 has been checked line by line.

## Privacy

The usage database stores:

- Open WebUI user UUIDs
- Token counts per response
- Model id and chat id per response
- Timestamps
- Tier assignments and whatever you type into `assigned_by` and `notes`

It never stores names, emails, prompts, or responses. Chat ids are opaque identifiers; joining them to content requires access to Open WebUI's own database.

## Project Structure

```text
openwebui-usage-tracking-filter/
├── README.md                          # This file
├── LICENSE                            # MIT
├── .gitignore
└── filter/
    └── usage_tracking_filter.py       # The filter. Paste this into Open WebUI.
```

The schema SQL lives inside the filter so that a single paste installs everything.

## Changelog

### 1.0.1 (2026-10-07)

Validated against Open WebUI 0.11.4 and fixed what did not hold up.

- **Fixed blocking.** The inlet previously rewrote the message list to "block" a request, which did not stop Open WebUI from calling the model. It now raises an exception, which is the supported way to stop a request. The user sees the limit message as a chat error.
- **Fixed request body contamination.** The inlet no longer adds private keys to the request body. Those keys were forwarded to OpenAI-compatible providers, and the official OpenAI API rejects unknown parameters.
- **Fixed under-counting on tool-calling turns.** The outlet now prefers Open WebUI's cumulative `input_tokens` / `output_tokens` over the last-call-only `prompt_tokens` / `completion_tokens`.
- **`default_group` valve now works.** The SQL functions take the default tier as a parameter instead of hardcoding `freemium`. Enrolment is skipped, with a logged warning, if the valve names a tier that does not exist, so a typo cannot break usage recording.
- **Views and functions are re-applied on every start.** Upgrades no longer need manual SQL. Tables and seed tiers are still created only once.
- **UserValves changed.** The unused `enabled` field, which would have been a self-service quota bypass had it worked, is replaced by `show_usage_status`, a per-user toggle for the status line only.
- **Database calls no longer block the event loop.** They run in a worker thread. Initialization is guarded by a lock.
- **Status line renders as finished** instead of as an in-progress spinner.
- Added LICENSE. README rewritten; the previous version listed limits and files that did not match the code.

### 1.0.0 (2026-01-24)

- Initial release.

## License

[MIT](LICENSE). Copyright (c) 2026 Beau D'Amore.
