"""Fake MCP servers for tests: a stdio one (run as a script) and an HTTP one with optional OAuth."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOOLS = [
    {"name": "echo", "description": "Echo text back", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "send_message", "description": "Send a message",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}},
]


def answer(msg: dict) -> dict | None:
    m, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        p = msg["params"]
        if p["name"] == "echo":
            res = {"content": [{"type": "text", "text": "echo: " + p["arguments"]["text"]}]}
        else:
            res = {"content": [{"type": "text", "text": f"sent to {p['arguments'].get('to')}"}]}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}}
    return {"jsonrpc": "2.0", "id": rid, "result": res}


def stdio_main():
    print("server log line that is not json", flush=True)
    for line in sys.stdin:
        out = answer(json.loads(line))
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


class HttpServer:
    """Streamable HTTP MCP server. With oauth=True it requires a bearer token issued by its own
    tiny authorization server (discovery + dynamic registration + PKCE token endpoint)."""

    def __init__(self, oauth=False, sse=False):
        self.oauth, self.sse = oauth, sse
        self.tokens = set()
        self.codes = {}
        self.registered = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, data, extra=None):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/.well-known/oauth-protected-resource/mcp":
                    return self._json(200, {"resource": srv.url + "/mcp", "authorization_servers": [srv.url]})
                if u.path == "/.well-known/oauth-authorization-server":
                    return self._json(200, {"authorization_endpoint": srv.url + "/authorize", "token_endpoint": srv.url + "/token",
                                            "registration_endpoint": srv.url + "/register"})
                self._json(404, {})

            def do_DELETE(self):
                self._json(200, {})

            def do_POST(self):
                u = urlparse(self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if u.path == "/register":
                    data = json.loads(raw)
                    srv.registered.append(data)
                    return self._json(201, {"client_id": "client-1", **data})
                if u.path == "/token":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    if form.get("grant_type") == "authorization_code" and form.get("code") in srv.codes:
                        tok = "tok-" + form["code"]
                        srv.tokens.add(tok)
                        return self._json(200, {"access_token": tok, "refresh_token": "r1", "expires_in": 3600})
                    return self._json(400, {"error": "invalid_grant"})
                if srv.oauth and self.headers.get("Authorization", "").removeprefix("Bearer ") not in srv.tokens:
                    return self._json(401, {"error": "unauthorized"}, {
                        "WWW-Authenticate": f'Bearer resource_metadata="{srv.url}/.well-known/oauth-protected-resource/mcp"'})
                msg = json.loads(raw)
                out = answer(msg)
                if out is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if srv.sse:
                    body = (": hi\n\n" + "event: message\ndata: " + json.dumps(out) + "\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Mcp-Session-Id", "s1")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json(200, out, {"Mcp-Session-Id": "s1"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    stdio_main()
