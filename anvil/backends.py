"""Model backends. The agent keeps history in Anthropic message format (lists of
content-block dicts); each backend sends that history and returns a Response whose
content uses the same block format."""

import copy
import json
import threading
import time
from dataclasses import dataclass, field

import anthropic

from .config import FALLBACK_MODELS, Config


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_write_1h: int = 0  # part of cache_creation_input_tokens written with the 1-hour TTL


@dataclass
class Response:
    content: list[dict]
    stop_reason: str
    model: str
    usage: Usage = field(default_factory=Usage)
    refusal_explanation: str | None = None


def _usage(u) -> Usage:
    creation = getattr(u, "cache_creation", None)
    one_hour = getattr(creation, "ephemeral_1h_input_tokens", 0) if creation is not None else 0
    return Usage(u.input_tokens or 0, u.output_tokens or 0,
                 getattr(u, "cache_read_input_tokens", 0) or 0,
                 getattr(u, "cache_creation_input_tokens", 0) or 0, one_hour or 0)


class MalformedToolInput(Exception):
    """The model streamed tool-input JSON that couldn't be parsed; re-issue the turn."""


def _dump(block) -> dict:
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", by_alias=True, exclude_none=True)


# --------------------------------------------------------------------------
# Anthropic messages format (api.anthropic.com or a compatible gateway)
# --------------------------------------------------------------------------

