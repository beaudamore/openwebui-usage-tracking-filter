"""
title: Usage Tracking Filter
author: beaudamore
date: 2026-10-07
version: 1.0.1
license: MIT
description: Token usage tracking and group-based rate limiting with PostgreSQL persistence
required_open_webui_version: >= 0.5.0
requirements: psycopg[binary], psycopg-pool
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from fastapi import Request

# PostgreSQL imports
import psycopg
import psycopg_pool

# Set up logging
logger = logging.getLogger("openwebui.filters.usage_tracking")
logger.setLevel(logging.INFO)


class UsageLimitExceeded(Exception):
    """Raised from inlet to stop the request. Open WebUI shows the message to the user."""


class Filter:
    """
    Usage Tracking Filter for Open WebUI
    
    Tracks token usage per user and enforces group-based limits.
    Stores only user UUIDs - no PII.
    
    Flow:
    - inlet(): Check if user is over daily/monthly limit → block if so
    - outlet(): Record token usage from response
    """
    
    class Valves(BaseModel):
        """Configuration for Usage Tracking Filter"""
        
        # Execution Priority - MUST run before other filters
        priority: int = Field(
            default=5,
            description="Filter execution priority. Set LOWER than other filters (e.g., 5) so usage check runs FIRST. Memory filter should be 10+."
        )
        
        # PostgreSQL Configuration
        postgres_host: str = Field(
            default="langgraph-postgres",
            description="PostgreSQL host (use same as memory filter if sharing)"
        )
        postgres_port: int = Field(
            default=5432,
            description="PostgreSQL port"
        )
        postgres_database: str = Field(
            default="langgraph_memory",
            description="PostgreSQL database name (can share with memory filter)"
        )
        postgres_user: str = Field(
            default="langgraph",
            description="PostgreSQL username"
        )
        postgres_password: str = Field(
            default="langgraph_password_change_me",
            description="PostgreSQL password"
        )
        
        # Default Group
        default_group: str = Field(
            default="freemium",
            description="Tier applied to users with no row in user_groups. Must match a group_name in usage_limits; new users are auto-enrolled into it on their first recorded response."
        )
        
        # Behavior Configuration
        enable_blocking: bool = Field(
            default=True,
            description="Block requests when limits exceeded. If False, only logs warnings."
        )
        show_usage_status: bool = Field(
            default=True,
            description="Show usage status messages to users"
        )
        warn_at_percent: int = Field(
            default=80,
            description="Warn users when they reach this percentage of their limit"
        )
        
        # Admin Bypass
        admin_bypass: bool = Field(
            default=True,
            description="Allow admin users to bypass limits"
        )
        
        # Debug
        debug_mode: bool = Field(
            default=False,
            description="Enable detailed debug logging"
        )

    class UserValves(BaseModel):
        """Per-user configuration (users can change these themselves, so nothing here may weaken enforcement)"""
        show_usage_status: bool = Field(
            default=True,
            description="Show the usage status line on your messages. Limit warnings and blocking are unaffected."
        )

    def __init__(self):
        self.name = "Usage Tracking Filter"
        self.valves = self.Valves()
        self._pool = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _log(self, message: str, level: str = "info"):
        """Centralized logging"""
        if level == "debug" and not self.valves.debug_mode:
            return
        print(f"[Usage Tracking] [{level.upper()}] {message}", flush=True)
        getattr(logger, level, logger.info)(f"[Usage Tracking] {message}")

    @staticmethod
    def _user_wants_status(__user__: Optional[Dict[str, Any]]) -> bool:
        """Read the per-user show_usage_status valve. Open WebUI injects it as __user__["valves"]."""
        user_valves = (__user__ or {}).get("valves")
        if user_valves is None:
            return True
        return bool(getattr(user_valves, "show_usage_status", True))

    def _get_tables_sql(self) -> str:
        """Tables, indexes and seed tiers. Run only when the schema is missing, so admin edits to tiers survive restarts."""
        return """
        -- ============================================================================
        -- USAGE LIMITS: Group-based quota definitions
        -- ============================================================================
        CREATE TABLE IF NOT EXISTS usage_limits (
            group_name VARCHAR(50) PRIMARY KEY,
            daily_token_limit BIGINT NOT NULL DEFAULT 1000000,
            monthly_token_limit BIGINT NOT NULL DEFAULT 10000000,
            rate_limit_rpm INT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            description TEXT
        );

        -- Insert default tiers (skip if already exist)
        INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description)
        VALUES 
            ('freemium', 1000000, 10000000, 'Free tier - 1M/day, 10M/month'),
            ('pro', 5000000, 100000000, 'Pro tier - 5M/day, 100M/month'),
            ('enterprise', -1, -1, 'Enterprise tier - unlimited (-1 = no limit)')
        ON CONFLICT (group_name) DO NOTHING;

        -- ============================================================================
        -- USER GROUPS: Maps user UUIDs to their group (no PII stored)
        -- ============================================================================
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id VARCHAR(255) PRIMARY KEY,
            group_name VARCHAR(50) NOT NULL REFERENCES usage_limits(group_name) DEFAULT 'freemium',
            assigned_at TIMESTAMPTZ DEFAULT NOW(),
            assigned_by VARCHAR(255),
            notes TEXT
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
            model_id VARCHAR(255),
            pipeline_id VARCHAR(255),
            chat_id VARCHAR(255),
            request_type VARCHAR(50) DEFAULT 'chat'
        );

        -- Indexes for fast queries
        CREATE INDEX IF NOT EXISTS idx_usage_records_user ON usage_records(user_id);
        CREATE INDEX IF NOT EXISTS idx_usage_records_date ON usage_records(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_usage_records_user_date ON usage_records(user_id, recorded_at DESC);
        -- Note: Cannot use DATE(recorded_at) in index - not immutable. Use recorded_at::date in queries instead.
        """

    def _get_routines_sql(self) -> str:
        """Views and functions. Idempotent; re-applied on every initialization so upgrades need no manual migration."""
        return """
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

        -- Drop pre-1.0.1 signatures so the versions below replace them instead of overloading them
        DROP FUNCTION IF EXISTS get_user_usage_status(VARCHAR);
        DROP FUNCTION IF EXISTS record_usage(VARCHAR, INT, INT, VARCHAR, VARCHAR, VARCHAR);

        -- Get user's current limits and usage in one call.
        -- p_default_group: tier assumed for users with no user_groups row (the default_group valve).
        CREATE OR REPLACE FUNCTION get_user_usage_status(
            p_user_id VARCHAR(255),
            p_default_group VARCHAR(50) DEFAULT 'freemium'
        )
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
                COALESCE(ug.group_name, p_default_group)::VARCHAR(50) as group_name,
                COALESCE(ul.daily_token_limit, 1000000) as daily_limit,
                COALESCE(ul.monthly_token_limit, 10000000) as monthly_limit,
                COALESCE(daily.total, 0) as tokens_today,
                COALESCE(monthly.total, 0) as tokens_this_month,
                CASE 
                    WHEN COALESCE(ul.daily_token_limit, 1000000) = -1 THEN FALSE
                    ELSE COALESCE(daily.total, 0) >= COALESCE(ul.daily_token_limit, 1000000)
                END as is_over_daily,
                CASE 
                    WHEN COALESCE(ul.monthly_token_limit, 10000000) = -1 THEN FALSE
                    ELSE COALESCE(monthly.total, 0) >= COALESCE(ul.monthly_token_limit, 10000000)
                END as is_over_monthly
            FROM (SELECT p_user_id as user_id) u
            LEFT JOIN user_groups ug ON u.user_id = ug.user_id
            LEFT JOIN usage_limits ul ON COALESCE(ug.group_name, p_default_group) = ul.group_name
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

        -- Record usage (simple insert wrapper).
        -- p_default_group: tier new users are enrolled into (the default_group valve).
        CREATE OR REPLACE FUNCTION record_usage(
            p_user_id VARCHAR(255),
            p_prompt_tokens INT,
            p_completion_tokens INT,
            p_model_id VARCHAR(255) DEFAULT NULL,
            p_pipeline_id VARCHAR(255) DEFAULT NULL,
            p_chat_id VARCHAR(255) DEFAULT NULL,
            p_default_group VARCHAR(50) DEFAULT 'freemium'
        ) RETURNS VOID AS $$
        BEGIN
            INSERT INTO usage_records (user_id, prompt_tokens, completion_tokens, total_tokens, model_id, pipeline_id, chat_id)
            VALUES (p_user_id, p_prompt_tokens, p_completion_tokens, p_prompt_tokens + p_completion_tokens, p_model_id, p_pipeline_id, p_chat_id);

            -- Auto-enrol the user in the default tier if they have no row yet.
            -- Guarded by EXISTS so a default_group that names a missing tier cannot
            -- violate the foreign key and abort the usage insert above.
            INSERT INTO user_groups (user_id, group_name)
            SELECT p_user_id, p_default_group
            WHERE EXISTS (SELECT 1 FROM usage_limits WHERE group_name = p_default_group)
            ON CONFLICT (user_id) DO NOTHING;
        END;
        $$ LANGUAGE plpgsql;

        -- Cleanup old records (run periodically)
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
        """

    def _ensure_schema(self, conn) -> bool:
        """
        Create tables on first run; (re)apply views and functions on every run.
        Returns True if the tables were created, False if they already existed.
        """
        with conn.cursor() as cur:
            # Check if main table exists
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'usage_limits'
                )
            """)
            tables_exist = cur.fetchone()[0]

            if tables_exist:
                self._log("Usage tracking tables already exist", "debug")
            else:
                self._log("Creating usage tracking tables...")
                cur.execute(self._get_tables_sql())

            # Views and functions are idempotent and cheap; always apply so that
            # upgrades of this filter take effect without manual migration.
            cur.execute(self._get_routines_sql())
            conn.commit()

            if not tables_exist:
                self._log("✅ Usage tracking schema created successfully")

            # Warn if the configured default tier does not exist
            cur.execute("SELECT 1 FROM usage_limits WHERE group_name = %s", (self.valves.default_group,))
            if cur.fetchone() is None:
                self._log(
                    f"default_group '{self.valves.default_group}' is not defined in usage_limits. "
                    f"Unassigned users get fallback limits (1M/day, 10M/month) and will not be auto-enrolled.",
                    "warning",
                )

            return not tables_exist

    def _connect(self):
        """Blocking: open the connection pool and ensure the schema. Runs in a worker thread."""
        conn_string = (
            f"postgresql://{self.valves.postgres_user}:"
            f"{quote_plus(self.valves.postgres_password)}@"
            f"{self.valves.postgres_host}:{self.valves.postgres_port}/"
            f"{self.valves.postgres_database}"
        )

        self._log(f"Connecting to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}")

        pool = psycopg_pool.ConnectionPool(
            conninfo=conn_string,
            min_size=1,
            max_size=5,
            open=True,
        )

        with pool.connection() as conn:
            if self._ensure_schema(conn):
                self._log("Schema was auto-created on first run")
            else:
                self._log("Using existing schema", "debug")

        return pool

    async def _initialize(self):
        """Initialize PostgreSQL connection pool and ensure schema exists (once, without blocking the event loop)"""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return
            try:
                self._pool = await asyncio.to_thread(self._connect)
                self._initialized = True
                self._log("Usage tracking initialized successfully")
            except Exception as e:
                self._log(f"Failed to initialize: {e}", "error")
                raise

    def _get_user_status(self, user_id: str) -> Dict[str, Any]:
        """Get user's current usage status from PostgreSQL"""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    # Use the helper function we created
                    cur.execute(
                        "SELECT * FROM get_user_usage_status(%s, %s)",
                        (user_id, self.valves.default_group),
                    )
                    row = cur.fetchone()
                    
                    if row:
                        return {
                            "group_name": row[0],
                            "daily_limit": row[1],
                            "monthly_limit": row[2],
                            "tokens_today": row[3],
                            "tokens_this_month": row[4],
                            "is_over_daily": row[5],
                            "is_over_monthly": row[6],
                        }
                    
                    # Fallback: user not found, return defaults
                    return {
                        "group_name": self.valves.default_group,
                        "daily_limit": 1000000,
                        "monthly_limit": 10000000,
                        "tokens_today": 0,
                        "tokens_this_month": 0,
                        "is_over_daily": False,
                        "is_over_monthly": False,
                    }
                    
        except Exception as e:
            self._log(f"Failed to get user status: {e}", "error")
            # On error, allow the request (fail open)
            return {
                "group_name": "unknown",
                "daily_limit": -1,
                "monthly_limit": -1,
                "tokens_today": 0,
                "tokens_this_month": 0,
                "is_over_daily": False,
                "is_over_monthly": False,
            }

    def _record_usage(
        self,
        user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        model_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        chat_id: Optional[str] = None
    ):
        """Record token usage to PostgreSQL"""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT record_usage(%s, %s, %s, %s, %s, %s, %s)",
                        (user_id, prompt_tokens, completion_tokens, model_id, pipeline_id, chat_id,
                         self.valves.default_group)
                    )
                conn.commit()
                
            total = prompt_tokens + completion_tokens
            self._log(f"Recorded {total} tokens for user {user_id[:8]}...", "debug")
            
        except Exception as e:
            self._log(f"Failed to record usage: {e}", "error")

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Inlet: Check user's usage against limits before processing
        """
        self._log("=== INLET START ===", "debug")
        
        if not __user__ or not __user__.get("id"):
            self._log("No user ID, skipping usage check", "debug")
            return body
        
        user_id = __user__["id"]
        user_role = __user__.get("role", "user")
        is_admin = self.valves.admin_bypass and user_role == "admin"
        
        try:
            # Initialize if needed
            if not self._initialized:
                await self._initialize()
            
            # Get user's current status (always, for all users including admins)
            status = await asyncio.to_thread(self._get_user_status, user_id)

            self._log(
                f"User {user_id[:8]}... ({status['group_name']}): "
                f"{status['tokens_today']}/{status['daily_limit']} today, "
                f"{status['tokens_this_month']}/{status['monthly_limit']} this month",
                "debug"
            )

            # Show current usage status unless disabled globally or by this user
            if self.valves.show_usage_status and self._user_wants_status(__user__) and __event_emitter__:
                daily_percent = 0 if status["daily_limit"] == -1 else (status["tokens_today"] / status["daily_limit"] * 100)
                monthly_percent = 0 if status["monthly_limit"] == -1 else (status["tokens_this_month"] / status["monthly_limit"] * 100)
                
                def fmt_tokens(n):
                    return f"{n/1000:.1f}K" if n >= 1000 else str(n)
                
                tokens_today = fmt_tokens(status["tokens_today"])
                daily_limit = fmt_tokens(status["daily_limit"]) if status["daily_limit"] != -1 else "∞"
                tokens_month = fmt_tokens(status["tokens_this_month"])
                monthly_limit = fmt_tokens(status["monthly_limit"]) if status["monthly_limit"] != -1 else "∞"
                
                is_warning = daily_percent >= self.valves.warn_at_percent or monthly_percent >= self.valves.warn_at_percent
                icon = "⚠️" if is_warning else "📊"
                
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": f"{icon} Usage: {tokens_today}/{daily_limit} today ({daily_percent:.0f}%) • {tokens_month}/{monthly_limit} month ({monthly_percent:.0f}%)",
                        "done": True
                    }
                })
            
            # Check if over limits (admin bypass only affects blocking, not display)
            if status["is_over_daily"] or status["is_over_monthly"]:
                if self.valves.enable_blocking and not is_admin:
                    # Determine which limit was hit
                    if status["is_over_daily"]:
                        limit_type = "daily"
                        limit_val = status["daily_limit"]
                        used_val = status["tokens_today"]
                        reset_info = "midnight UTC"
                    else:
                        limit_type = "monthly"
                        limit_val = status["monthly_limit"]
                        used_val = status["tokens_this_month"]
                        reset_info = "the 1st of next month"
                    
                    error_msg = (
                        f"⚠️ **Usage Limit Reached**\n\n"
                        f"You've reached your {limit_type} token limit for the **{status['group_name']}** tier.\n\n"
                        f"- **Used:** {used_val:,} tokens\n"
                        f"- **Limit:** {limit_val:,} tokens\n"
                        f"- **Resets:** {reset_info}\n\n"
                        f"Contact your administrator to upgrade your plan."
                    )
                    
                    self._log(f"User {user_id[:8]}... blocked: over {limit_type} limit", "info")
                    
                    if __event_emitter__:
                        await __event_emitter__({
                            "type": "status",
                            "data": {
                                "description": f"❌ {limit_type.title()} limit reached",
                                "done": True
                            }
                        })
                    
                    # Raising stops the request; Open WebUI returns the message as a chat error
                    raise UsageLimitExceeded(error_msg)
                elif is_admin:
                    self._log(f"Admin user {user_id[:8]}... over limit but bypassing block", "debug")
                else:
                    self._log(f"User {user_id[:8]}... over limit but blocking disabled", "warning")

        except UsageLimitExceeded:
            raise
        except Exception as e:
            self._log(f"Inlet error: {e}", "error")
            # Fail open - allow request on error
        
        self._log("=== INLET COMPLETE ===", "debug")
        return body

    async def outlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Outlet: Record token usage from the response
        """
        self._log("=== OUTLET START ===", "debug")

        # Outlet body is built fresh by Open WebUI; nothing from inlet survives
        user_id = __user__.get("id") if __user__ else None

        if not user_id:
            self._log("No user ID in outlet, skipping", "debug")
            return body
        
        try:
            if not self._initialized:
                await self._initialize()
            
            # Extract usage from response
            # Open WebUI puts this in various places depending on the model
            usage = None
            
            # Check body directly
            if "usage" in body:
                usage = body["usage"]
            
            # Check in messages (assistant message might have usage)
            messages = body.get("messages", [])
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and "usage" in msg:
                    usage = msg["usage"]
                    break
            
            if usage:
                # Open WebUI normalizes usage: input_tokens/output_tokens are cumulative across
                # tool-call round trips, while prompt_tokens/completion_tokens hold only the last call.
                prompt_tokens = (
                    usage.get("input_tokens", 0)
                    or usage.get("prompt_tokens", 0)
                    or usage.get("prompt_eval_count", 0)
                    or 0
                )
                completion_tokens = (
                    usage.get("output_tokens", 0)
                    or usage.get("completion_tokens", 0)
                    or usage.get("eval_count", 0)
                    or 0
                )
                
                if prompt_tokens > 0 or completion_tokens > 0:
                    await asyncio.to_thread(
                        self._record_usage,
                        user_id,
                        prompt_tokens,
                        completion_tokens,
                        body.get("model"),
                        None,
                        body.get("chat_id"),
                    )

                    total = prompt_tokens + completion_tokens
                    self._log(f"Recorded {total} tokens for user {user_id[:8]}...", "info")

                    # Check if user has now exceeded limits after this usage
                    status = await asyncio.to_thread(self._get_user_status, user_id)
                    daily_percent = 0 if status["daily_limit"] == -1 else (status["tokens_today"] / status["daily_limit"] * 100)
                    monthly_percent = 0 if status["monthly_limit"] == -1 else (status["tokens_this_month"] / status["monthly_limit"] * 100)
                    
                    # If over limit or near limit, append warning to the actual chat response
                    if status["is_over_daily"] or status["is_over_monthly"] or daily_percent >= self.valves.warn_at_percent or monthly_percent >= self.valves.warn_at_percent:
                        def fmt_tokens(n):
                            return f"{n/1000:.1f}K" if n >= 1000 else str(n)
                        
                        tokens_today = fmt_tokens(status["tokens_today"])
                        daily_limit = fmt_tokens(status["daily_limit"]) if status["daily_limit"] != -1 else "∞"
                        tokens_month = fmt_tokens(status["tokens_this_month"])
                        monthly_limit = fmt_tokens(status["monthly_limit"]) if status["monthly_limit"] != -1 else "∞"
                        
                        # Build warning message
                        if status["is_over_daily"] or status["is_over_monthly"]:
                            warning_msg = (
                                f"\n\n---\n"
                                f"⚠️ **Usage Limit Reached**\n\n"
                                f"You've reached your {'daily' if status['is_over_daily'] else 'monthly'} limit for the **{status['group_name']}** tier.\n"
                                f"- Today: {tokens_today}/{daily_limit} ({daily_percent:.0f}%)\n"
                                f"- This month: {tokens_month}/{monthly_limit} ({monthly_percent:.0f}%)\n\n"
                                f"Your next request will be blocked until the limit resets."
                            )
                        else:
                            warning_msg = (
                                f"\n\n---\n"
                                f"⚠️ **Approaching Usage Limit**\n\n"
                                f"- Today: {tokens_today}/{daily_limit} ({daily_percent:.0f}%)\n"
                                f"- This month: {tokens_month}/{monthly_limit} ({monthly_percent:.0f}%)"
                            )
                        
                        # Append warning to the last assistant message
                        messages = body.get("messages", [])
                        for i in range(len(messages) - 1, -1, -1):
                            if messages[i].get("role") == "assistant":
                                if isinstance(messages[i].get("content"), str):
                                    messages[i]["content"] += warning_msg
                                break
                        
                        self._log(f"Added usage warning to chat for user {user_id[:8]}...", "info")
                else:
                    self._log("Usage found but no tokens to record", "debug")
            else:
                self._log("No usage data in response", "debug")
                
        except Exception as e:
            self._log(f"Outlet error: {e}", "error")
        
        self._log("=== OUTLET COMPLETE ===", "debug")
        return body
