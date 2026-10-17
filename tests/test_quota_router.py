import os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "callbacks"))
os.environ["QUOTA_AUTOLOAD"] = "0"
import quota_router as q  # noqa: E402

POOLS = {"web": [{"tool": "free-1", "monthly": 2}, {"tool": "free-2", "monthly": 2}, {"tool": "paid"}]}


class Clock:
    def __init__(self, y=2026, m=10, d=15):
        self.now = datetime(y, m, d, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def make(clock=None):
    return q.QuotaRouter(pools=POOLS, state=q.QuotaState(":memory:"), clock=clock or Clock())


def test_free_first_in_order_then_paid():
    r = make()
    assert [r.pick("web") for _ in range(6)] == ["free-1", "free-1", "free-2", "free-2", "paid", "paid"]


def test_month_rollover_resets_counters():
    c = Clock(); r = make(c)
    for _ in range(4): r.pick("web")
    assert r.pick("web") == "paid"
    c.now = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    assert r.pick("web") == "free-1"


def test_quota_error_blocks_until_next_month():
    c = Clock(); r = make(c)
    assert r.record_failure("free-1", 429, "Your account has run out of searches") == "quota"
    assert r.pick("web") == "free-2"
    c.now = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)
    assert r.pick("web") == "free-1"


def test_plain_rate_limit_is_a_short_cooldown():
    c = Clock(); r = make(c)
    assert r.record_failure("free-1", 429, "Too Many Requests") == "rate"
    assert r.pick("web") == "free-2"
    c.now = datetime(2026, 10, 15, 12, 20, tzinfo=timezone.utc)  # 20 min later, cooldown is 15
    assert r.pick("web") == "free-1"


def test_other_failures_do_not_consume_quota():
    r = make()
    assert r.pick("web") == "free-1"
    assert r.record_failure("free-1", 500, "upstream exploded") == "other"
    assert r.state.used("free-1", "2026-10") == 0


def test_everything_exhausted_returns_last_tool():
    r = make()
    for t in ("free-1", "free-2", "paid"): r.record_failure(t, 402, "payment required")
    assert r.pick("web") == "paid"


def test_hook_rewrites_only_known_pools():
    import asyncio
    r = make()
    d = asyncio.run(r.async_pre_call_hook(None, None, {"search_tool_name": "web", "query": "x"}, "asearch"))
    assert d["search_tool_name"] == "free-1" and d["metadata"]["quota_tool"] == "free-1"
    d = asyncio.run(r.async_pre_call_hook(None, None, {"search_tool_name": "other"}, "asearch"))
    assert d["search_tool_name"] == "other"
    d = asyncio.run(r.async_pre_call_hook(None, None, {"search_tool_name": "web"}, "acompletion"))
    assert d["search_tool_name"] == "web"


def test_invalid_key_blocks_the_tool_for_hours_not_months():
    c = Clock(); r = make(c)
    assert r.record_failure("free-1", 401, "Unauthorized: missing or invalid API key.") == "auth"
    assert r.pick("web") == "free-2"
    c.now = datetime(2026, 10, 15, 19, 0, tzinfo=timezone.utc)  # 7 h later, block is 6 h
    assert r.pick("web") == "free-1"


def test_forbidden_with_quota_wording_is_a_quota_block():
    r = make()
    assert r.record_failure("free-1", 403, "You exceeded your plan limit") == "quota"


def test_hook_fails_open_when_state_is_broken():
    import asyncio
    r = make()
    r.state = None  # simulate a broken store
    d = asyncio.run(r.async_pre_call_hook(None, None, {"search_tool_name": "web", "query": "x"}, "asearch"))
    assert d["search_tool_name"] == "web"  # untouched: LiteLLM's own routing takes over
    asyncio.run(r.async_log_failure_event({"exception": RuntimeError("x"), "metadata": {"quota_tool": "free-1"}}, None, None, None))
