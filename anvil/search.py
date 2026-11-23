"""Search across everything Anvil keeps: sessions, artifacts, scheduled tasks and projects.

Plain case-insensitive matching of every word in the query; good enough for a personal
history of a few thousand sessions, with no index to keep in sync.
"""

import json
from pathlib import Path

from .config import CONFIG_DIR, SESSIONS_DIR
from .fsutil import read_text

PROJECTS_DIR = CONFIG_DIR / "projects"
MAX_FILE_BYTES = 8_000_000


def _terms(q: str) -> list[str]:
    return [t for t in q.lower().split() if t]


def _snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, pos - width // 3)
    snip = text[start:start + width].replace("\n", " ").strip()
    return ("…" if start else "") + snip + ("…" if start + width < len(text) else "")


def _matches(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return all(t in low for t in terms)


def _session_text(messages: list) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
            continue
        for b in c or []:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
    return "\n".join(parts)


def _load(p: Path) -> dict | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        d = json.loads(read_text(p))
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def search(q: str, limit: int = 50, kinds: set[str] | None = None) -> list[dict]:
    terms = _terms(q)
    if not terms:
        return []
    want = kinds or {"session", "artifact", "schedule", "project"}
    out: list[dict] = []

    if "session" in want and SESSIONS_DIR.is_dir():
        for p in SESSIONS_DIR.glob("*.json"):
            d = _load(p)
            if not d:
                continue
            title = d.get("title") or ""
            body = _session_text(d.get("messages", []))
            hay = f"{title}\n{d.get('root', '')}\n{body}"
            if _matches(hay, terms):
                out.append({"type": "session", "id": p.stem, "title": title or "Session",
                            "snippet": _snippet(body or title, terms), "root": d.get("root", ""),
                            "project_id": d.get("project_id", ""), "updated": d.get("saved_at", ""),
                            "title_hit": _matches(title, terms)})

    if "artifact" in want:
        from .artifacts import ARTIFACTS_DIR
        for p in ARTIFACTS_DIR.glob("*.json") if ARTIFACTS_DIR.is_dir() else []:
            a = _load(p)
            if a and _matches(f"{a.get('title', '')}\n{a.get('content', '')}", terms):
                out.append({"type": "artifact", "id": a["id"], "title": a.get("title", ""), "kind": a.get("kind", ""),
                            "snippet": _snippet(a.get("content", ""), terms), "root": a.get("root", ""),
                            "session_id": a.get("session_id", ""), "updated": a.get("updated_at", ""),
                            "title_hit": _matches(a.get("title", ""), terms)})

    if "schedule" in want:
        from .schedules import load
        for s in load():
            if _matches(f"{s.get('name', '')}\n{s.get('prompt', '')}", terms):
                out.append({"type": "schedule", "id": s["id"], "title": s.get("name", ""),
                            "snippet": _snippet(s.get("prompt", ""), terms), "root": s.get("root", ""),
                            "updated": s.get("updated_at", ""), "title_hit": _matches(s.get("name", ""), terms)})

    if "project" in want and PROJECTS_DIR.is_dir():  # written by the Projects feature, when present
        for p in PROJECTS_DIR.glob("*.json"):
            d = _load(p)
            if not d:
                continue
            name = d.get("name") or d.get("title") or p.stem
            hay = f"{name}\n{d.get('instructions', '')}\n{d.get('root', '') or d.get('folder', '')}"
            if _matches(hay, terms):
                out.append({"type": "project", "id": d.get("id") or p.stem, "title": name,
                            "snippet": _snippet(d.get("instructions", "") or name, terms),
                            "root": d.get("root", "") or d.get("folder", ""), "updated": d.get("updated_at", ""),
                            "title_hit": _matches(name, terms)})

    out.sort(key=lambda r: (r.pop("title_hit"), r.get("updated", "")), reverse=True)
    return out[:limit]
