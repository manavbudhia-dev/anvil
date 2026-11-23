"""A small Model Context Protocol client: stdio and Streamable HTTP transports.

Only what Anvil needs: initialize, tools/list and tools/call. Servers may send pings and other
requests; pings are answered, anything else gets "method not found".
"""

import json
import os
import queue
import subprocess
import tempfile
import threading
from itertools import chain, count

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "anvil", "version": "0.2"}
DEFAULT_TIMEOUT = 60.0
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class MCPError(Exception):
    pass


class AuthRequired(MCPError):
    """The server answered 401. `www_authenticate` carries the challenge (it may name the
    protected-resource metadata URL that starts MCP OAuth discovery)."""

    def __init__(self, msg: str, www_authenticate: str = ""):
        super().__init__(msg)
        self.www_authenticate = www_authenticate


def _rpc_error(err: dict) -> MCPError:
    return MCPError(f"{err.get('message', 'error')} (code {err.get('code')})")


class StdioTransport:
    """Runs the server as a child process and speaks newline-delimited JSON-RPC over its pipes."""

    def __init__(self, command: str, args: list[str] | None = None, env: dict | None = None, cwd: str | None = None):
        self.command, self.args = command, list(args or [])
        self.env, self.cwd = env or {}, cwd
        self.proc: subprocess.Popen | None = None
        self._ids = count(1)
        self._waiting: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr = None

    def start(self) -> None:
        import shutil

        exe = shutil.which(self.command) or self.command
        self._stderr = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen([exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self._stderr, cwd=self.cwd, env={**os.environ, **self.env},
                                         **NO_WINDOW)
        except OSError as e:
            raise MCPError(f"couldn't start {self.command!r}: {e}") from e
        threading.Thread(target=self._read_loop, name=f"mcp-stdio-{self.command}", daemon=True).start()

    def stderr_tail(self, n: int = 1500) -> str:
        if not self._stderr:
            return ""
        try:
            self._stderr.seek(0)
            return self._stderr.read().decode(errors="replace")[-n:].strip()
        except (OSError, ValueError):
            return ""

    def _send(self, msg: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError(f"server process exited. {self.stderr_tail()}".strip())
        data = (json.dumps(msg) + "\n").encode()
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise MCPError(f"server process closed its input: {e}. {self.stderr_tail()}".strip()) from e

    def _read_loop(self) -> None:
        out = self.proc.stdout
        for raw in iter(out.readline, b""):
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue  # servers sometimes log to stdout
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:  # a request from the server
                self._answer_server_request(msg)
            elif "id" in msg:
                with self._lock:
                    q = self._waiting.get(msg["id"])
                if q:
                    q.put(msg)
        with self._lock:  # process ended: wake everyone up
            for q in self._waiting.values():
                q.put({"error": {"code": -32000, "message": f"server process exited. {self.stderr_tail()}".strip()}})

    def _answer_server_request(self, msg: dict) -> None:
        try:
            if msg["method"] == "ping":
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": "method not supported by Anvil"}})
        except MCPError:
            pass

    def request(self, method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        rid = next(self._ids)
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._waiting[rid] = q
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                self._send_quiet({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": rid, "reason": "timeout"}})
                raise MCPError(f"{method} timed out after {timeout:.0f}s")
        finally:
            with self._lock:
                self._waiting.pop(rid, None)
        if "error" in msg:
            raise _rpc_error(msg["error"])
        return msg.get("result") or {}

    def _send_quiet(self, msg: dict) -> None:
        try:
            self._send(msg)
        except MCPError:
            pass

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        p = self.proc
        if not p:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        if self._stderr:
            self._stderr.close()


class _Resp:
    """A minimal HTTP response wrapper over urllib (no third-party HTTP client needed)."""

    def __init__(self, raw, status: int, headers):
        self.raw, self.status_code, self.headers = raw, status, headers

    def read(self) -> bytes:
        return self.raw.read() if self.raw else b""

    def iter_lines(self):
        for line in iter(self.raw.readline, b""):
            yield line.decode("utf-8", "replace").rstrip("\r\n")

    def close(self) -> None:
        if self.raw:
            self.raw.close()


def http_request(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> _Resp:
    """Send a request; HTTP error statuses are returned, not raised. Network failures raise OSError."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout)
        return _Resp(raw, raw.status, raw.headers)
    except urllib.error.HTTPError as e:
        return _Resp(e, e.code, e.headers)


class HttpTransport:
    """Streamable HTTP: every message is a POST; the answer is JSON or a short SSE stream."""

    def __init__(self, url: str, headers: dict | None = None, token=None):
        self.url = url
        self.headers = dict(headers or {})
        self.token = token  # callable returning a bearer token (or None), called on every request
        self.session_id: str | None = None
        self.protocol: str | None = None
        self._ids = count(1)

    def start(self) -> None:
        pass

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "anvil-mcp", **self.headers}
        tok = self.token() if self.token else None
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _post(self, msg: dict, timeout: float) -> _Resp:
        try:
            resp = http_request("POST", self.url, json.dumps(msg).encode(), self._headers(), timeout)
        except OSError as e:
            raise MCPError(f"couldn't reach {self.url}: {e}") from e
        if resp.status_code in (401, 403):
            body = resp.read().decode(errors="replace")[:300]
            resp.close()
            what = "the server needs you to sign in (401)" if resp.status_code == 401 else f"access denied (403): {body}"
            raise AuthRequired(what, resp.headers.get("WWW-Authenticate", "") or "")
        if resp.status_code >= 400:
            body = resp.read().decode(errors="replace")[:500]
            resp.close()
            raise MCPError(f"HTTP {resp.status_code} from {self.url}: {body}")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        return resp

    def request(self, method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        rid = next(self._ids)
        resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, timeout)
        try:
            ctype = resp.headers.get("Content-Type", "") or ""
            if "text/event-stream" in ctype:
                msg = self._from_sse(resp, rid)
            else:
                data = json.loads(resp.read() or b"{}")
                msg = next((m for m in data if m.get("id") == rid), {}) if isinstance(data, list) else data
        except (json.JSONDecodeError, OSError) as e:
            raise MCPError(f"bad response to {method}: {e}") from e
        finally:
            resp.close()
        if not msg:
            raise MCPError(f"no response to {method}")
        if "error" in msg:
            raise _rpc_error(msg["error"])
        return msg.get("result") or {}

    def _from_sse(self, resp: _Resp, rid: int) -> dict:
        data_lines: list[str] = []
        for line in chain(resp.iter_lines(), [""]):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "" and data_lines:
                try:
                    msg = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    msg = None
                data_lines = []
                if isinstance(msg, dict) and msg.get("id") == rid and "method" not in msg:
                    return msg
        return {}

    def notify(self, method: str, params: dict | None = None) -> None:
        resp = self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, 30)
        resp.close()

    def close(self) -> None:
        if self.session_id:
            try:
                http_request("DELETE", self.url, None, self._headers(), 5).close()
            except OSError:
                pass


class MCPClient:
    def __init__(self, transport):
        self.transport = transport
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions = ""

    def connect(self, timeout: float = 30) -> None:
        self.transport.start()
        res = self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}, timeout=timeout)
        self.server_info = res.get("serverInfo") or {}
        self.capabilities = res.get("capabilities") or {}
        self.instructions = res.get("instructions") or ""
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol = res.get("protocolVersion") or PROTOCOL_VERSION
        self.transport.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(50):  # pagination guard
            res = self.transport.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in res.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float = 300) -> dict:
        return self.transport.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass


def result_to_blocks(result: dict) -> tuple[list[dict] | str, bool]:
    """Convert a tools/call result into tool_result content for the model."""
    blocks: list[dict] = []
    for c in result.get("content") or []:
        t = c.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": c.get("text", "")})
        elif t == "image" and c.get("data"):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": c.get("mimeType", "image/png"),
                                                      "data": c["data"]}})
        elif t == "resource":
            r = c.get("resource") or {}
            blocks.append({"type": "text", "text": r.get("text") or f"[resource {r.get('uri', '')}]"})
        elif t == "resource_link":
            blocks.append({"type": "text", "text": f"[{c.get('name', 'link')}] {c.get('uri', '')}"})
        elif t == "audio":
            blocks.append({"type": "text", "text": "[audio content omitted]"})
    if not blocks and result.get("structuredContent") is not None:
        blocks.append({"type": "text", "text": json.dumps(result["structuredContent"], indent=2)[:30000]})
    is_error = bool(result.get("isError"))
    if all(b["type"] == "text" for b in blocks):
        return "\n".join(b["text"] for b in blocks) or "(no output)", is_error
    return blocks, is_error
