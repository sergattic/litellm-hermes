# Findings (LiteLLM v1.103.0)

Correction (verified in production): in `GET /spend/logs`, a request served by a fallback tool is logged with `model_group` = the tool it was first sent to. Per-tool attempts are therefore taken from the failure/success events (`standard_logging_object.model_group`, correct per attempt) into `quota_router.usage`.

Measured with throwaway containers and fake search backends, not taken from documentation.

| Question | Result |
|---|---|
| Several keys under one search tool name | LiteLLM picks one **at random** (`random.choice` in `router_utils/search_api_router.py`, with a comment that search tools have no TPM/RPM limits). |
| What happens when one of them is exhausted | With 3 same-name keys, one returning 429: 15 of 40 requests (about a third) hit it and **failed**. `num_retries` did not retry onto another key. |
| Fallback to a differently named tool | **Works**: 5 of 5 requests served by the fallback when the primary kept returning 429. |
| Fallback on an authentication error | Does not trigger (401 returned to the caller). Hence the router blocks dead keys itself. |
| Budgets, priorities or quotas for search tools | None in the search code path. The cost calculator only prices queries per provider. |
| Can a hook see and change search calls | **Yes.** `async_pre_call_hook` receives `call_type="asearch"` with `search_tool_name` and `query`, and rewriting `search_tool_name` changes which tool serves the call. |
| Per-request fallback list (`fallbacks` set by the hook) for search | **Works**: the request falls through the given tools in order within the same call, and each failing attempt is reported with its own tool as `model_group` (verified in `tests/e2e`). This is what lets the ladder skip exhausted tools. |
| Does the stock image support a database | Yes: Prisma and the schema are bundled; setting `DATABASE_URL` is enough. Virtual keys work. |
| Providers built in | brave, dataforseo, duckduckgo, exa_ai, fastcrw, firecrawl, google_pse, linkup, parallel_ai, perplexity, searchapi, searxng, serper, tavily, you_com, apiserpent, tinyfish. No SerpApi, Zyte, Diffbot, ScrapingBee, Supadata or Scrape Creators. |
| Extraction | No endpoint for it. |

## SearXNG with rotating proxies (v2026.10.17)

| Question | Result |
|---|---|
| Does it rotate the outgoing proxy | **Yes**, per request, round-robin (`itertools.cycle` in `searx/network/network.py`). Test: 24 searches over 3 SOCKS proxies, each with its own address: 8 each. It also has `outgoing.source_ips` for hosts with several local addresses. |
| SOCKS and HTTP proxies | Both work (`socks5h://` tested). |
| A proxy that is down from the start | Fast failure; with `retries: 2` all 12 searches succeeded through the others. |
| A proxy that dies after use and then hangs | **Not handled**: requests that reach it wait for the timeout and fail (about every other search in the test) and the engine is marked `Suspended`. No health checking or ejection. |
| Different proxy groups per engine | **Yes.** An engine's own `network:` block (with `proxies`) replaces the global list for that engine only. Test: engine A exits only through proxy 1, engine B rotates only over proxies 2 and 3. A custom `network` block also replaces the engine-level `enable_http`. |
| Plain `http://` targets | Refused unless the engine sets `enable_http: true` (cost me an hour; real engines use https). |
| Real keyless engines from a single datacenter address | Bing, Mojeek, Startpage and Yahoo answered; DuckDuckGo gave a CAPTCHA, Brave rate-limited (too many requests), Google and Qwant answered access denied. This is what rotating addresses is meant to improve (not tested with real rotating proxies). |

## Not yet verified

- Behaviour with real provider keys (everything above used fake backends).
- How each provider words its quota errors; the router uses the plugin's patterns, proven for Tavily's and Parallel's real messages (Tavily verified live).
- Spend reporting per virtual key for search calls with a database attached.
