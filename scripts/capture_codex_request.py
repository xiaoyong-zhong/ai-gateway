"""One-request localhost-only diagnostic proxy; never records authorization headers."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        Path("runtime/codex-request.bin").write_bytes(body)
        print(json.dumps({"path": self.path, "bytes": len(body),
                          "encoding": self.headers.get("Content-Encoding")}), flush=True)
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in ("host", "connection", "content-length")}
        req = Request("http://192.168.31.113:8080" + self.path, data=body, headers=headers)
        try:
            upstream = urlopen(req, timeout=90)
        except HTTPError as error:
            upstream = error
        with upstream, Path("runtime/codex-response.sse").open("wb") as recording:
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "text/event-stream"))
            self.end_headers()
            while data := upstream.read1(65536):
                recording.write(data)
                self.wfile.write(data)
                self.wfile.flush()


server = HTTPServer(("127.0.0.1", 4011), Handler)
server.timeout = 60
print("Listening for one diagnostic request on 127.0.0.1:4011", flush=True)
try:
    server.handle_request()
finally:
    server.server_close()
