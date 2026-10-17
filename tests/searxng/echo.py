"""Fake search target: answers every query with the source address it saw."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        ip = self.client_address[0]
        body = json.dumps({"results": [{"title": "seen from " + ip, "url": "http://echo.local/" + ip, "content": ip}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
