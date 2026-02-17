# Usage Tracking Filter for Open WebUI

A token usage tracking and rate limiting system for [Open WebUI](https://github.com/open-webui/open-webui) with group-based quotas and PostgreSQL persistence.

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)

## ✨ Features

- **Group-Based Limits**: Define daily/monthly token limits per user group (freemium, pro, enterprise)
- **Zero PII Storage**: Only stores Open WebUI user UUIDs - no emails or names
- **PostgreSQL Backend**: Reliable, persistent usage tracking
- **Auto-Schema Creation**: Tables, views, and functions created automatically on first run
- **Graceful Blocking**: Friendly messages when limits are reached
- **Usage Analytics**: Query historical usage patterns per user or group
- **Pipeline Compatible**: Works with any pipeline or model in Open WebUI

## 📁 Project Structure

```
openwebui-usage-tracking-filter/
├── README.md                    # This file
├── filter/
│   ├── usage_tracking_filter.py      # Main filter code
│   └── requirements.txt              # Python dependencies
├── docker/
│   └── docker-compose.usage.yml      # PostgreSQL container (or use existing)
└── docs/
    ├── SETUP.md                      # Full installation guide
    └── ADMIN_GUIDE.md                # Managing users and groups
```

## 🚀 Quick Start

The filter is **table-aware** and auto-creates all schema on first run!

### Option A: Use Existing PostgreSQL (Recommended)

If you're already running PostgreSQL for LangGraph Memory Filter, just install the filter with the same connection credentials. Schema will be created automatically.

### Option B: Fresh PostgreSQL Instance

```bash
cd docker
docker-compose -f docker-compose.usage.yml up -d
```

### Install the Filter

1. Go to Open WebUI **Admin Panel** → **Functions**
2. Click **"+ Add Function"**
3. Paste the contents of `filter/usage_tracking_filter.py`
4. Save and enable the filter
5. **Important**: Set priority lower than other filters (e.g., 5) so it runs first

### Configure

In the filter's Valves:
- Set PostgreSQL connection details
- Set `default_group` to "freemium" (or your default tier)

On first request, the filter will:
1. Connect to PostgreSQL
2. Check if tables exist
3. **Auto-create all tables, views, and functions** if missing
4. Log: `✅ Usage tracking schema created successfully`

## 📊 Default Limits

| Group | Daily Limit | Monthly Limit |
|-------|-------------|---------------|
| freemium | 50,000 tokens | 1,000,000 tokens |
| pro | 500,000 tokens | 10,000,000 tokens |
| enterprise | unlimited | unlimited |

Customize by updating rows in `usage_limits` table.

## 🔧 Managing Users

### Assign User to Group

```sql
INSERT INTO user_groups (user_id, group_name, assigned_by)
VALUES ('user-uuid-here', 'pro', 'admin')
ON CONFLICT (user_id) DO UPDATE SET 
  group_name = EXCLUDED.group_name,
  assigned_at = NOW();
```

### Check User's Usage

```sql
SELECT * FROM usage_summary WHERE user_id = 'user-uuid-here';
```

### View All Users Near Limits

```sql
SELECT * FROM users_near_limit;
```

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [SETUP.md](docs/SETUP.md) | Complete installation and setup guide |
| [ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md) | User and group management |

## 🔒 Privacy

This filter stores **only**:
- Open WebUI user UUIDs (no email, name, or other PII)
- Token counts per request
- Timestamps

No conversation content or personal information is stored.

## License

MIT License - See LICENSE file for details.
