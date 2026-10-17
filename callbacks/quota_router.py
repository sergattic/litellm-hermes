"""Free-first search routing for LiteLLM: the ladder logic of the Hermes `resilient-search` plugin, run once, centrally.

Clients call a *pool* (POST /v1/search/<pool>). A pool is an ordered list of LiteLLM search tools (one per key), free first.
For every request this hook:
  * skips tools already marked exhausted for the current month (UTC calendar month, no timers),
  * sends the request to the first remaining tool and sets the rest as that request's fallbacks, in order,
  * marks a tool exhausted when it fails with a quota/credit/rate-limit error, until the month rolls over.
Any other failure just moves on to the next tool without marking anything. State is Postgres (schema `quota_router`).
"""
import os
import re
import threading
from datetime import datetime, timezone

try:
    import psycopg
    import yaml
except ImportError:  # pragma: no cover - both ship in the LiteLLM image
    psycopg = yaml = None

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # unit tests run without LiteLLM
    class CustomLogger:  # type: ignore
        pass

POOLS_FILE = os.environ.get("QUOTA_POOLS_FILE", "/app/quota_pools.yaml")
# Same patterns as resilient-search: "this key is out of quota/credit for the period", not a network blip.
EXHAUSTED = re.compile(r"usage limit|insufficient credit|error code:\s*402|\b402\b|\b429\b|rate limit exceed|quota exceed|out of credit", re.I)


def period(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


class PgState:
    """One row per tool per month: calls and exhausted flag (the same shape as the plugin's usage_state.json)."""

    def __init__(self, url, schema="quota_router"):
        # LiteLLM adds Prisma-only options (connection_limit, pool_timeout...) to DATABASE_URL; psycopg rejects them.
        self.url, self.schema, self.conn, self.lock = url.split("?")[0], schema, None, threading.Lock()
        self._exec("CREATE SCHEMA IF NOT EXISTS %s" % schema)
        self._exec("CREATE TABLE IF NOT EXISTS %s.usage(tool text, month text, calls integer NOT NULL DEFAULT 0, "
                   "exhausted boolean NOT NULL DEFAULT false, marked_at timestamptz, PRIMARY KEY(tool, month))" % schema)

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

    def exhausted(self, tool, month):
        row = self._exec("SELECT exhausted FROM %s.usage WHERE tool=%%s AND month=%%s" % self.schema, (tool, month))
        return bool(row and row[0])

    def record_call(self, tool, month):
        self._exec("INSERT INTO %s.usage(tool,month,calls) VALUES(%%s,%%s,1) "
                   "ON CONFLICT(tool,month) DO UPDATE SET calls=%s.usage.calls+1" % (self.schema, self.schema), (tool, month))

    def mark_exhausted(self, tool, month):
        self._exec("INSERT INTO %s.usage(tool,month,exhausted,marked_at) VALUES(%%s,%%s,true,now()) "
                   "ON CONFLICT(tool,month) DO UPDATE SET exhausted=true, marked_at=now()" % self.schema, (tool, month))


class QuotaRouter(CustomLogger):
    def __init__(self, pools=None, state=None, clock=None):
        self.pools = pools if pools is not None else self._load_pools()
        self._state = state
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _load_pools():
        if yaml is None or not os.path.exists(POOLS_FILE):
            return {}
        with open(POOLS_FILE) as f:
            return (yaml.safe_load(f) or {}).get("pools", {})

    @property
    def state(self):
        # Connected on first use, so LiteLLM starts even if the database is briefly down; hooks fail open until it is back.
        if self._state is None:
            self._state = PgState(os.environ["DATABASE_URL"])
        return self._state

    def order(self, pool):
        """The pool's tools that are not exhausted this month, in ladder order (the last tool if everything is)."""
        month, tools = period(self.clock()), self.pools[pool]
        with self.state.lock:
            usable = [t for t in tools if not self.state.exhausted(t, month)]
        return usable or [tools[-1]]

    def record_failure(self, tool, message):
        if EXHAUSTED.search(message or ""):
            with self.state.lock:
                self.state.mark_exhausted(tool, period(self.clock()))
            return True
        return False

    # --- LiteLLM hooks -------------------------------------------------------------------------
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Fail open: if anything here breaks, the request goes through unchanged instead of failing.
        try:
            if str(call_type) not in ("asearch", "search"):
                return data
            pool = data.get("search_tool_name") or data.get("model")
            if pool not in self.pools:
                return data
            first, *rest = self.order(pool)
            data["search_tool_name"] = first
            if rest:
                data["fallbacks"] = rest
            # Tags make LiteLLM report calls and spend per upstream key: GET /spend/tags, or Usage -> Tags in the UI.
            data.setdefault("metadata", {})["tags"] = list(data.get("metadata", {}).get("tags") or []) + ["search-pool:" + pool]
        except Exception as exc:  # noqa: BLE001
            print("quota_router: pre-call hook failed open: %r" % (exc,), flush=True)
        return data

    def _tool_of(self, kwargs):
        tool = (kwargs.get("standard_logging_object") or {}).get("model_group")  # the tool of THIS attempt, fallbacks included
        return tool if any(tool in tools for tools in self.pools.values()) else None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            tool = self._tool_of(kwargs)
            if tool:
                with self.state.lock:
                    self.state.record_call(tool, period(self.clock()))
        except Exception as exc:  # noqa: BLE001
            print("quota_router: success hook failed open: %r" % (exc,), flush=True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            tool = self._tool_of(kwargs)
            if tool:
                with self.state.lock:
                    self.state.record_call(tool, period(self.clock()))
                exc = kwargs.get("exception")
                marked = self.record_failure(tool, str(exc))
                # One line per failed attempt, like the plugin: the decision, the HTTP status LiteLLM mapped it to, and the provider's message.
                print("quota_router: %s failed (status=%s) -> %s: %s" % (
                    tool, getattr(exc, "status_code", "?"),
                    "marked exhausted for " + period(self.clock()) if marked else "moving on, not marked",
                    str(exc).replace("\n", " ")[:400]), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("quota_router: failure hook failed open: %r" % (exc,), flush=True)


quota_router = QuotaRouter() if os.environ.get("QUOTA_AUTOLOAD", "1") == "1" else None
