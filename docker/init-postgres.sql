-- Usage Tracking Schema for Open WebUI
-- Can be added to existing PostgreSQL instance (e.g., langgraph_memory)

-- ============================================================================
-- USAGE LIMITS: Group-based quota definitions
-- ============================================================================
CREATE TABLE IF NOT EXISTS usage_limits (
    group_name VARCHAR(50) PRIMARY KEY,
    daily_token_limit BIGINT NOT NULL DEFAULT 50000,
    monthly_token_limit BIGINT NOT NULL DEFAULT 1000000,
    rate_limit_rpm INT,  -- Optional: requests per minute
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT
);

-- Insert default tiers
INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description)
VALUES 
    ('freemium', 50000, 1000000, 'Free tier - 50k/day, 1M/month'),
    ('pro', 500000, 10000000, 'Pro tier - 500k/day, 10M/month'),
    ('enterprise', -1, -1, 'Enterprise tier - unlimited (-1 = no limit)')
ON CONFLICT (group_name) DO NOTHING;

-- ============================================================================
-- USER GROUPS: Maps user UUIDs to their group (no PII stored)
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_groups (
    user_id VARCHAR(255) PRIMARY KEY,  -- Open WebUI UUID only
    group_name VARCHAR(50) NOT NULL REFERENCES usage_limits(group_name) DEFAULT 'freemium',
    assigned_at TIMESTAMPTZ DEFAULT NOW(),
    assigned_by VARCHAR(255),  -- Admin who assigned (optional, can be null)
    notes TEXT  -- Admin notes (optional)
);

CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_name);

