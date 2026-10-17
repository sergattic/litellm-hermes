"""Free-first, quota-aware routing for LiteLLM search tools.

LiteLLM picks randomly between search tools that share a name and has no notion of monthly
quotas (checked on v1.103.0). This hook adds it:

* Clients call a *pool* name:  POST /v1/search/<pool>
* quota_pools.yaml lists real LiteLLM search tools for that pool in priority order, each with an
  optional monthly quota. The first tool that still has quota and is not cooling down is used,
  so free tiers are spent before paid ones.
* A tool that answers with a quota/rate error is blocked (until the next month for quota errors,
  for a short cooldown for plain rate limits) and the router's fallbacks move the request on.

State lives in the Postgres LiteLLM already uses (DATABASE_URL), in its own schema `quota_router`: backed up with the database,
survives container recreation, nothing else to run.
"""
import os
import threading
import time
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    import psycopg
except ImportError:  # pragma: no cover - psycopg ships in the LiteLLM image
    psycopg = None

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # unit tests stub this
    class CustomLogger:  # type: ignore
        pass

POOLS_FILE = os.environ.get("QUOTA_POOLS_FILE", "/app/quota_pools.yaml")
DB_URL = os.environ.get("DATABASE_URL")
SCHEMA = "quota_router"
RATE_COOLDOWN_S = int(os.environ.get("QUOTA_RATE_COOLDOWN_S", "900"))
AUTH_BLOCK_S = int(os.environ.get("QUOTA_AUTH_BLOCK_S", "21600"))
QUOTA_WORDS = ("quota", "exhaust", "out of searches", "out of credits", "insufficient", "credit", "plan limit",
               "usage limit", "payment required", "exceeded your")


def month_key(now):
    return now.strftime("%Y-%m")


def next_month_start(now):
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc).timestamp()


def classify_failure(status, message, now):
    """Return (kind, blocked_until or None). kind is 'quota', 'rate', 'auth' or 'other'."""
    text = (message or "").lower()
    if status in (401, 403) and not any(w in text for w in QUOTA_WORDS):
        return "auth", now.timestamp() + AUTH_BLOCK_S  # dead or wrong key: stop sending traffic to it for a while
    if status in (401, 403) or "invalid api key" in text or "unauthorized" in text:
        return ("quota", next_month_start(now)) if any(w in text for w in QUOTA_WORDS) else ("auth", now.timestamp() + AUTH_BLOCK_S)
    if status == 402 or any(w in text for w in QUOTA_WORDS):
        return "quota", next_month_start(now)
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "rate", now.timestamp() + RATE_COOLDOWN_S
    return "other", None


class PgState:
    """Quota state: tables live in their own schema inside LiteLLM's database."""

    def __init__(self, url, schema=SCHEMA):
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        # LiteLLM adds Prisma-only options (connection_limit, pool_timeout...) to DATABASE_URL; psycopg rejects them.
        self.url, self.schema = url.split("?")[0], schema
        self.lock = threading.Lock()
        self.conn = None
        self._exec("CREATE SCHEMA IF NOT EXISTS %s" % schema)
        self._exec("CREATE TABLE IF NOT EXISTS %s.usage(tool text, month text, used integer NOT NULL, PRIMARY KEY(tool, month))" % schema)
        self._exec("CREATE TABLE IF NOT EXISTS %s.blocked(tool text PRIMARY KEY, until double precision NOT NULL, reason text)" % schema)

    def _exec(self, sql, params=()):
        for attempt in (0, 1):  # one reconnect if the database restarted
            try:
                if self.conn is None or self.conn.closed:
                    self.conn = psycopg.connect(self.url, autocommit=True, connect_timeout=5)
                cur = self.conn.execute(sql, params)
                return cur.fetchone() if cur.description else None
            except psycopg.OperationalError:
                self.conn = None
                if attempt:
                    raise

    def used(self, tool, month):
        row = self._exec("SELECT used FROM %s.usage WHERE tool=%%s AND month=%%s" % self.schema, (tool, month))
        return row[0] if row else 0

    def add(self, tool, month, n=1):
        self._exec("INSERT INTO %s.usage(tool,month,used) VALUES(%%s,%%s,%%s) "
                   "ON CONFLICT(tool,month) DO UPDATE SET used=GREATEST(0, %s.usage.used+%%s)" % (self.schema, self.schema),
                   (tool, month, max(0, n), n))

    def blocked_until(self, tool):
        row = self._exec("SELECT until FROM %s.blocked WHERE tool=%%s" % self.schema, (tool,))
        return row[0] if row else 0

    def block(self, tool, until, reason):
        self._exec("INSERT INTO %s.blocked(tool,until,reason) VALUES(%%s,%%s,%%s) "
                   "ON CONFLICT(tool) DO UPDATE SET until=EXCLUDED.until, reason=EXCLUDED.reason" % self.schema, (tool, until, reason))


