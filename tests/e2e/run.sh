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
h=$(count); a=$(echo "$h" | python3 -c 'import sys,json;h=json.load(sys.stdin);print(h.get("9001",0),h.get("9002",0),h.get("9004",0))')
check "ordered: free-a x5, free-b x5, paid x2" "$a" "5 5 2"

c1=$(search broken); h1=$(count | python3 -c 'import sys,json;print(json.load(sys.stdin).get("9003",0))')
check "broken: first call succeeds through fallback" "$c1" "200"
c2=$(search broken); c3=$(search broken); h3=$(count | python3 -c 'import sys,json;print(json.load(sys.stdin).get("9003",0))')
check "broken: later calls skip the exhausted tool (9003 hit once)" "$c2 $c3 $h3" "200 200 1"
rows=$(docker compose -p $P exec -T db psql -U l -d l -Atc "select tool||'='||used from quota_router.usage order by tool" 2>/dev/null | tr '\n' ' ')
check "quota counters are stored in Postgres (schema quota_router)" "$rows" "bad=1 free-a=5 free-b=5 good=2 paid=2 "
[ "$fails" = 0 ] && echo "ALL PASSED" || echo "$fails FAILED"
exit $fails