-- ============================================================================
-- USAGE RECORDS: Token usage log (the main tracking table)
-- ============================================================================
CREATE TABLE IF NOT EXISTS usage_records (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    model_id VARCHAR(255),  -- Which model was used
    pipeline_id VARCHAR(255),  -- Which pipeline (if any)
    chat_id VARCHAR(255),  -- Conversation ID (optional)
    request_type VARCHAR(50) DEFAULT 'chat'  -- chat, completion, embedding, etc.
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_usage_records_user ON usage_records(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_records_date ON usage_records(recorded_at);
CREATE INDEX IF NOT EXISTS idx_usage_records_user_date ON usage_records(user_id, recorded_at DESC);

-- Partition-friendly index for date range queries
CREATE INDEX IF NOT EXISTS idx_usage_records_daily ON usage_records(user_id, DATE(recorded_at));

-- ============================================================================
-- VIEWS: Convenient queries for common operations
-- ============================================================================

-- Daily usage per user
CREATE OR REPLACE VIEW usage_daily AS
SELECT 
    user_id,
    DATE(recorded_at) as usage_date,
    SUM(prompt_tokens) as prompt_tokens,
    SUM(completion_tokens) as completion_tokens,
    SUM(total_tokens) as total_tokens,
    COUNT(*) as request_count
FROM usage_records
GROUP BY user_id, DATE(recorded_at);

-- Monthly usage per user
CREATE OR REPLACE VIEW usage_monthly AS
SELECT 
    user_id,
    DATE_TRUNC('month', recorded_at) as usage_month,
    SUM(prompt_tokens) as prompt_tokens,
    SUM(completion_tokens) as completion_tokens,
    SUM(total_tokens) as total_tokens,
    COUNT(*) as request_count
FROM usage_records
GROUP BY user_id, DATE_TRUNC('month', recorded_at);

-- Current usage summary (today + this month)
CREATE OR REPLACE VIEW usage_summary AS
SELECT 
    ug.user_id,
    ug.group_name,
    ul.daily_token_limit,
    ul.monthly_token_limit,
    COALESCE(daily.total_tokens, 0) as tokens_today,
    COALESCE(monthly.total_tokens, 0) as tokens_this_month,
    CASE 
        WHEN ul.daily_token_limit = -1 THEN 100.0
        ELSE ROUND(COALESCE(daily.total_tokens, 0)::NUMERIC / ul.daily_token_limit * 100, 2)
    END as daily_percent_used,
    CASE 
        WHEN ul.monthly_token_limit = -1 THEN 100.0
        ELSE ROUND(COALESCE(monthly.total_tokens, 0)::NUMERIC / ul.monthly_token_limit * 100, 2)
    END as monthly_percent_used
FROM user_groups ug
JOIN usage_limits ul ON ug.group_name = ul.group_name
LEFT JOIN (
    SELECT user_id, SUM(total_tokens) as total_tokens
    FROM usage_records
    WHERE DATE(recorded_at) = CURRENT_DATE
    GROUP BY user_id
) daily ON ug.user_id = daily.user_id
LEFT JOIN (
    SELECT user_id, SUM(total_tokens) as total_tokens
    FROM usage_records
    WHERE recorded_at >= DATE_TRUNC('month', CURRENT_DATE)
    GROUP BY user_id
) monthly ON ug.user_id = monthly.user_id;

-- Users approaching their limits (>80% used)
CREATE OR REPLACE VIEW users_near_limit AS
SELECT * FROM usage_summary
WHERE daily_percent_used > 80 OR monthly_percent_used > 80
ORDER BY monthly_percent_used DESC, daily_percent_used DESC;

-- ============================================================================
-- FUNCTIONS: Helper functions for the filter
-- ============================================================================

-- Get user's current limits and usage in one call
CREATE OR REPLACE FUNCTION get_user_usage_status(p_user_id VARCHAR(255))
RETURNS TABLE (
    group_name VARCHAR(50),
    daily_limit BIGINT,
    monthly_limit BIGINT,
    tokens_today BIGINT,
    tokens_this_month BIGINT,
    is_over_daily BOOLEAN,
    is_over_monthly BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        COALESCE(ug.group_name, 'freemium')::VARCHAR(50) as group_name,
        COALESCE(ul.daily_token_limit, 50000) as daily_limit,
        COALESCE(ul.monthly_token_limit, 1000000) as monthly_limit,
        COALESCE(daily.total, 0) as tokens_today,
        COALESCE(monthly.total, 0) as tokens_this_month,
        CASE 
            WHEN COALESCE(ul.daily_token_limit, 50000) = -1 THEN FALSE
            ELSE COALESCE(daily.total, 0) >= COALESCE(ul.daily_token_limit, 50000)
        END as is_over_daily,
        CASE 
            WHEN COALESCE(ul.monthly_token_limit, 1000000) = -1 THEN FALSE
            ELSE COALESCE(monthly.total, 0) >= COALESCE(ul.monthly_token_limit, 1000000)
        END as is_over_monthly
    FROM (SELECT p_user_id as user_id) u
    LEFT JOIN user_groups ug ON u.user_id = ug.user_id
    LEFT JOIN usage_limits ul ON COALESCE(ug.group_name, 'freemium') = ul.group_name
    LEFT JOIN (
        SELECT ur.user_id, SUM(ur.total_tokens)::BIGINT as total
        FROM usage_records ur
        WHERE ur.user_id = p_user_id AND DATE(ur.recorded_at) = CURRENT_DATE
        GROUP BY ur.user_id
    ) daily ON TRUE
    LEFT JOIN (
        SELECT ur.user_id, SUM(ur.total_tokens)::BIGINT as total
        FROM usage_records ur
        WHERE ur.user_id = p_user_id AND ur.recorded_at >= DATE_TRUNC('month', CURRENT_DATE)
        GROUP BY ur.user_id
    ) monthly ON TRUE;
END;
$$ LANGUAGE plpgsql;

-- Record usage (simple insert wrapper)
CREATE OR REPLACE FUNCTION record_usage(
    p_user_id VARCHAR(255),
    p_prompt_tokens INT,
    p_completion_tokens INT,
    p_model_id VARCHAR(255) DEFAULT NULL,
    p_pipeline_id VARCHAR(255) DEFAULT NULL,
    p_chat_id VARCHAR(255) DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    INSERT INTO usage_records (user_id, prompt_tokens, completion_tokens, total_tokens, model_id, pipeline_id, chat_id)
    VALUES (p_user_id, p_prompt_tokens, p_completion_tokens, p_prompt_tokens + p_completion_tokens, p_model_id, p_pipeline_id, p_chat_id);
    
    -- Auto-create user in freemium group if not exists
    INSERT INTO user_groups (user_id, group_name)
    VALUES (p_user_id, 'freemium')
    ON CONFLICT (user_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- CLEANUP: Optional maintenance
-- ============================================================================

-- Delete old records (run periodically via cron/pg_cron)
-- Keeps 90 days of detailed records by default
CREATE OR REPLACE FUNCTION cleanup_old_usage_records(days_to_keep INT DEFAULT 90)
RETURNS INT AS $$
DECLARE
    deleted_count INT;
BEGIN
    DELETE FROM usage_records
    WHERE recorded_at < NOW() - (days_to_keep || ' days')::INTERVAL;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;
