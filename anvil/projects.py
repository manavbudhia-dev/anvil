"""Projects and threads.

A project is a named workspace: a folder, standing instructions and a memory file that every
session in it shares. Each project has one project chat, where a coordinator agent talks with you
and starts threads. A thread is a separate agent session in the same project that works on one
task in the background, keeps a checklist, and asks you when it needs a decision.

Everything is plain JSON under ~/.config/anvil/projects/, with stable ids and updated_at stamps
so a later sync service can merge it.
"""

import json
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .fsutil import atomic_write_text, read_text

PROJECTS_DIR = CONFIG_DIR / "projects"
SCHEMA = 1
MEMORY_LIMIT = 20_000          # characters of memory put in the system prompt
THREAD_STATUSES = ("working", "needs_you", "done", "failed", "stopped")
CHECK_STATES = ("todo", "doing", "done")
_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(3)}"


@dataclass
class Project:
    id: str
    name: str
    root: str
    instructions: str = ""
    chat_id: str = ""           # the project chat's session id
    created_at: str = ""
    updated_at: str = ""
    schema: int = SCHEMA
    extra: dict = field(default_factory=dict)  # fields from newer versions, kept on save

    # storage --------------------------------------------------------------
    @property
    def path(self) -> Path:
        return PROJECTS_DIR / f"{self.id}.json"

    @property
    def memory_path(self) -> Path:
        return PROJECTS_DIR / f"{self.id}.memory.md"

    def save(self) -> None:
        self.updated_at = now_iso()
        data = asdict(self)
        extra = data.pop("extra") or {}
        with _LOCK:
            atomic_write_text(self.path, json.dumps({**extra, **data}, indent=2), private=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and k != "extra"}
        extra = {k: v for k, v in d.items() if k not in cls.__dataclass_fields__}
        return cls(**known, extra=extra)

    @classmethod
    def load(cls, pid: str) -> "Project | None":
        if not re.fullmatch(r"[\w-]+", pid or ""):
            return None
        p = PROJECTS_DIR / f"{pid}.json"
        try:
            return cls.from_dict(json.loads(read_text(p)))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    @classmethod
    def create(cls, name: str, root: str, instructions: str = "") -> "Project":
        name = name.strip()
        if not name:
            raise ValueError("Give the project a name.")
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Folder not found: {path}")
        ts = now_iso()
        p = cls(id=new_id("prj"), name=name[:80], root=str(path), instructions=instructions.strip(),
                chat_id=new_id("chat"), created_at=ts, updated_at=ts)
        p.save()
        return p

    def delete(self) -> None:
        for f in (self.path, self.memory_path):
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    # memory ---------------------------------------------------------------
    def read_memory(self) -> str:
        try:
            return read_text(self.memory_path)
        except OSError:
            return ""

    def write_memory(self, text: str) -> None:
        with _LOCK:
            atomic_write_text(self.memory_path, text, private=True)

    def remember(self, note: str) -> None:
        note = " ".join(note.split())
        if not note:
            raise ValueError("empty note")
        with _LOCK:
            cur = ""
            try:
                cur = read_text(self.memory_path)
            except OSError:
                pass
            stamp = datetime.now().strftime("%Y-%m-%d")
            atomic_write_text(self.memory_path, cur.rstrip("\n") + ("\n" if cur.strip() else "") + f"- {note} ({stamp})\n",
                              private=True)

    def view(self) -> dict:
        return {"id": self.id, "name": self.name, "root": self.root, "instructions": self.instructions,
                "chat_id": self.chat_id, "created_at": self.created_at, "updated_at": self.updated_at}


def list_projects() -> list[Project]:
    out = []
    if PROJECTS_DIR.exists():
        for f in PROJECTS_DIR.glob("*.json"):
            p = Project.load(f.stem)
            if p:
                out.append(p)
    out.sort(key=lambda p: p.updated_at, reverse=True)
    return out


def find_project(ref: str) -> Project | None:
    """By id, or by name (case-insensitive)."""
    p = Project.load(ref)
    if p:
        return p
    ref = ref.strip().lower()
    return next((p for p in list_projects() if p.name.lower() == ref), None)


