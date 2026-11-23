"""Artifacts: outputs worth keeping (an HTML page, a Markdown report, an SVG diagram, a snippet).

The agent saves them with the save_artifact tool; the desktop app lists and previews them.
One JSON file per artifact under artifacts/, with stable ids and timestamps so they can be
synced later. HTML is previewed in a sandboxed frame with no access to the app.
"""

import json
import re
import secrets
import threading
from datetime import datetime, timezone

from .config import CONFIG_DIR
from .fsutil import atomic_write_text, read_text

ARTIFACTS_DIR = CONFIG_DIR / "artifacts"
KINDS = ("html", "markdown", "svg", "code", "text")
MAX_CHARS = 2_000_000
_LOCK = threading.Lock()
ID_RE = re.compile(r"^[a-z0-9-]{6,64}$")

TOOL_DEF = {
    "name": "save_artifact",
    "description": (
        "Save a self-contained output the user will want to view or keep outside the chat: an HTML page or "
        "interactive demo, a Markdown report or doc, an SVG diagram, or a standalone code file. The user sees it "
        "in Anvil's Artifacts panel with a live preview. HTML must be one self-contained file (inline CSS/JS; "
        "CDN scripts are fine). To revise an artifact, pass its artifact_id; that keeps the same artifact and "
        "bumps its version. Don't use this for normal project files: write those with write_file."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["title", "kind", "content"],
                     "properties": {
                         "title": {"type": "string", "description": "Short name, 2-6 words"},
                         "kind": {"type": "string", "enum": list(KINDS)},
                         "content": {"type": "string", "description": "Full content of the artifact"},
                         "language": {"type": "string", "description": "For kind=code: the language, e.g. python"},
                         "artifact_id": {"type": "string", "description": "Id of an existing artifact to update"}}},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(aid: str):
    if not ID_RE.match(aid or ""):
        raise ValueError("bad artifact id")
    return ARTIFACTS_DIR / f"{aid}.json"


def save(title: str, kind: str, content: str, language: str = "", artifact_id: str = "",
         session_id: str = "", root: str = "", project_id: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if len(content) > MAX_CHARS:
        raise ValueError(f"artifact is too large ({len(content):,} chars; limit {MAX_CHARS:,})")
    with _LOCK:
        old = get(artifact_id) if artifact_id else None
        if artifact_id and not old:
            raise ValueError(f"no artifact with id {artifact_id!r}")
        now = _now()
        art = {
            "id": old["id"] if old else f"{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4)}",
            "title": (title or "Untitled").strip()[:120], "kind": kind, "language": language[:40],
            "content": content, "session_id": session_id or (old or {}).get("session_id", ""),
            "root": root or (old or {}).get("root", ""), "project_id": project_id or (old or {}).get("project_id", ""),
            "created_at": (old or {}).get("created_at", now), "updated_at": now,
            "version": (old or {}).get("version", 0) + 1,
        }
        atomic_write_text(_path(art["id"]), json.dumps(art))
    return art


def get(aid: str) -> dict | None:
    try:
        return json.loads(read_text(_path(aid)))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def list_all(session_id: str = "", project_id: str = "") -> list[dict]:
    out = []
    if ARTIFACTS_DIR.is_dir():
        for p in ARTIFACTS_DIR.glob("*.json"):
            try:
                a = json.loads(read_text(p))
            except (OSError, json.JSONDecodeError):
                continue
            if session_id and a.get("session_id") != session_id:
                continue
            if project_id and a.get("project_id") != project_id:
                continue
            out.append({k: v for k, v in a.items() if k != "content"} | {"size": len(a.get("content", ""))})
    out.sort(key=lambda a: a.get("updated_at", ""), reverse=True)
    return out


def delete(aid: str) -> None:
    p = _path(aid)
    if p.exists():
        p.unlink()


class ArtifactTools:
    """Plugs save_artifact into the agent (see anvil/extensions.py)."""

    listeners: list = []  # callables(artifact) told about every save (the desktop app refreshes its list)

    def tool_defs(self, agent) -> list[dict]:
        return [TOOL_DEF]

    def owns(self, name: str) -> bool:
        return name == "save_artifact"

    def validate(self, name: str, args) -> str | None:
        if not isinstance(args, dict):
            return "input is not a JSON object"
        for k in TOOL_DEF["input_schema"]["required"]:
            if k not in args:
                return f"missing required field {k!r}"
        for k, v in args.items():
            if k not in TOOL_DEF["input_schema"]["properties"]:
                return f"unexpected field {k!r}"
            if not isinstance(v, str):
                return f"field {k!r} must be string"
        if args["kind"] not in KINDS:
            return f"field 'kind' must be one of {', '.join(KINDS)}"
        return None

    def approval(self, agent, name, args):
        return False, []  # only writes into Anvil's own artifacts folder

    def preview(self, agent, name, args):
        return f"Save artifact \"{args.get('title', '')}\""

    def label(self, name: str) -> str:
        return ""

    def run(self, agent, name, args):
        try:
            art = save(args["title"], args["kind"], args["content"], args.get("language", ""),
                       args.get("artifact_id", ""), session_id=getattr(agent, "session_id", ""),
                       root=str(agent.root), project_id=getattr(agent, "project_id", "") or "")
        except ValueError as e:
            from .tools import ToolError
            raise ToolError(str(e))
        for fn in list(self.listeners):
            try:
                fn(art)
            except Exception:
                pass
        return (f"Saved artifact \"{art['title']}\" (id {art['id']}, version {art['version']}). "
                "The user can open it from the Artifacts panel.")
