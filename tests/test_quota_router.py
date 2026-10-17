import asyncio
import os
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "callbacks"))
os.environ["QUOTA_AUTOLOAD"] = "0"
import quota_router as q  # noqa: E402

POOLS = {"web": ["free-1", "free-2", "paid"]}


class MemState:
    """In-memory stand-in for PgState so the routing logic is tested without a database (the e2e test covers Postgres)."""

    def __init__(self):
        self.lock, self.ex, self.calls = threading.Lock(), set(), {}

    def exhausted(self, tool, month): return (tool, month) in self.ex
    def record_call(self, tool, month): self.calls[(tool, month)] = self.calls.get((tool, month), 0) + 1
    def mark_exhausted(self, tool, month): self.ex.add((tool, month))


class Clock:
    def __init__(self): self.now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    def __call__(self): return self.now


def make(clock=None):
    return q.QuotaRouter(pools=POOLS, state=MemState(), clock=clock or Clock())


def hook(r, data, call_type="asearch"):
    return asyncio.run(r.async_pre_call_hook(None, None, data, call_type))


def test_first_tool_gets_the_request_and_the_rest_are_its_fallbacks_in_order():
    d = hook(make(), {"search_tool_name": "web", "query": "x"})
    assert d["search_tool_name"] == "free-1" and d["fallbacks"] == ["free-2", "paid"]


def test_exhausted_tools_are_skipped_everywhere():
    r = make(); r.record_failure("free-1", "This request exceeds this API key's set usage limit")
    d = hook(r, {"search_tool_name": "web"})
    assert d["search_tool_name"] == "free-2" and d["fallbacks"] == ["paid"]
    r.record_failure("free-2", "Error code: 402 - Insufficient credit in account")
    d = hook(r, {"search_tool_name": "web"})
    assert d["search_tool_name"] == "paid" and "fallbacks" not in d


def test_a_new_month_forgets_everything():
    c = Clock(); r = make(c); r.record_failure("free-1", "429 Too Many Requests")
    assert r.order("web") == ["free-2", "paid"]
    c.now = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)
    assert r.order("web") == ["free-1", "free-2", "paid"]


def test_same_patterns_as_the_plugin_including_plain_429():
    r = make()
    for msg in ("usage limit", "Insufficient credit", "Error code: 402", "HTTP 429", "rate limit exceeded", "quota exceeded", "out of credits"):
        assert r.record_failure("free-1", msg), msg
    for msg in ("Connection timed out", "500 internal server error", "Unauthorized: invalid API key", "empty result"):
        assert not r.record_failure("free-2", msg), msg


def test_everything_exhausted_still_returns_the_last_tool():
    r = make()
    for t in POOLS["web"]: r.record_failure(t, "429")
    assert r.order("web") == ["paid"]


def test_only_known_pools_and_search_calls_are_touched():
    r = make()
    assert hook(r, {"search_tool_name": "other"})["search_tool_name"] == "other"
    assert hook(r, {"search_tool_name": "web"}, "acompletion")["search_tool_name"] == "web"


def test_attempts_are_counted_per_tool_from_the_events():
    r = make()
    ok = {"standard_logging_object": {"model_group": "free-2"}}
    asyncio.run(r.async_log_success_event(ok, None, None, None))
    asyncio.run(r.async_log_failure_event({"standard_logging_object": {"model_group": "free-1"}, "exception": RuntimeError("429")}, None, None, None))
    assert r.state.calls == {("free-2", "2026-10"): 1, ("free-1", "2026-10"): 1} and r.state.exhausted("free-1", "2026-10")


def test_hook_fails_open_when_the_database_is_unreachable(monkeypatch):
    r = q.QuotaRouter(pools=POOLS, clock=Clock())
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    d = hook(r, {"search_tool_name": "web", "query": "x"})
    assert d["search_tool_name"] == "web"  # untouched: LiteLLM's own routing takes over
