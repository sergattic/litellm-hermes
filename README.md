# litellm-hermes

A portable [LiteLLM](https://github.com/BerriAI/litellm) gateway for a fleet of [Hermes](https://github.com/NousResearch/hermes-agent) agents.
One URL and one auth for every Hermes instance, many provider keys behind it, and
**free-tier keys used first** with monthly quotas, so adding a feature means changing one place instead of every agent.

No secrets in this repo: keys live in `.env` (gitignored) and are referenced from the config as `os.environ/NAME`.

## What you get

| Piece | What it does |
|---|---|
| `docker-compose.yml` | LiteLLM + its own Postgres (virtual keys, spend tracking). Binds to loopback; nothing host-specific. |
| `callbacks/quota_router.py` | Free-first routing for **search tools**: ordered pools, monthly quotas per key, automatic blocking of exhausted or dead keys. SQLite state, no extra service. |
| `config/litellm.config.yaml` | Example config: one search tool per key, fallbacks, DB, callback. |
| `config/quota_pools.yaml` | The pools: which keys are tried, in what order, with what monthly allowance. |
| `scripts/gen_fallbacks.py` | Prints the `router_settings.fallbacks` block that matches your pools. |
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

1. Every key is its own LiteLLM search tool with a **unique** name.
2. `config/quota_pools.yaml` lists tools per pool, in priority order, with an optional `monthly` allowance.
3. Clients call the pool name. The hook picks the first tool that still has quota and is not blocked, and counts the use.
4. A quota error blocks the tool until the 1st of next month; a plain rate limit blocks it for 15 minutes; an invalid key for 6 hours (tunable with `QUOTA_RATE_COOLDOWN_S` and `QUOTA_AUTH_BLOCK_S`).
5. If a call still fails, LiteLLM's `router_settings.fallbacks` move it to the next tool in the same request.

Why a hook at all? LiteLLM picks at random between search tools that share a name and has no quota notion for them. See `docs/FINDINGS.md` for the measurements.

## Adding a search engine LiteLLM does not support

LiteLLM ships with about 17 search providers. For anything else, run a tiny service that speaks the
SearXNG JSON format and register it as a `searxng` tool with `api_base` pointing at it; it then takes part in pools,
quotas, fallbacks and auth like any other tool.

## Known limits

- Usage is counted when a tool is picked. A request served by a *fallback* tool is not counted against that tool, so quotas can undercount slightly after failures.
- Quotas are soft under heavy concurrency (a few extra calls can slip past the limit).
- One LiteLLM instance only: the quota state is a local SQLite file.
- LiteLLM has no page-extraction endpoint; extraction needs a separate service.

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
