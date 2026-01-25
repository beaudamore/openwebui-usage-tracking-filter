# Admin Guide: Managing Users and Groups

## User Groups

Users are assigned to groups that define their token limits. By default:

| Group | Daily Limit | Monthly Limit |
|-------|-------------|---------------|
| freemium | 50,000 | 1,000,000 |
| pro | 500,000 | 10,000,000 |
| enterprise | unlimited | unlimited |

## Managing Groups

### Add a New Group

```sql
INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description)
VALUES ('premium', 200000, 5000000, 'Premium tier - 200k/day, 5M/month');
```

### Modify Group Limits

```sql
UPDATE usage_limits 
SET daily_token_limit = 100000, monthly_token_limit = 2000000
WHERE group_name = 'freemium';
```

### View All Groups

```sql
SELECT * FROM usage_limits;
```

## Managing Users

### Assign User to Group

You need the user's Open WebUI UUID (visible in the admin panel or database).

```sql
INSERT INTO user_groups (user_id, group_name, assigned_by, notes)
VALUES (
    'abc12345-1234-5678-9abc-def012345678',  -- User's UUID
    'pro',                                     -- Target group
    'admin',                                   -- Who assigned
    'Upgraded to pro on 2026-01-24'           -- Optional notes
)
ON CONFLICT (user_id) DO UPDATE SET 
    group_name = EXCLUDED.group_name,
    assigned_at = NOW(),
    assigned_by = EXCLUDED.assigned_by,
    notes = EXCLUDED.notes;
```

### Change User's Group

```sql
UPDATE user_groups 
SET group_name = 'enterprise', 
    assigned_at = NOW(),
    notes = 'Upgraded to enterprise'
WHERE user_id = 'abc12345-1234-5678-9abc-def012345678';
```

### Remove User from Groups (Reset to Default)

```sql
DELETE FROM user_groups WHERE user_id = 'abc12345-1234-5678-9abc-def012345678';
```

The user will fall back to the `default_group` configured in the filter (typically "freemium").

## Viewing Usage

### Single User's Status

```sql
SELECT * FROM usage_summary WHERE user_id = 'abc12345-...';
```

### All Users' Current Usage

```sql
SELECT * FROM usage_summary ORDER BY monthly_percent_used DESC;
```

### Users Approaching Limits (>80%)

```sql
SELECT * FROM users_near_limit;
```

### Daily Usage History

```sql
SELECT * FROM usage_daily 
WHERE user_id = 'abc12345-...' 
ORDER BY usage_date DESC 
LIMIT 30;
```

### Monthly Usage History

```sql
SELECT * FROM usage_monthly 
WHERE user_id = 'abc12345-...' 
ORDER BY usage_month DESC;
```

### Top Users by Monthly Usage

```sql
SELECT 
    user_id,
    SUM(total_tokens) as total_tokens,
    COUNT(*) as request_count
FROM usage_records
WHERE recorded_at >= DATE_TRUNC('month', CURRENT_DATE)
GROUP BY user_id
ORDER BY total_tokens DESC
LIMIT 20;
```

## Maintenance

### Clean Up Old Records

Keep 90 days of records (default):

```sql
SELECT cleanup_old_usage_records(90);
```

Keep 30 days:

```sql
SELECT cleanup_old_usage_records(30);
```

### Automate Cleanup with pg_cron

```sql
-- Run daily at 3 AM
SELECT cron.schedule('cleanup-usage', '0 3 * * *', 
    'SELECT cleanup_old_usage_records(90)');
```

## Bulk Operations

### Reset All Users' Monthly Counters

Usage automatically resets by date queries - no action needed.

### Export Usage Report

```sql
COPY (
    SELECT 
        ug.user_id,
        ug.group_name,
        us.tokens_today,
        us.tokens_this_month,
        us.daily_percent_used,
        us.monthly_percent_used
    FROM user_groups ug
    JOIN usage_summary us ON ug.user_id = us.user_id
    ORDER BY us.tokens_this_month DESC
) TO '/tmp/usage_report.csv' WITH CSV HEADER;
```

### Bulk Upgrade Users

```sql
-- Upgrade all users with >500k monthly usage to pro
INSERT INTO user_groups (user_id, group_name, assigned_by, notes)
SELECT 
    user_id, 
    'pro', 
    'auto-upgrade',
    'Auto-upgraded based on high usage'
FROM usage_monthly
WHERE usage_month = DATE_TRUNC('month', CURRENT_DATE)
  AND total_tokens > 500000
ON CONFLICT (user_id) DO UPDATE SET 
    group_name = 'pro',
    assigned_at = NOW(),
    notes = 'Auto-upgraded based on high usage';
```

## Security Notes

- **No PII stored**: Only UUIDs, never emails or names
- **UUIDs are opaque**: Map to users via Open WebUI's admin panel
- **Audit trail**: `assigned_at` and `assigned_by` track who made changes
