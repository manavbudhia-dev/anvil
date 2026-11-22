"""Connectors: MCP servers Anvil can use as tools (Gmail, Notion, Slack, GitHub, ...).

Settings live in connectors.json (safe to sync between machines); tokens, keys and OAuth
credentials live in connector-secrets.json (0600, never synced). Plugins can also bring
connectors; those are read from the plugin and only their on/off switch is stored here.

Each connector's tools are offered to the model as  mcp__<connector>__<tool>.
"""

import hashlib
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

from .config import CONFIG_DIR
from .fsutil import atomic_write_text, read_text
from .mcp import AuthRequired, HttpTransport, MCPClient, MCPError, StdioTransport, result_to_blocks
from . import mcp_oauth

CONNECTORS_FILE = CONFIG_DIR / "connectors.json"
SECRETS_FILE = CONFIG_DIR / "connector-secrets.json"
_FILE_LOCK = threading.RLock()
RETRY_AFTER = 300  # seconds before a failed connector is tried again automatically
CONNECT_TIMEOUT = 40

# Presets. Only endpoints and commands checked against each vendor's own docs are listed;
# anything else can be added with the custom options.
PRESETS = [
    {"id": "google-workspace", "name": "Gmail, Drive & Calendar", "icon": "mail", "transport": "stdio",
     "command": "uvx", "args": ["workspace-mcp", "--tools", "{services}"], "auth": "none",
     "description": "Read and send email, search Drive, manage calendar events (Google Workspace MCP server).",
     "fields": [
         {"key": "services", "label": "Services", "target": "arg", "default": "gmail drive calendar",
          "help": "Space separated: gmail drive calendar docs sheets slides forms tasks contacts chat"},
         {"key": "GOOGLE_OAUTH_CLIENT_ID", "label": "Google OAuth client ID", "target": "env", "required": True},
         {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "label": "Google OAuth client secret", "target": "env",
          "secret": True, "required": True}],
     "notes": "Needs uv (https://docs.astral.sh/uv/) and Python 3.11+. Create a Desktop OAuth client in Google Cloud "
              "Console and enable the Gmail, Drive and Calendar APIs. The server opens Google sign-in on first use.",
     "docs": "https://pypi.org/project/workspace-mcp/"},
    {"id": "notion", "name": "Notion", "icon": "doc", "transport": "http", "url": "https://mcp.notion.com/mcp",
     "auth": "oauth", "description": "Search, read and edit pages and databases in your Notion workspace.",
     "fields": [], "docs": "https://developers.notion.com/guides/mcp/overview"},
    {"id": "slack", "name": "Slack", "icon": "chat", "transport": "http", "url": "https://mcp.slack.com/mcp",
     "auth": "oauth", "description": "Search messages, read channels and post in Slack.",
     "fields": [
         {"key": "client_id", "label": "Slack app client ID", "target": "oauth_client_id", "required": True},
         {"key": "client_secret", "label": "Slack app client secret", "target": "oauth_client_secret",
          "secret": True, "required": True}],
     "notes": "Slack only allows registered apps: create an app at api.slack.com/apps, add the redirect URL "
              "http://localhost:33418/callback and the user scopes you need, then paste its client ID and secret.",
     "docs": "https://docs.slack.dev/ai/slack-mcp-server/"},
    {"id": "github", "name": "GitHub", "icon": "code", "transport": "http", "url": "https://api.githubcopilot.com/mcp/",
     "auth": "bearer", "description": "Issues, pull requests, code search and repository files.",
     "fields": [{"key": "token", "label": "Personal access token", "target": "bearer", "secret": True, "required": True,
                 "help": "github.com → Settings → Developer settings → Personal access tokens"}],
     "docs": "https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/set-up-the-github-mcp-server"},
    {"id": "linear", "name": "Linear", "icon": "check", "transport": "http", "url": "https://mcp.linear.app/mcp",
     "auth": "oauth", "description": "Find, create and update Linear issues and projects.",
     "fields": [{"key": "token", "label": "API key (optional)", "target": "bearer", "secret": True,
                 "help": "Leave empty to sign in with your browser instead."}],
     "docs": "https://linear.app/docs/mcp"},
    {"id": "filesystem", "name": "Filesystem", "icon": "folder", "transport": "stdio", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-filesystem", "{path}"], "auth": "none",
     "description": "Give Anvil access to a folder outside the project.",
     "fields": [{"key": "path", "label": "Folder", "target": "arg", "required": True}],
     "notes": "Needs Node.js.", "docs": "https://github.com/modelcontextprotocol/servers"},
    {"id": "fetch", "name": "Fetch", "icon": "globe", "transport": "stdio", "command": "uvx",
     "args": ["mcp-server-fetch"], "auth": "none", "description": "Fetch web pages as Markdown.",
     "fields": [], "notes": "Needs uv.", "docs": "https://github.com/modelcontextprotocol/servers"},
    {"id": "custom-http", "name": "Custom (remote URL)", "icon": "plug", "transport": "http", "url": "",
     "auth": "oauth", "description": "Any remote MCP server reachable over HTTPS.", "custom": True, "fields": []},
    {"id": "custom-stdio", "name": "Custom (local command)", "icon": "plug", "transport": "stdio", "command": "",
     "args": [], "auth": "none", "description": "Any MCP server started as a local command.", "custom": True, "fields": []},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "connector"


# --- storage --------------------------------------------------------------------

def _load(path) -> dict:
    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_state() -> dict:
    with _FILE_LOCK:
        d = _load(CONNECTORS_FILE)
        d.setdefault("connectors", [])
        d.setdefault("overrides", {})
        return d


def save_state(d: dict) -> None:
    with _FILE_LOCK:
        atomic_write_text(CONNECTORS_FILE, json.dumps(d, indent=2))


def load_secrets() -> dict:
    with _FILE_LOCK:
        return _load(SECRETS_FILE)


def save_secrets(d: dict) -> None:
    with _FILE_LOCK:
        atomic_write_text(SECRETS_FILE, json.dumps(d, indent=2), private=True)


def update_secret(cid: str, **values) -> None:
    with _FILE_LOCK:
        s = load_secrets()
        entry = s.setdefault(cid, {})
        for k, v in values.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        save_secrets(s)


def all_connectors() -> list[dict]:
    """User connectors plus the ones enabled plugins bring."""
    st = load_state()
    out = [dict(c, source=c.get("source") or "user") for c in st["connectors"]]
    try:
        from .plugins import mcp_servers
        for c in mcp_servers():
            ov = st["overrides"].get(c["id"], {})
            # Off until you turn it on: a plugin's connector runs a program from the plugin.
            out.append({**c, "enabled": ov.get("enabled", False)})
    except Exception:
        pass
    return out


def get(cid: str) -> dict | None:
    return next((c for c in all_connectors() if c["id"] == cid), None)


def _apply_fields(preset: dict, values: dict, conn: dict, secret: dict) -> None:
    args_map = {}
    for f in preset.get("fields", []):
        v = values.get(f["key"])
        if v is None or v == "":
            v = f.get("default", "")
        v = str(v).strip()
        if f.get("required") and not v and not (f.get("secret") and _has_secret(conn, f, secret)):
            raise ValueError(f"{f['label']} is required")
        if not v:
            continue
        t = f["target"]
        if t == "arg":
            args_map[f["key"]] = v
        elif t == "env":
            (secret.setdefault("env", {}) if f.get("secret") else conn.setdefault("env", {}))[f["key"]] = v
        elif t == "bearer":
            secret["bearer"] = v
        elif t == "oauth_client_id":
            conn["oauth_client_id"] = v
        elif t == "oauth_client_secret":
            secret["oauth_client_secret"] = v
    if preset.get("args"):
        args = []
        for a in preset["args"]:
            m = re.fullmatch(r"\{(\w+)\}", a)
            if m:
                val = args_map.get(m.group(1), "")
                args += val.split() if m.group(1) == "services" else ([val] if val else [])
            else:
                args.append(a)
        conn["args"] = args


def _has_secret(conn: dict, f: dict, secret: dict) -> bool:
    t = f["target"]
    if t == "env":
        return bool(secret.get("env", {}).get(f["key"]))
    if t == "bearer":
        return bool(secret.get("bearer"))
    if t == "oauth_client_secret":
        return bool(secret.get("oauth_client_secret"))
    return False


def save_connector(data: dict) -> dict:
    """Create or update a user connector from the connectors screen."""
    st = load_state()
    cid = data.get("id") or ""
    existing = next((c for c in st["connectors"] if c["id"] == cid), None) if cid else None
    preset = PRESET_BY_ID.get(data.get("preset") or (existing or {}).get("preset") or "custom-http")
    if preset is None:
        raise ValueError("unknown connector type")
    conn = dict(existing or {})
    name = (data.get("name") or conn.get("name") or preset["name"]).strip()[:60]
    if not conn:
        base = slug(name)
        taken = {c["id"] for c in all_connectors()}
        cid = base if base not in taken else f"{base}-{secrets.token_hex(2)}"
        conn = {"id": cid, "preset": preset["id"], "created_at": _now(), "enabled": True}
    conn.update(name=name, transport=preset["transport"], auth=data.get("auth") or preset.get("auth", "none"),
                updated_at=_now())
    secret = dict(load_secrets().get(cid, {}))
    values = data.get("values") or {}
    if preset.get("custom"):
        if preset["transport"] == "http":
            url = str(data.get("url") or conn.get("url") or "").strip()
            if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError("Use an https:// URL (plain http only for localhost)")
            conn["url"] = url
            if values.get("token"):
                secret["bearer"] = values["token"].strip()
                conn["auth"] = "bearer"
            if values.get("client_id"):
                conn["oauth_client_id"] = values["client_id"].strip()
            if values.get("client_secret"):
                secret["oauth_client_secret"] = values["client_secret"].strip()
        else:
            cmd = str(data.get("command") or conn.get("command") or "").strip()
            if not cmd:
                raise ValueError("Command is required")
            import shlex
            parts = shlex.split(cmd, posix=True)
            conn["command"], conn["args"] = parts[0], parts[1:]
            env, senv = {}, {}
            for line in str(data.get("env") or "").splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    (senv if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I) else env)[k.strip()] = v.strip()
            if data.get("env") is not None:
                conn["env"] = env
                secret["env"] = {**secret.get("env", {}), **senv}
    else:
        conn["url"] = preset.get("url", "")
        conn["command"] = preset.get("command", "")
        _apply_fields(preset, values, conn, secret)
        if preset["id"] == "linear" and secret.get("bearer"):
            conn["auth"] = "bearer"
    if "enabled" in data:
        conn["enabled"] = bool(data["enabled"])
    st["connectors"] = [c for c in st["connectors"] if c["id"] != cid] + [conn]
    save_state(st)
    s = load_secrets()
    s[cid] = {**s.get(cid, {}), **secret}
    save_secrets(s)
    manager().forget(cid)
    return conn


