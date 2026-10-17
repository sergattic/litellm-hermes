# Findings (LiteLLM v1.103.0)

Measured with throwaway containers and fake search backends, not taken from documentation.

| Question | Result |
|---|---|
| Several keys under one search tool name | LiteLLM picks one **at random** (`random.choice` in `router_utils/search_api_router.py`, with a comment that search tools have no TPM/RPM limits). |
| What happens when one of them is exhausted | With 3 same-name keys, one returning 429: 15 of 40 requests (about a third) hit it and **failed**. `num_retries` did not retry onto another key. |
| Fallback to a differently named tool | **Works**: 5 of 5 requests served by the fallback when the primary kept returning 429. |
| Fallback on an authentication error | Does not trigger (401 returned to the caller). Hence the router blocks dead keys itself. |
| Budgets, priorities or quotas for search tools | None in the search code path. The cost calculator only prices queries per provider. |
| Can a hook see and change search calls | **Yes.** `async_pre_call_hook` receives `call_type="asearch"` with `search_tool_name` and `query`, and rewriting `search_tool_name` changes which tool serves the call. |
| Does the stock image support a database | Yes: Prisma and the schema are bundled; setting `DATABASE_URL` is enough. Virtual keys work. |
| Providers built in | brave, dataforseo, duckduckgo, exa_ai, fastcrw, firecrawl, google_pse, linkup, parallel_ai, perplexity, searchapi, searxng, serper, tavily, you_com, apiserpent, tinyfish. No SerpApi, Zyte, Diffbot, ScrapingBee, Supadata or Scrape Creators. |
| Extraction | No endpoint for it. |

## Not yet verified

- Behaviour with real provider keys (everything above used fake backends).
- How each real provider words its quota errors; `QUOTA_WORDS` in the router is a first guess.
- Spend reporting per virtual key for search calls with a database attached.