def load_pools(path):
    if yaml is None or not os.path.exists(path):
        return {}
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("pools", {})


class QuotaRouter(CustomLogger):
    def __init__(self, pools=None, state=None, clock=None):
        self.pools = pools if pools is not None else load_pools(POOLS_FILE)
        self._state = state
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def state(self):
        # Connected on first use, so LiteLLM starts even if the database is briefly down; hooks fail open until it is back.
        if self._state is None:
            self._state = PgState(DB_URL)
        return self._state

    def pick(self, pool):
        """Choose the first usable tool of a pool and count one use against it."""
        now = self.clock()
        entries = self.pools[pool]
        with self.state.lock:
            for e in entries:
                tool, limit = e["tool"], e.get("monthly")
                if self.state.blocked_until(tool) > now.timestamp():
                    continue
                if limit is not None and self.state.used(tool, month_key(now)) >= limit:
                    continue
                self.state.add(tool, month_key(now), 1)
                return tool
        return entries[-1]["tool"]  # everything exhausted: let the last one answer (or fail) visibly

    def record_failure(self, tool, status, message):
        now = self.clock()
        kind, until = classify_failure(status, message, now)
        with self.state.lock:
            if kind == "other":
                self.state.add(tool, month_key(now), -1)  # a failed call should not eat quota
            else:
                self.state.block(tool, until, kind)
        return kind

    # --- LiteLLM hooks -------------------------------------------------------------------------
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Fail open: if anything here breaks (for example after a LiteLLM upgrade changes what a hook
        # receives), the request goes through unchanged instead of failing.
        try:
            if str(call_type) not in ("asearch", "search"):
                return data
            pool = data.get("search_tool_name") or data.get("model")
            if pool not in self.pools:
                return data
            tool = self.pick(pool)
            data["search_tool_name"] = tool
            data.setdefault("metadata", {})["quota_pool"] = pool
            data["metadata"]["quota_tool"] = tool
            # Tags make LiteLLM report calls and spend per upstream key: GET /spend/tags, or Usage -> Tags in the UI.
            data["metadata"]["tags"] = list(data["metadata"].get("tags") or []) + ["search-tool:" + tool, "search-pool:" + pool]
        except Exception as exc:  # noqa: BLE001
            print("quota_router: pre-call hook failed open: %r" % (exc,), flush=True)
        return data

    def _tool_of(self, kwargs):
        for src in (kwargs, kwargs.get("litellm_params") or {}):
            for md in (src.get("metadata") or {}, src.get("litellm_metadata") or {}):
                if md.get("quota_tool"):
                    return md["quota_tool"]
        return None

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            tool = self._tool_of(kwargs)
            if not tool:
                return
            exc = kwargs.get("exception")
            self.record_failure(tool, getattr(exc, "status_code", None), str(exc))
        except Exception as exc:  # noqa: BLE001
            print("quota_router: failure hook failed open: %r" % (exc,), flush=True)


quota_router = QuotaRouter() if os.environ.get("QUOTA_AUTOLOAD", "1") == "1" else None