def set_enabled(cid: str, enabled: bool) -> None:
    st = load_state()
    for c in st["connectors"]:
        if c["id"] == cid:
            c["enabled"], c["updated_at"] = bool(enabled), _now()
            break
    else:
        st["overrides"].setdefault(cid, {})["enabled"] = bool(enabled)
    save_state(st)
    manager().forget(cid)


def delete_connector(cid: str) -> None:
    st = load_state()
    st["connectors"] = [c for c in st["connectors"] if c["id"] != cid]
    save_state(st)
    s = load_secrets()
    s.pop(cid, None)
    save_secrets(s)
    manager().forget(cid)


def public_view(c: dict) -> dict:
    """What the UI may see: no secret values, only whether they're set."""
    sec = load_secrets().get(c["id"], {})
    st = manager().status.get(c["id"], {})
    preset = PRESET_BY_ID.get(c.get("preset", ""), {})
    return {"id": c["id"], "name": c.get("name", c["id"]), "preset": c.get("preset", ""), "source": c.get("source", "user"),
            "transport": c.get("transport"), "url": c.get("url", ""),
            "command": " ".join([c.get("command", "")] + list(c.get("args", []))).strip(),
            "env": c.get("env", {}), "secret_env": sorted(sec.get("env", {})), "auth": c.get("auth", "none"),
            "enabled": c.get("enabled", True), "signed_in": bool(sec.get("oauth", {}).get("access_token")),
            "token_set": bool(sec.get("bearer")), "client_id": c.get("oauth_client_id", ""),
            "state": st.get("state", "idle"), "error": st.get("error", ""), "tools": st.get("tools", []),
            "icon": preset.get("icon", "plug"), "updated_at": c.get("updated_at", "")}