# --- agent wiring -------------------------------------------------------------

def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required}}


REMEMBER = _tool("remember", "Save one lasting fact about this project to project memory (shared by every "
                 "session in the project and shown to future ones): a decision, a preference of the user, "
                 "how to build or run something. Don't save secrets or things the code already says.",
                 {"note": {"type": "string", "description": "The fact, one sentence."}}, ["note"])
ASK_USER = _tool("ask_user", "Put a multiple-choice decision in front of the user and wait for their pick. Use it "
                 "only when a choice changes the user's goal or can't be undone. Recommend one option.",
                 {"question": {"type": "string"},
                  "options": {"type": "array", "description": "2-4 options",
                              "items": {"type": "object", "properties": {
                                  "label": {"type": "string", "description": "a few words"},
                                  "detail": {"type": "string", "description": "what happens if picked"}},
                                  "required": ["label"]}},
                  "recommended": {"type": "integer", "description": "index of the option you recommend"}},
                 ["question", "options"])
START_THREAD = _tool("start_thread", "Start a thread: a separate agent that works on one task in this project in the "
                     "background while you keep talking with the user. Give it everything it needs, since it "
                     "doesn't see this chat. Start several for independent pieces of work.",
                     {"title": {"type": "string", "description": "3-6 words, verb first, e.g. 'Fix Safari export button'"},
                      "task": {"type": "string", "description": "Full instructions for the thread"}},
                     ["title", "task"])
LIST_THREADS = _tool("list_threads", "List this project's threads with their status, checklist progress and latest summary.",
                     {}, [])
MESSAGE_THREAD = _tool("message_thread", "Send a follow-up instruction to an existing thread. It runs after the "
                       "thread's current step if it's busy.",
                       {"thread_id": {"type": "string"}, "text": {"type": "string"}}, ["thread_id", "text"])
UPDATE_CHECKLIST = _tool("update_checklist", "Show your progress as a checklist the user can see. Each call replaces "
                         "the whole list. Post it when you start and update it as steps complete.",
                         {"items": {"type": "array", "items": {"type": "object", "properties": {
                             "text": {"type": "string"}, "state": {"type": "string", "enum": list(CHECK_STATES)}},
                             "required": ["text", "state"]}}},
                         ["items"])
SET_STATUS = _tool("set_status", "Mark how your task ended, with a one-paragraph result for the user. Call it once "
                   "at the end: done when the task is complete, failed when you couldn't do it.",
                   {"status": {"type": "string", "enum": ["done", "failed"]},
                    "summary": {"type": "string"}}, ["status", "summary"])

CHAT_PROMPT = """
# Project: {name}
You are the coordinator of this project's chat. The user drops requests here and comes back later.
- Answer quick questions yourself. For real work (code changes, investigations, anything that takes more than a few tool calls), start a thread with start_thread and tell the user in one line. Independent pieces of work get separate threads that run in parallel.
- Thread results come back to you as notes at the start of the user's next message; use list_threads to check on them.
- Save lasting facts (decisions, preferences, how to run things) with remember.
"""

THREAD_PROMPT = """
# Project: {name}
You are working on one thread of this project: "{title}". You run in the background; the user reads your checklist and final reply when they come back.
- Call update_checklist when you start and as each step completes.
- If you need a decision only the user can make, call ask_user. Otherwise keep going on the most likely reading and say which one you chose.
- When finished, call set_status with done or failed and a short result, then end with the same result as your reply.
- Save lasting facts about the project with remember.
"""


def system_suffix(project: Project, kind: str, title: str = "") -> str:
    parts = [(CHAT_PROMPT if kind == "chat" else THREAD_PROMPT).format(name=project.name, title=title or "task")]
    if project.instructions.strip():
        parts.append(f"\n# Project instructions (from the user)\n{project.instructions.strip()}\n")
    mem = project.read_memory().strip()
    if mem:
        parts.append(f"\n# Project memory\n{mem[-MEMORY_LIMIT:]}\n")
    return "".join(parts)


