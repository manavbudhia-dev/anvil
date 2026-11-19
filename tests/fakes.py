"""In-process fake model APIs for tests. Each test scripts the replies and inspects the requests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def text(t: str) -> dict:
    return {"kind": "text", "text": t}


def tool(name: str, **inp) -> dict:
    return {"kind": "tool", "calls": [(name, inp)]}


def tools(*calls: tuple[str, dict]) -> dict:
    return {"kind": "tool", "calls": list(calls)}


class _Base:
    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[dict] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def next_reply(self, body: dict) -> dict:
        with self.lock:
            return self.script.pop(0) if self.script else text("done")

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                with fake.lock:
                    fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                fake.handle(self, body)

        return H

    @staticmethod
    def send_json(h, code: int, obj: dict):
        data = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    @staticmethod
    def send_sse(h, chunks: list[str]):
        h.send_response(200)
        h.send_header("content-type", "text/event-stream")
        h.end_headers()
        h.wfile.write("".join(chunks).encode())


def _has_marker(obj) -> bool:
    if isinstance(obj, dict):
        return "cache_control" in obj or any(_has_marker(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_marker(v) for v in obj)
    return False


class FakeAnthropic(_Base):
    """mode: official | strict (rejects top-level cache/thinking fields) | nocache (rejects any cache marker).
    Reports cache usage: the first real request writes the cache, later ones read it."""

    def __init__(self, mode: str = "official"):
        super().__init__()
        self.mode = mode
        self.real_calls = 0

    def handle(self, h, body):
        if self.mode == "strict" and any(k in body for k in ("cache_control", "thinking", "output_config")):
            return self.send_json(h, 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": "Extra inputs are not permitted"}})
        if self.mode == "nocache" and _has_marker(body):
            return self.send_json(h, 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": "cache_control not supported"}})
        model = body.get("model", "claude-opus-5-5")
        if body.get("max_tokens") == 0:  # keep-alive ping
            usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 9400,
                     "cache_creation_input_tokens": 0}
            return self.send_json(h, 200, {"id": "ping", "type": "message", "role": "assistant", "model": model,
                                           "content": [], "stop_reason": "max_tokens", "stop_sequence": None,
                                           "usage": usage})
        with self.lock:
            self.real_calls += 1
            n = self.real_calls
        reply = self.next_reply(body)
        usage = {"input_tokens": 50, "output_tokens": 1,
                 "cache_read_input_tokens": 0 if n == 1 else 9000,
                 "cache_creation_input_tokens": 9000 if n == 1 else 400}
        evs = [{"type": "message_start", "message": {"id": f"m{n}", "type": "message", "role": "assistant",
                                                     "model": model, "content": [], "stop_reason": None,
                                                     "stop_sequence": None, "usage": usage}}]
        if reply["kind"] == "text":
            evs += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply["text"]}},
                    {"type": "content_block_stop", "index": 0}]
            stop = "end_turn"
        else:
            for i, (name, inp) in enumerate(reply["calls"]):
                evs += [{"type": "content_block_start", "index": i,
                         "content_block": {"type": "tool_use", "id": f"tu{n}_{i}", "name": name, "input": {}}},
                        {"type": "content_block_delta", "index": i,
                         "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}},
                        {"type": "content_block_stop", "index": i}]
            stop = "tool_use"
        evs += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 20}},
                {"type": "message_stop"}]
        self.send_sse(h, [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in evs])


class FakeOpenAI(_Base):
    """OpenAI chat-completions format. reject_markers: 400 when a request carries cache_control."""

    def __init__(self, reject_markers: bool = False):
        super().__init__()
        self.reject_markers = reject_markers

    def handle(self, h, body):
        if self.reject_markers and _has_marker(body):
            return self.send_json(h, 400, {"error": {"message": "Unrecognized field cache_control",
                                                     "type": "invalid_request_error"}})
        reply = self.next_reply(body)

        def chunk(delta=None, finish=None, usage=None):
            c = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body.get("model"),
                 "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            if usage:
                c["usage"] = usage
            return f"data: {json.dumps(c)}\n\n"

        out = []
        if reply["kind"] == "text":
            out += [chunk({"role": "assistant", "content": reply["text"]}), chunk({}, "stop")]
        else:
            for i, (name, inp) in enumerate(reply["calls"]):
                out.append(chunk({"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                                  "function": {"name": name, "arguments": json.dumps(inp)}}]}))
            out.append(chunk({}, "tool_calls"))
        out.append(chunk(usage={"prompt_tokens": 1200, "completion_tokens": 10,
                                "prompt_tokens_details": {"cached_tokens": 1000}}))
        out.append("data: [DONE]\n\n")
        self.send_sse(h, out)
