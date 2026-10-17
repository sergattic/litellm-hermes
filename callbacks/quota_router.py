"""Free-first, quota-aware routing for LiteLLM search tools.

LiteLLM picks randomly between search tools that share a name and has no notion of monthly
quotas (checked on v1.103.0). This hook adds it:

* Clients call a *pool* name:  POST /v1/search/<pool>
* quota_pools.yaml lists real LiteLLM search tools for that pool in priority order, each with an
  optional monthly quota. The first tool that still has quota and is not cooling down is used,
  so free tiers are spent before paid ones.
* A tool that answers with a quota/rate error is blocked (until the next month for quota errors,
  for a short cooldown for plain rate limits) and the router's fallbacks move the request on.

State lives in a small SQLite file (QUOTA_DB), so no extra service is needed.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # unit tests stub this
    class CustomLogger:  # type: ignore
        pass

POOLS_FILE = os.environ.get("QUOTA_POOLS_FILE", "/app/quota_pools.yaml")
DB_PATH = os.environ.get("QUOTA_DB", "/data/quota.db")
RATE_COOLDOWN_S = int(os.environ.get("QUOTA_RATE_COOLDOWN_S", "900"))
AUTH_BLOCK_S = int(os.environ.get("QUOTA_AUTH_BLOCK_S", "21600"))
DEBUG = os.environ.get("QUOTA_DEBUG") == "1"
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


class QuotaState:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS usage(tool TEXT, month TEXT, used INTEGER, PRIMARY KEY(tool, month))")
        self.db.execute("CREATE TABLE IF NOT EXISTS blocked(tool TEXT PRIMARY KEY, until REAL, reason TEXT)")
        self.db.commit()

    def used(self, tool, month):
        row = self.db.execute("SELECT used FROM usage WHERE tool=? AND month=?", (tool, month)).fetchone()
        return row[0] if row else 0

    def add(self, tool, month, n=1):
        self.db.execute("INSERT INTO usage(tool,month,used) VALUES(?,?,?) ON CONFLICT(tool,month) DO UPDATE SET used=MAX(0,used+?)",
                        (tool, month, max(0, n), n))
        self.db.commit()

    def blocked_until(self, tool):
        row = self.db.execute("SELECT until FROM blocked WHERE tool=?", (tool,)).fetchone()
        return row[0] if row else 0

    def block(self, tool, until, reason):
        self.db.execute("INSERT INTO blocked(tool,until,reason) VALUES(?,?,?) ON CONFLICT(tool) DO UPDATE SET until=?, reason=?",
                        (tool, until, reason, until, reason))
        self.db.commit()


def load_pools(path):
    if yaml is None or not os.path.exists(path):
        return {}
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("pools", {})


class QuotaRouter(CustomLogger):
    def __init__(self, pools=None, state=None, clock=None):
        self.pools = pools if pools is not None else load_pools(POOLS_FILE)
        self.state = state or QuotaState(DB_PATH)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        if DEBUG:
            with open("/tmp/quota_debug.log", "a") as f:
                f.write("FAIL keys=%s tool=%s exc=%r\n" % (sorted(kwargs), self._tool_of(kwargs), kwargs.get("exception")))
        try:
            tool = self._tool_of(kwargs)
            if not tool:
                return
            exc = kwargs.get("exception")
            self.record_failure(tool, getattr(exc, "status_code", None), str(exc))
        except Exception as exc:  # noqa: BLE001
            print("quota_router: failure hook failed open: %r" % (exc,), flush=True)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        if DEBUG:
            lp = kwargs.get("litellm_params") or {}
            with open("/tmp/quota_debug.log", "a") as f:
                f.write("OK keys=%s model=%s tool=%s lp_keys=%s\n" % (sorted(kwargs), kwargs.get("model"), self._tool_of(kwargs), sorted(lp)))


quota_router = QuotaRouter() if os.environ.get("QUOTA_AUTOLOAD", "1") == "1" else None
