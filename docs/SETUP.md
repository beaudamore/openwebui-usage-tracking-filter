# Usage Tracking Filter Setup Guide

## Prerequisites

- Open WebUI >= 0.5.0
- PostgreSQL 14+ (can share with LangGraph Memory Filter)
- `psycopg[binary,pool]>=3.1.0` installed in Open WebUI container

## Installation

### Step 1: Database Setup

The filter is **table-aware** and will automatically create all required tables, views, and functions on first run. No manual SQL execution is required!

#### Option A: Using Existing PostgreSQL (Recommended)

If you're already running PostgreSQL for the LangGraph Memory Filter, simply configure the filter to point to it. The schema will be created automatically.

#### Option B: Fresh PostgreSQL Instance (Optional)

If you want a dedicated database:

```bash
cd docker
docker-compose -f docker-compose.usage.yml up -d
```

> **Note:** The `docker/init-postgres.sql` file is kept for reference and manual schema management, but is not required for normal operation.

### Step 2: Install Dependencies

```bash
# In your Open WebUI container
docker exec -it <open-webui-container> pip install "psycopg[binary,pool]>=3.1.0"
```

### Step 3: Install the Filter

1. Go to Open WebUI **Admin Panel** → **Functions**
2. Click **"+ Add Function"**
3. Set the type to **Filter**
4. Paste the contents of `filter/usage_tracking_filter.py`
5. Save

### Step 4: Configure Valves

Click the gear icon on your filter and set:

| Valve | Value | Notes |
|-------|-------|-------|
| `priority` | `5` | Must be LOWER than memory filter (10) |
| `postgres_host` | `langgraph-postgres` | Same as memory filter |
| `postgres_port` | `5432` | Same as memory filter |
| `postgres_database` | `langgraph_memory` | Same as memory filter |
| `postgres_user` | `langgraph` | Same as memory filter |
| `postgres_password` | `your-password` | Same as memory filter |
| `default_group` | `freemium` | Default tier for new users |
| `enable_blocking` | `true` | Set to false for logging-only mode |

### Step 5: Enable the Filter

Toggle the filter ON in the Functions panel.

On first request, the filter will:
1. Connect to PostgreSQL
2. Check if tables exist
3. **Auto-create the entire schema** if tables are missing
4. Log "✅ Usage tracking schema created successfully"

## Verifying Installation

1. Send a test message in Open WebUI
2. Check the logs for: `[Usage Tracking] [INFO] ✅ Usage tracking schema created successfully`
3. Check PostgreSQL for usage records:

```sql
SELECT * FROM usage_records ORDER BY recorded_at DESC LIMIT 10;
```

4. Check your usage summary:

```sql
SELECT * FROM usage_summary;
```

## Filter Priority Order

For proper operation, set priorities so filters run in this order:

1. **Usage Tracking** (priority: 5) - Block if over limit
2. **Memory Filter** (priority: 10) - Load/save memories
3. Other filters...
4. Pipeline processes request

## Troubleshooting

### Schema creation fails

Check PostgreSQL user permissions. The user needs CREATE TABLE privileges:
```sql
GRANT CREATE ON DATABASE langgraph_memory TO langgraph;
```

### Usage not being recorded

1. Check that the model/pipeline returns usage data
2. Enable `debug_mode` in valves to see detailed logs
3. Verify the `outlet()` method is being called

### Users not being blocked

1. Check `enable_blocking` is `true`
2. Verify user isn't an admin (admins bypass by default)
3. Check the user's group has limits set