# --- runtime --------------------------------------------------------------------

def tool_name(cid: str, tool: str) -> str:
    clean = lambda s: re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    name = f"mcp__{clean(cid)}__{clean(tool)}"
    if len(name) > 64:
        h = hashlib.sha1(name.encode()).hexdigest()[:6]
        name = name[:57] + "_" + h
    return name


def _clean_schema(schema) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    s = {k: v for k, v in schema.items() if k != "$schema"}
    s.setdefault("type", "object")
    if s["type"] == "object":
        s.setdefault("properties", {})
    return s


class Manager:
    """Live connections, shared by every session in the process."""

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
        self.tools: dict[str, list[dict]] = {}
        self.status: dict[str, dict] = {}
        self.names: dict[str, tuple[str, dict]] = {}   # full tool name -> (connector id, mcp tool)
        self.lock = threading.RLock()
        self._conn_locks: dict[str, threading.Lock] = {}
        self._token_lock = threading.Lock()
        self.listeners: list = []  # callables(cid) told when a status changes

    def _set(self, cid: str, **st) -> None:
        with self.lock:
            self.status[cid] = {**self.status.get(cid, {}), **st, "at": time.time()}
        for fn in list(self.listeners):
            try:
                fn(cid)
            except Exception:
                pass

    def _token_for(self, cid: str):
        def provider():
            sec = load_secrets().get(cid, {})
            conn = get(cid) or {}
            if conn.get("auth") == "bearer" or (sec.get("bearer") and not sec.get("oauth")):
                return sec.get("bearer") or None
            bundle = sec.get("oauth")
            if not bundle or not bundle.get("access_token"):
                return None
            if bundle.get("expires_at") and bundle["expires_at"] - 60 < time.time():
                with self._token_lock:
                    sec = load_secrets().get(cid, {})
                    bundle = sec.get("oauth", {})
                    if bundle.get("expires_at") and bundle["expires_at"] - 60 < time.time():
                        try:
                            bundle = mcp_oauth.refresh(bundle)
                            update_secret(cid, oauth=bundle)
                        except mcp_oauth.OAuthError:
                            update_secret(cid, oauth=None)
                            return None
            return bundle.get("access_token")
        return provider

    def _transport(self, c: dict):
        if c.get("transport") == "http":
            return HttpTransport(c["url"], c.get("headers") or {}, token=self._token_for(c["id"]))
        sec_env = load_secrets().get(c["id"], {}).get("env", {})
        return StdioTransport(c.get("command", ""), c.get("args", []), {**c.get("env", {}), **sec_env}, c.get("cwd"))

    def connect(self, cid: str, force: bool = False) -> MCPClient | None:
        lock = self._conn_locks.setdefault(cid, threading.Lock())
        with lock:
            if cid in self.clients and not force:
                return self.clients[cid]
            c = get(cid)
            if not c or not c.get("enabled", True):
                return None
            st = self.status.get(cid, {})
            if not force and st.get("state") in ("error", "needs_auth") and time.time() - st.get("at", 0) < RETRY_AFTER:
                return None
            old = self.clients.pop(cid, None)
            if old:
                old.close()
            self._set(cid, state="connecting", error="")
            client = MCPClient(self._transport(c))
            try:
                client.connect()
                tools = client.list_tools()
            except AuthRequired as e:
                client.close()
                self._set(cid, state="needs_auth", error=str(e), www_authenticate=e.www_authenticate, tools=[])
                return None
            except (MCPError, OSError) as e:
                client.close()
                self._set(cid, state="error", error=str(e)[:600], tools=[])
                return None
            with self.lock:
                self.clients[cid] = client
                self.tools[cid] = tools
                for t in tools:
                    self.names[tool_name(cid, t["name"])] = (cid, t)
            self._set(cid, state="ready", error="", tools=[t["name"] for t in tools])
            return client

    def forget(self, cid: str) -> None:
        with self.lock:
            client = self.clients.pop(cid, None)
            self.tools.pop(cid, None)
            self.names = {k: v for k, v in self.names.items() if v[0] != cid}
            self.status.pop(cid, None)
        if client:
            threading.Thread(target=client.close, daemon=True).start()
        self._set(cid, state="idle")

    def close_all(self) -> None:
        for cid in list(self.clients):
            self.forget(cid)

    def tool_defs(self) -> list[dict]:
        enabled = [c for c in all_connectors() if c.get("enabled", True)]
        todo = [c["id"] for c in enabled if c["id"] not in self.clients]
        if todo:
            pool = ThreadPoolExecutor(max_workers=min(8, len(todo)))
            futs = [pool.submit(self.connect, cid) for cid in todo]
            wait(futs, timeout=CONNECT_TIMEOUT)
            pool.shutdown(wait=False)
        defs = []
        enabled_ids = {c["id"]: c for c in enabled}
        with self.lock:
            for cid, tools in self.tools.items():
                if cid not in enabled_ids:
                    continue
                label = enabled_ids[cid].get("name", cid)
                for t in tools:
                    desc = f"[{label} connector] {t.get('description') or t.get('title') or t['name']}"
                    defs.append({"name": tool_name(cid, t["name"]), "description": desc[:4000],
                                 "input_schema": _clean_schema(t.get("inputSchema"))})
        defs.sort(key=lambda d: d["name"])  # stable order keeps the prompt cache warm
        return defs

    def lookup(self, name: str) -> tuple[str, dict] | None:
        with self.lock:
            return self.names.get(name)

    def call(self, name: str, args: dict):
        hit = self.lookup(name)
        if not hit:
            raise MCPError(f"connector tool {name} isn't available any more")
        cid, tool = hit
        client = self.clients.get(cid) or self.connect(cid, force=True)
        if client is None:
            raise MCPError(self.status.get(cid, {}).get("error") or "connector is not connected")
        try:
            res = client.call_tool(tool["name"], args)
        except AuthRequired as e:
            self.forget(cid)
            self._set(cid, state="needs_auth", error=str(e), www_authenticate=e.www_authenticate)
            raise MCPError("the connector's sign-in expired; reconnect it in Connectors") from e
        except MCPError as e:
            if "exited" in str(e) or "couldn't reach" in str(e):
                self.forget(cid)
            raise
        return result_to_blocks(res)

    # --- OAuth sign-in ------------------------------------------------------------
    def start_login(self, cid: str, on_done=None) -> str:
        c = get(cid)
        if not c or c.get("transport") != "http":
            raise ValueError("only remote connectors use browser sign-in")
        sec = load_secrets().get(cid, {})
        flow = mcp_oauth.Flow(c["url"], self.status.get(cid, {}).get("www_authenticate", ""),
                              client_id=c.get("oauth_client_id", ""), client_secret=sec.get("oauth_client_secret", ""),
                              scope=c.get("scope", ""))
        try:
            url = flow.start()
        except (mcp_oauth.OAuthError, Exception) as e:
            raise ValueError(f"couldn't start sign-in: {e}") from e
        self._set(cid, state="signing_in", error="")

        def finish():
            try:
                bundle = flow.wait()
                update_secret(cid, oauth=bundle)
                st = load_state()
                for conn in st["connectors"]:
                    if conn["id"] == cid and conn.get("auth") != "oauth":
                        conn["auth"] = "oauth"
                save_state(st)
                self.status.pop(cid, None)
                self.connect(cid, force=True)
            except Exception as e:
                self._set(cid, state="needs_auth", error=f"sign-in failed: {e}")
            if on_done:
                on_done(cid)

        threading.Thread(target=finish, daemon=True).start()
        return url

    def sign_out(self, cid: str) -> None:
        update_secret(cid, oauth=None)
        self.forget(cid)


