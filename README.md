# litellm-hermes

A portable [LiteLLM](https://github.com/BerriAI/litellm) gateway for a fleet of [Hermes](https://github.com/NousResearch/hermes-agent) agents.
One URL and one auth for every Hermes instance, many provider keys behind it, and
**free-tier keys used first** (the ladder of the `resilient-search` plugin, run once), so adding a feature means changing one place instead of every agent.

No secrets in this repo: keys live in `.env` (gitignored) and are referenced from the config as `os.environ/NAME`.

## What you get

| Piece | What it does |
|---|---|
| `docker-compose.yml` | LiteLLM + its own Postgres (virtual keys, spend tracking). Binds to loopback; nothing host-specific. |
| `callbacks/quota_router.py` | Free-first routing for **search tools**: ordered pools, exhausted keys skipped until next month. The ladder logic of the Hermes `resilient-search` plugin, run once centrally. State in LiteLLM's own Postgres. |
| `config/litellm.config.yaml` | Example config: one search tool per key, fallbacks, DB, callback. |
| `config/quota_pools.yaml` | The pools: which keys are tried, in what order. |
| `tests/` | Unit tests for the routing logic and an end-to-end test (real LiteLLM + fake search backends). |

## Quick start

```bash
cp .env.example .env              # fill in secrets and provider keys, never commit it
docker compose up -d              # LiteLLM on 127.0.0.1:4000 (set LITELLM_PORT to change)
# one virtual key per Hermes instance, so usage and access are separate
curl -s http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' -d '{"key_alias":"hermes-<instance>"}'
# search through the pool named "web"
curl -s http://127.0.0.1:4000/v1/search/web -H "Authorization: Bearer <virtual key>" \
  -H 'Content-Type: application/json' -d '{"query":"hello","max_results":3}'
```

To share it with Hermes containers on another Docker network, use `docker-compose.hermes-net.example.yml`.
To reach it from other servers, put it behind a tunnel with access control; never publish the port directly.

## How routing works

This is the ladder of the Hermes `resilient-search` plugin, moved into one place:

1. Every key is its own LiteLLM search tool with a **unique** name (`tavily-free-1`, `tavily-free-2`, `exa-paid`...).
2. `config/quota_pools.yaml` lists the tools of a pool in ladder order, free first. Clients call the pool name.
3. For each request the hook drops the tools already **exhausted this month**, sends the request to the first one left, and gives LiteLLM the rest as that request's fallbacks, in order. A failure moves on to the next tool within the same request.
4. A tool is marked exhausted for the calendar month (UTC) when it fails with a quota/credit/rate-limit error: the message matches `usage limit | insufficient credit | 402 | 429 | rate limit exceed | quota exceed | out of credit`. A new month starts clean. There are no timers.
5. Any other failure (timeout, 500, bad key) just moves on to the next tool and marks nothing.
6. If every tool is exhausted, the last one still gets the request.

LiteLLM alone picks randomly between search tools and cannot skip exhausted ones; see `docs/FINDINGS.md`.

## Usage and billing

With the database enabled (it is, in `docker-compose.yml`) LiteLLM records every search call. Verified on v1.103.0:

| You want | Where |
|---|---|
| Spend and calls per **Hermes instance** | One virtual key per instance. `GET /key/info?key=...` (field `spend`), or the admin UI (Virtual Keys, Logs). |
| Calls and spend per **upstream key** (free-1, free-2, paid) | Each row in `GET /spend/logs` (and the Logs page in the UI) shows the tool that served the call as `model_group`. Calls per pool: `GET /spend/tags` (tag `search-pool:<pool>`). |
| Which keys are exhausted this month, and how many calls each took | Postgres table `quota_router.usage` (`calls`, `exhausted`, `marked_at` per tool and month). |

Caveats:

- The cost LiteLLM shows is its **list price per query**, also for free-tier calls (for example Tavily 0.008, Exa 0.005 USD). Treat it as an estimate of what the traffic would cost, not as your invoice; real paid usage is the paid tool's tag.
- The per-key group-by report (`/global/spend/report`) needs an enterprise licence. Tags and the key/log endpoints do not.
- The raw request log lives in Postgres, so it can also be queried directly or exported.

## Keyless engines with rotating IPs (optional)

Many keyless engines (DuckDuckGo, Brave, Google...) throttle or block a single datacenter address.
[SearXNG](https://github.com/searxng/searxng) queries them for you and can rotate its outgoing proxy per request.
It runs as its own container next to LiteLLM, not inside it; LiteLLM talks to it through its built-in `searxng` provider,
so it takes part in pools, fallbacks and per-instance auth like any other search tool.

```bash
cp config/searxng/settings.yml.example config/searxng/settings.yml   # list your SOCKS/HTTP proxies
docker compose --profile keyless up -d
```

Then add a tool and put it in a pool (it has no key, so it is never marked exhausted by quota):

```yaml
# litellm.config.yaml
search_tools:
  - search_tool_name: searxng-keyless
    litellm_params: {search_provider: searxng, api_base: "http://searxng:8080"}
# quota_pools.yaml: put it after the free keys, before the paid one, e.g.
#   web: [tavily-free-1, searxng-keyless, exa-paid]
```

**Different proxies for different engines.** Give an engine its own `network:` block with its own `proxies` list (see
`config/searxng/settings.yml.example`): that engine uses only those proxies, everything else keeps the global list. This lets you
spend scarce residential proxies only on the engines that block datacenter addresses (Google, DuckDuckGo) and use cheap or home
proxies for the rest. Note: a custom `network` block replaces the engine-level settings, so repeat `enable_http: true` inside it
if you ever point an engine at a plain-http target (real engines use https and do not need it).

Measured (`tests/searxng/run.sh`): requests rotate evenly over the proxy list (24 searches, 3 proxies: 8 each), and a proxy
that is down when SearXNG starts is skipped by retries. **Limits:** SearXNG has no proxy health checks. A proxy that dies
and then hangs makes every search that lands on it fail after the timeout until you remove it from the list; check your
proxies regularly. Results quality differs from API providers, and engines still block individual proxy addresses.
SearXNG refuses plain-`http` targets unless an engine sets `enable_http: true` (real engines use https).

## Adding a search engine LiteLLM does not support

LiteLLM ships with about 17 search providers. For anything else, run a tiny service that speaks the
SearXNG JSON format and register it as a `searxng` tool with `api_base` pointing at it; it then takes part in pools,
quotas, fallbacks and auth like any other tool.

## Known limits

- An empty result list is returned as is; the plugin treats it as a failure and tries the next backend, this does not.
- Counting is not atomic across several LiteLLM replicas; run one instance.
- LiteLLM has no page-extraction endpoint; extraction needs a separate service.
- Only LiteLLM's built-in search providers are in the ladder. Other engines (SerpApi, Zyte...) come later as extra tools.

## Tests

```bash
docker run --rm -v "$PWD":/r python:3.12-slim sh -c 'pip -q install pytest pyyaml && cd /r && python -m pytest -q tests/test_quota_router.py'
tests/e2e/run.sh                  # needs Docker; starts and removes its own containers
```

## License

MIT

## Upgrading LiteLLM

The hook is a normal LiteLLM callback loaded from the config; nothing in LiteLLM is patched. It relies on:
the callback interface (`CustomLogger.async_pre_call_hook` and the failure event), the search call type
(`asearch`) with its `search_tool_name` field, and the router's search fallbacks. The search API is newer than
the rest of LiteLLM, so check it on every upgrade:

1. Pin the image (`LITELLM_IMAGE`, ideally with its `@sha256:` digest) and never follow a moving tag.
2. Before bumping the pin, run `LITELLM_IMAGE=<new image> tests/e2e/run.sh` and the unit tests.
3. If the hook breaks, it fails open: requests still work, with LiteLLM's own random choice between keys, and a
   line `quota_router: ... failed open` appears in the container logs. Watch for that line after an upgrade.
