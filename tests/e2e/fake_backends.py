"""Fake SearXNG-style search backends. 9001/9002/9004/9005 answer; 9003 always says its quota is exhausted."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HITS = {}

def make(port, exhausted):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.startswith("/__count"):
                body = json.dumps(HITS).encode(); self.send_response(200); self.end_headers(); self.wfile.write(body); return
            HITS[port] = HITS.get(port, 0) + 1
            if exhausted:
                self.send_response(429); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"error":"Your account has run out of searches"}'); return
            body = json.dumps({"query": "x", "results": [{"title": f"served by {port}", "url": f"http://example.com/{port}", "content": "c"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        do_POST = do_GET
    return ThreadingHTTPServer(("0.0.0.0", port), H)

for p in (9001, 9002, 9003, 9004, 9005):
    threading.Thread(target=make(p, p == 9003).serve_forever, daemon=True).start()
threading.Event().wait()
