#!/usr/bin/env bash
# Verifies SearXNG's proxy rotation: needs Docker. Each proxy container has its own address, so the
# address the fake target sees tells which proxy carried the request.
set -u
cd "$(dirname "$0")"
P=lt-sxtest; PORT=${SEARXNG_TEST_PORT:-8099}; fails=0
trap 'docker compose -p $P down -v >/dev/null 2>&1' EXIT
docker compose -p $P up -d >/dev/null 2>&1 || { echo "compose up failed"; exit 1; }
for i in $(seq 1 40); do curl -sf -m 3 http://127.0.0.1:$PORT/healthz >/dev/null 2>&1 && break; sleep 2; done; sleep 3
seen() { for i in $(seq 1 $1); do curl -s -m 30 "http://127.0.0.1:$PORT/search?q=$2$i&format=json&engines=echo" | python3 -c 'import sys,json
r=json.load(sys.stdin);res=r.get("results",[]);print(res[0]["content"] if res else "NORESULT")'; done | sort | uniq -c | awk '{print $1"x"$2}' | tr '\n' ' '; }
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: expected [$3] got [$2]"; fails=$((fails+1)); fi; }
n=$(seen 24 a | tr ' ' '\n' | grep -c '^8x172')
check "24 searches spread evenly over 3 proxies (8 each)" "$n" "3"
# Per-engine proxy groups: engine echo-a may only use proxy 1 (.11), engine echo-b only proxies 2 and 3 (.12/.13).
ips() { for i in $(seq 1 $1); do curl -s -m 30 "http://127.0.0.1:$PORT/search?q=$3$i&format=json&engines=$2" | python3 -c 'import sys,json
r=json.load(sys.stdin);res=r.get("results",[]);print(res[0]["content"] if res else "NORESULT")'; done | sort -u | tr '\n' ' '; }
check "engine echo-a only ever exits through its own group (proxy 1)" "$(ips 10 echo-a g1)" "172.29.77.11 "
check "engine echo-b rotates only inside its group (proxies 2 and 3)" "$(ips 10 echo-b g2)" "172.29.77.12 172.29.77.13 "

# A proxy that is already down when SearXNG starts fails fast, and retries move the request to the next proxy.
# (A proxy that dies AFTER use and then hangs is NOT covered: those requests wait for the timeout and fail until
# the proxy is removed. See docs/FINDINGS.md. SearXNG has no proxy health checking.)
docker compose -p $P stop socks3 >/dev/null 2>&1
docker compose -p $P restart searxng >/dev/null 2>&1
for i in $(seq 1 40); do curl -sf -m 3 http://127.0.0.1:$PORT/healthz >/dev/null 2>&1 && break; sleep 2; done; sleep 3
r=$(seen 12 b)
check "with one proxy down from the start, all 12 searches succeed (retries)" "$(echo "$r" | grep -c NORESULT)" "0"
[ "$fails" = 0 ] && echo "ALL PASSED" || echo "$fails FAILED"
exit $fails
