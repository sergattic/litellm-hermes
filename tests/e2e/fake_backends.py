"""Fake SearXNG-style search backends.
9001/9002 answer 5 times, then say their quota is used up; 9003 always does; 9004/9005 always answer; 9006 always fails with a 500."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HITS = {}
LIMIT = {9001: 5, 9002: 5}

def make(port, exhausted):
    status500 = port == 9006
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.startswith("/__count"):
                body = json.dumps(HITS).encode(); self.send_response(200); self.end_headers(); self.wfile.write(body); return
            HITS[port] = HITS.get(port, 0) + 1
            if status500:
                self.send_response(500); self.end_headers(); self.wfile.write(b"upstream broke"); return
            if exhausted or HITS[port] > LIMIT.get(port, 10**9):
                self.send_response(429); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"error":"This request exceeds this API key\'s set usage limit"}'); return
            body = json.dumps({"query": "x", "results": [{"title": f"served by {port}", "url": f"http://example.com/{port}", "content": "c"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        do_POST = do_GET
    return ThreadingHTTPServer(("0.0.0.0", port), H)

for p in (9001, 9002, 9003, 9004, 9005, 9006):
    threading.Thread(target=make(p, p == 9003).serve_forever, daemon=True).start()
threading.Event().wait()
