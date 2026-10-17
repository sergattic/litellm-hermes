#!/usr/bin/env python3
"""Print the router_settings.fallbacks block for config/quota_pools.yaml (each tool falls back to the ones after it)."""
import sys
import yaml

path = sys.argv[1] if len(sys.argv) > 1 else "config/quota_pools.yaml"
pools = yaml.safe_load(open(path))["pools"]
rows = {}
for entries in pools.values():
    tools = [e["tool"] for e in entries]
    for i, t in enumerate(tools[:-1]):
        rows[t] = tools[i + 1:]
print("router_settings:\n  fallbacks:")
for t, rest in rows.items():
    print("    - {%s: [%s]}" % (t, ", ".join(rest)))
