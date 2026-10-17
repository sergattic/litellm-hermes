#!/usr/bin/env bash
# End-to-end test: real LiteLLM + fake search backends. Needs Docker. Run from anywhere: tests/e2e/run.sh
set -u
cd "$(dirname "$0")"
P=lt-e2e; PORT=${E2E_PORT:-4011}; U=http://127.0.0.1:$PORT; fails=0
trap 'docker compose -p $P down -v >/dev/null 2>&1' EXIT
docker compose -p $P up -d >/dev/null 2>&1 || { echo "compose up failed"; exit 1; }
for i in $(seq 1 60); do curl -sf -m 2 $U/health/liveliness >/dev/null 2>&1 && break; sleep 3; done
count() { docker compose -p $P exec -T fake python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9001/__count").read().decode())'; }
search() { curl -s -o /tmp/e2e_out -w '%{http_code}' -m 30 -H 'Authorization: Bearer sk-test' -H 'Content-Type: application/json' $U/v1/search/$1 -d '{"query":"t","max_results":1}'; }
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: expected $3, got $2"; fails=$((fails+1)); fi; }

codes=""; for i in $(seq 1 12); do codes="$codes$(search ordered) "; done
check "ordered: all 12 requests succeed" "$(echo $codes | tr ' ' '\n' | sort -u | tr '\n' ' ')" "200 "
a=$(count | python3 -c 'import sys,json;h=json.load(sys.stdin);print(h.get("9001",0),h.get("9002",0),h.get("9004",0))')
check "ordered: free-a 5 ok + 1 quota error, free-b 5 ok + 1 quota error, paid x2 (each exhausted tool is skipped afterwards)" "$a" "6 6 2"

c1=$(search broken); c2=$(search broken); c3=$(search broken); h=$(count | python3 -c 'import sys,json;print(json.load(sys.stdin).get("9003",0))')
check "broken: falls through to the next tool and never hits the exhausted one again (9003 hit once)" "$c1 $c2 $c3 $h" "200 200 200 1"

f=""; for i in 1 2 3; do f="$f$(search flaky) "; done; h=$(count | python3 -c 'import sys,json;print(json.load(sys.stdin).get("9006",0))')
check "flaky: a non-quota failure moves on every time and never marks the tool (9006 hit 3 times)" "$f$h" "200 200 200 3"

sleep 3
rows=$(docker compose -p $P exec -T db psql -U l -d l -Atc "select tool||'='||calls||'/'||case when exhausted then 'exhausted' else 'ok' end from quota_router.usage order by tool" 2>/dev/null | tr '\n' ' ')
check "Postgres holds calls and exhausted flags per tool" "$rows" "bad=1/exhausted flaky=3/ok free-a=6/exhausted free-b=6/exhausted good=6/ok paid=2/ok "
[ "$fails" = 0 ] && echo "ALL PASSED" || echo "$fails FAILED"
exit $fails