_MANAGER: Manager | None = None
_MLOCK = threading.Lock()


def manager() -> Manager:
    global _MANAGER
    with _MLOCK:
        if _MANAGER is None:
            _MANAGER = Manager()
        return _MANAGER


# --- agent tool provider ------------------------------------------------------------

class ConnectorTools:
    """Plugs connector tools into the agent (see anvil/extensions.py)."""

    def tool_defs(self, agent) -> list[dict]:
        if not any(c.get("enabled", True) for c in all_connectors()):
            return []
        return manager().tool_defs()

    def owns(self, name: str) -> bool:
        return name.startswith("mcp__")

    def validate(self, name: str, args) -> str | None:
        hit = manager().lookup(name)
        if not hit:
            return f"unknown tool {name!r}"
        if not isinstance(args, dict):
            return "input is not a JSON object"
        for req in (hit[1].get("inputSchema") or {}).get("required", []) or []:
            if req not in args:
                return f"missing required field {req!r}"
        return None

    def approval(self, agent, name: str, args: dict) -> tuple[bool, list[str]]:
        hit = manager().lookup(name)
        ann = (hit[1].get("annotations") or {}) if hit else {}
        if ann.get("readOnlyHint") is True:
            return False, []
        if ann.get("destructiveHint") is True:
            return True, ["The connector marks this tool as destructive (it may delete or overwrite data)."]
        return True, []

    def preview(self, agent, name: str, args: dict) -> str:
        hit = manager().lookup(name)
        if not hit:
            return name
        c = get(hit[0]) or {}
        return f"{c.get('name', hit[0])} → {hit[1]['name']}\n{json.dumps(args, indent=2, ensure_ascii=False)[:4000]}"

    def label(self, name: str) -> str:
        hit = manager().lookup(name)
        if not hit:
            return ""
        c = get(hit[0]) or {}
        return f"{c.get('name', hit[0])} · {hit[1]['name']}"

    def run(self, agent, name: str, args: dict):
        content, is_error = manager().call(name, args)
        if is_error:
            from .tools import ToolError
            raise ToolError(content if isinstance(content, str) else "the connector reported an error")
        return content