class AnthropicBackend:
    def __init__(self, config: Config, ui):
        self.config = config
        self.ui = ui
        self.gateway = bool(config.resolved_base_url())
        # Request "levels", most capable first. Gateways may reject newer fields; on a 400 we step
        # down one level and stay there for the session, keeping prompt caching as long as possible:
        #   full   - thinking, effort, refusal fallback, cache marker on the system prompt + automatic caching
        #   blocks - basic request with explicit cache markers on content blocks (accepted by most gateways)
        #   plain  - same without the 1-hour TTL field
        #   none   - no caching
        self.levels = ["full", "blocks", "plain", "none"]
        if config.cache_ttl != "1h":
            self.levels.remove("plain")
        self.level = "blocks" if config.compat_mode else "full"
        self.inflight = 0
        self.last_start = 0.0
        self.ping_ok = True
        self._lock = threading.Lock()
        key = config.resolved_key()
        kwargs = {"api_key": key, "max_retries": 4}
        if self.gateway:
            kwargs["base_url"] = config.resolved_base_url()
            if key:
                kwargs["default_headers"] = {"Authorization": f"Bearer {key}", "x-api-key": key}
        self.client = anthropic.Anthropic(**kwargs)

    @property
    def minimal(self) -> bool:
        return self.level != "full"

    @property
    def caching(self) -> bool:
        return self.level != "none"

    @property
    def supports_web_search(self) -> bool:
        return not self.minimal

    def _marker(self) -> dict:
        m = {"type": "ephemeral"}
        if self.config.cache_ttl == "1h" and self.level in ("full", "blocks"):
            m["ttl"] = "1h"
        return m

    def _kwargs(self, system: str, tools: list[dict], messages: list[dict], max_tokens: int | None) -> dict:
        model = self.config.model
        if self.minimal:
            tools = [{k: v for k, v in t.items() if k != "eager_input_streaming"} for t in tools if "input_schema" in t]
        if self.caching:
            # A fixed read point on the system prompt (tools render before it, so both are covered).
            system_param = [{"type": "text", "text": system, "cache_control": self._marker()}]
        else:
            system_param = system
        kwargs = dict(
            model=model,
            max_tokens=max_tokens or self.config.max_tokens or (8192 if self.minimal else 64000),
            system=system_param, tools=tools, messages=messages,
        )
        if self.level == "full":
            kwargs["cache_control"] = self._marker()  # automatic breakpoint that follows the conversation
            if not model.startswith("claude-haiku-4"):
                kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
                kwargs["output_config"] = {"effort": self.config.effort}
        elif self.caching and messages:
            kwargs["messages"] = self._mark_tail(messages)
        return kwargs

    def _mark_tail(self, messages: list[dict]) -> list[dict]:
        """Explicit breakpoints on the last block of the newest message and of the newest user turn
        before it (a second read point). Copies only what it marks: stored history stays unmarked."""
        out = list(messages)
        marked = 0
        for i in range(len(out) - 1, -1, -1):
            m = out[i]
            if marked == 0 or (m["role"] == "user" and marked == 1 and i < len(out) - 1):
                content = m["content"]
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                if not content or content[-1].get("type") in ("thinking", "redacted_thinking"):
                    continue
                new = copy.copy(m)
                new["content"] = list(content[:-1]) + [{**content[-1], "cache_control": self._marker()}]
                out[i] = new
                marked += 1
                if marked == 2:
                    break
        return out

    def _open(self, kwargs: dict):
        if not self.minimal and self.config.model in FALLBACK_MODELS:
            return self.client.beta.messages.stream(
                **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
        return self.client.messages.stream(**kwargs)

    def complete(self, system, tools, messages, max_tokens=None, show=True) -> Response:
        with self._lock:
            self.inflight += 1
            self.last_start = time.monotonic()
        try:
            while True:
                try:
                    return self._complete(system, tools, messages, max_tokens, show)
                except anthropic.BadRequestError as e:
                    nxt = self.levels.index(self.level) + 1
                    if nxt >= len(self.levels):
                        raise
                    self.ui.stream_aborted()
                    self.level = self.levels[nxt]
                    what = {"blocks": "compatibility mode (prompt caching kept)",
                            "plain": "compatibility mode with the default 5-minute cache",
                            "none": "compatibility mode without prompt caching"}[self.level]
                    self.ui.notice(f"Endpoint rejected the request ({e.message}). Retrying in {what}.")
        finally:
            with self._lock:
                self.inflight -= 1

    def ping(self, system, tools, messages) -> Usage | None:
        """Refresh the prompt cache without generating anything (max_tokens=0). Same prefix as a
        real request, so it bills only a cache read. Returns None if the endpoint refuses it."""
        if not (self.caching and self.ping_ok and messages):
            return None
        with self._lock:
            if self.inflight:
                return None
            self.inflight += 1
            self.last_start = time.monotonic()
        try:
            kwargs = self._kwargs(system, tools, messages, None)
            kwargs["max_tokens"] = 0
            if self.level == "full" and self.config.model in FALLBACK_MODELS:
                msg = self.client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"],
                                                       fallbacks="default")
            else:
                msg = self.client.messages.create(**kwargs)
            return _usage(msg.usage)
        except anthropic.APIError:
            self.ping_ok = False  # this endpoint doesn't take keep-alive requests; stop trying
            return None
        finally:
            with self._lock:
                self.inflight -= 1

    def _complete(self, system, tools, messages, max_tokens, show) -> Response:
        ui = self.ui
        try:
            with self._open(self._kwargs(system, tools, messages, max_tokens)) as stream:
                current = None
                for event in stream:
                    if not show:
                        continue
                    et = event.type
                    if et == "content_block_start":
                        current = event.content_block.type
                        if current == "text":
                            ui.text_start()
                        elif current == "thinking":
                            ui.thinking_start()
                        elif current == "fallback":
                            ui.notice("Model declined; continuing on the fallback model.")
                    elif et == "content_block_delta":
                        dt = event.delta.type
                        if dt == "text_delta":
                            ui.text_delta(event.delta.text)
                        elif dt == "thinking_delta":
                            ui.thinking_delta(event.delta.thinking)
                    elif et == "content_block_stop":
                        if current == "text":
                            ui.text_end()
                        elif current == "thinking":
                            ui.thinking_end()
                        current = None
                msg = stream.get_final_message()
        except ValueError as e:
            raise MalformedToolInput(str(e))
        details = getattr(msg, "stop_details", None)
        return Response(
            content=[_dump(b) for b in msg.content],
            stop_reason=msg.stop_reason or "end_turn",
            model=msg.model,
            usage=_usage(msg.usage),
            refusal_explanation=getattr(details, "explanation", None) if details else None,
        )


# --------------------------------------------------------------------------
# OpenAI chat-completions format (OpenRouter, LiteLLM, most gateways)
# --------------------------------------------------------------------------

def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "user":
            texts, images = [], []
            for b in content:
                if b["type"] == "tool_result":
                    c = b.get("content", "")
                    if isinstance(c, list):
                        images += [f"data:{x['source']['media_type']};base64,{x['source']['data']}"
                                   for x in c if isinstance(x, dict) and x.get("type") == "image"]
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    if b.get("is_error"):
                        c = f"[error] {c}"
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": c})
                elif b["type"] == "text":
                    texts.append(b["text"])
            if images:  # tool messages can't carry images in this format
                out.append({"role": "user", "content": [{"type": "text", "text": "Screenshot(s) from the browser tool:"}]
                            + [{"type": "image_url", "image_url": {"url": u}} for u in images]})
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
        else:
            text = "".join(b.get("text", "") for b in content if b["type"] == "text")
            calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                for b in content if b["type"] == "tool_use"
            ]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
        for t in tools if "input_schema" in t
    ]


FINISH_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use",
              "length": "max_tokens", "content_filter": "refusal"}