def tool_defs(kind: str, can_spawn: bool = True) -> list[dict]:
    if kind == "chat":
        tools = [REMEMBER, ASK_USER, LIST_THREADS]
        if can_spawn:
            tools += [START_THREAD, MESSAGE_THREAD]
        return tools
    return [REMEMBER, ASK_USER, UPDATE_CHECKLIST, SET_STATUS]


def clean_checklist(items) -> list[dict]:
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and str(it.get("text", "")).strip():
            state = it.get("state") if it.get("state") in CHECK_STATES else "todo"
            out.append({"text": str(it["text"]).strip()[:200], "state": state})
    return out[:30]


def clean_options(options) -> list[dict]:
    out = []
    for o in options if isinstance(options, list) else []:
        if isinstance(o, str) and o.strip():
            out.append({"label": o.strip()[:60], "detail": ""})
        elif isinstance(o, dict) and str(o.get("label", "")).strip():
            out.append({"label": str(o["label"]).strip()[:60], "detail": str(o.get("detail", "") or "")[:300]})
    return out[:4]


class ProjectHooks:
    """Runs the project tools for one session. `host` supplies what depends on the front end:

    host.ask(question, options, recommended) -> str        the user's pick (or typed answer)
    host.start_thread(title, task) -> str                  only for the project chat
    host.list_threads() -> str
    host.message_thread(thread_id, text) -> str
    host.changed()                                         checklist/status changed, refresh views
    """

    def __init__(self, project: Project, kind: str, state, host):
        self.project = project
        self.kind = kind
        self.state = state      # object with .checklist, .status, .summary
        self.host = host

    def defs(self) -> list[dict]:
        return tool_defs(self.kind, can_spawn=hasattr(self.host, "start_thread"))

    def names(self) -> set[str]:
        return {t["name"] for t in self.defs()}

    def run(self, name: str, args: dict) -> str:
        if name == "remember":
            self.project.remember(str(args.get("note", "")))
            return "Saved to project memory."
        if name == "ask_user":
            opts = clean_options(args.get("options"))
            if len(opts) < 2:
                raise ValueError("give at least two options")
            rec = args.get("recommended")
            rec = rec if isinstance(rec, int) and 0 <= rec < len(opts) else 0
            answer = self.host.ask(str(args.get("question", "")).strip(), opts, rec)
            return f"The user chose: {answer}" if answer else "The user didn't answer."
        if name == "update_checklist":
            self.state.checklist = clean_checklist(args.get("items"))
            self.host.changed()
            done = sum(1 for i in self.state.checklist if i["state"] == "done")
            return f"Checklist updated ({done}/{len(self.state.checklist)} done)."
        if name == "set_status":
            st = args.get("status")
            if st not in ("done", "failed"):
                raise ValueError("status must be done or failed")
            self.state.final_status = st
            self.state.summary = str(args.get("summary", "")).strip()[:2000]
            self.host.changed()
            return f"Status set to {st}."
        if name == "start_thread":
            title, task = str(args.get("title", "")).strip(), str(args.get("task", "")).strip()
            if not title or not task:
                raise ValueError("title and task are required")
            return self.host.start_thread(title[:80], task)
        if name == "list_threads":
            return self.host.list_threads()
        if name == "message_thread":
            return self.host.message_thread(str(args.get("thread_id", "")), str(args.get("text", "")).strip())
        raise ValueError(f"unknown project tool {name}")


def describe_threads(threads: list[dict]) -> str:
    if not threads:
        return "No threads yet."
    lines = []
    for t in threads:
        cl = t.get("checklist") or []
        prog = f" [{sum(1 for i in cl if i['state'] == 'done')}/{len(cl)}]" if cl else ""
        line = f"- {t['id']} · {t.get('status') or 'idle'}{prog} · {t.get('title', '')}"
        if t.get("summary"):
            line += f"\n  Result: {t['summary'][:400]}"
        lines.append(line)
    return "\n".join(lines)