class OpenAIBackend:
    supports_web_search = False

    def __init__(self, config: Config, ui):
        import openai  # imported lazily so Anthropic-only users don't need it configured

        self.openai = openai
        self.config = config
        self.ui = ui
        self.send_usage_option = True
        mode = (config.openai_cache_control or "auto").lower()
        model = config.model.lower()
        self.cache_markers = mode == "on" or (mode == "auto" and ("claude" in model or "anthropic" in model))
        self.caching = True  # providers cache stable prefixes automatically; markers add it for Claude
        self.inflight = 0
        self.last_start = 0.0
        self.client = openai.OpenAI(
            api_key=config.resolved_key() or "none",
            base_url=config.resolved_base_url() or None,
            max_retries=4,
        )

    def ping(self, system, tools, messages):
        return None  # no zero-token keep-alive in this format

    def _with_cache_markers(self, oa_messages: list[dict]) -> list[dict]:
        """cache_control on the system prompt and the newest user message, in the content-part form
        that OpenRouter, LiteLLM and similar gateways pass through to Claude."""
        out = list(oa_messages)
        targets = [0] if out and out[0]["role"] == "system" else []
        last_user = next((i for i in range(len(out) - 1, 0, -1) if out[i]["role"] == "user"), None)
        if last_user is not None:
            targets.append(last_user)
        for i in targets:
            m = dict(out[i])
            c = m["content"]
            parts = [{"type": "text", "text": c}] if isinstance(c, str) else [dict(x) for x in c]
            if parts:
                parts[-1]["cache_control"] = {"type": "ephemeral"}
                m["content"] = parts
                out[i] = m
        return out

    def complete(self, system, tools, messages, max_tokens=None, show=True) -> Response:
        self.inflight += 1
        try:
            return self._complete(system, tools, messages, max_tokens, show)
        finally:
            self.inflight -= 1

    def _complete(self, system, tools, messages, max_tokens, show) -> Response:
        oa_messages = to_openai_messages(system, messages)
        kwargs = dict(
            model=self.config.model,
            messages=self._with_cache_markers(oa_messages) if self.cache_markers else oa_messages,
            stream=True,
        )
        oa_tools = to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
        if max_tokens or self.config.max_tokens:
            kwargs["max_tokens"] = max_tokens or self.config.max_tokens
        if self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        if self.send_usage_option:
            kwargs["stream_options"] = {"include_usage": True}
        while True:
            try:
                return self._stream(kwargs, show)
            except self.openai.BadRequestError:
                self.ui.stream_aborted()
                if self.cache_markers:  # this gateway doesn't take cache markers
                    self.cache_markers = False
                    kwargs["messages"] = oa_messages
                    self.ui.notice("This endpoint rejected cache markers; continuing without them.")
                elif self.send_usage_option:  # some gateways reject stream_options
                    self.send_usage_option = False
                    kwargs.pop("stream_options", None)
                else:
                    raise

    def _stream(self, kwargs: dict, show: bool) -> Response:
        ui = self.ui
        text, finish, model = "", None, self.config.model
        calls: dict[int, dict] = {}
        usage = Usage()
        text_open = thinking_open = False
        for chunk in self.client.chat.completions.create(**kwargs):
            if getattr(chunk, "model", None):
                model = chunk.model
            if getattr(chunk, "usage", None):
                u = chunk.usage
                details = getattr(u, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) or 0
                written = (getattr(details, "cache_write_tokens", 0) or 0) if details is not None else 0
                usage = Usage(max((u.prompt_tokens or 0) - cached - written, 0), u.completion_tokens or 0, cached, written)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is not None:
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning and show:
                    if not thinking_open:
                        ui.thinking_start()
                        thinking_open = True
                    ui.thinking_delta(reasoning)
                if delta.content:
                    if thinking_open:
                        ui.thinking_end()
                        thinking_open = False
                    if show and not text_open:
                        ui.text_start()
                        text_open = True
                    text += delta.content
                    if show:
                        ui.text_delta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index if tc.index is not None else len(calls),
                                            {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["args"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason
        if thinking_open:
            ui.thinking_end()
        if text_open:
            ui.text_end()

        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        for i, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"__unparseable_arguments__": slot["args"][:2000]}
            content.append({"type": "tool_use", "id": slot["id"] or f"call_{i}", "name": slot["name"], "input": args})
        stop = FINISH_MAP.get(finish or "stop", "end_turn")
        if calls and stop == "end_turn":
            stop = "tool_use"  # some gateways report "stop" even when they return tool calls
        return Response(content=content, stop_reason=stop, model=model, usage=usage)


def make_backend(config: Config, ui):
    if config.resolved_provider() == "openai":
        return OpenAIBackend(config, ui)
    return AnthropicBackend(config, ui)
