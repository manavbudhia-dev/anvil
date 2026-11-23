"""Scheduled tasks: run a prompt in a project folder on a cron schedule.

Runs happen while the desktop app is open, or on a server with `anvil schedule daemon`
(a systemd service or similar). Only one scheduler runs at a time (a heartbeat lock file), so
the desktop app and a daemon on the same machine don't both fire the same task.

A run that was missed while nothing was running fires once when the scheduler next starts.
"""

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .fsutil import atomic_write_text, read_text

SCHEDULES_FILE = CONFIG_DIR / "schedules.json"
LOCK_FILE = CONFIG_DIR / "scheduler.lock"
LOCK_STALE = 90
CHECK_EVERY = 20
MODES = ("ask", "edits", "auto")
_LOCK = threading.RLock()

PRESETS = {  # friendly names the UI offers
    "hourly": "0 * * * *", "daily": "0 9 * * *", "weekdays": "0 9 * * 1-5", "weekly": "0 9 * * 1",
}


# --- cron -------------------------------------------------------------------------

_FIELDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_NAMES = {3: ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
          4: ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}


def _field(spec: str, idx: int) -> set[int]:
    lo, hi = _FIELDS[idx]
    if idx == 4:
        hi = 7  # 0 and 7 both mean Sunday
    names = _NAMES.get(idx, [])
    out: set[int] = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError("step must be at least 1")
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names, idx), _num(y, names, idx)
        else:
            a = _num(part, names, idx)
            b = hi if step > 1 else a
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise ValueError(f"'{spec}' is out of range {lo}-{hi}")
        out.update(v % 7 if idx == 4 else v for v in range(a, b + 1, step))
    return out


def _num(x: str, names: list[str], idx: int) -> int:
    x = x.strip()
    if x[:3] in names:
        return names.index(x[:3]) + (1 if idx == 3 else 0)
    return int(x)


class Cron:
    def __init__(self, expr: str):
        expr = _ALIASES.get(expr.strip().lower(), PRESETS.get(expr.strip().lower(), expr.strip()))
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("a schedule needs 5 fields: minute hour day-of-month month day-of-week")
        try:
            self.minute, self.hour, self.dom, self.month, self.dow = (_field(p, i) for i, p in enumerate(parts))
        except ValueError as e:
            raise ValueError(f"bad schedule '{expr}': {e}") from None
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"
        self.expr = expr

    def _day_ok(self, d: datetime) -> bool:
        dom_ok = d.day in self.dom
        dow_ok = (d.weekday() + 1) % 7 in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # classic cron: either matches when both are restricted

    def next_after(self, after: datetime) -> datetime:
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=366 * 5)
        while t < limit:
            if t.month not in self.month:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hour:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minute:
                t += timedelta(minutes=1)
                continue
            return t
        raise ValueError(f"schedule '{self.expr}' never runs")


def describe(expr: str) -> str:
    e = _ALIASES.get(expr.strip().lower(), PRESETS.get(expr.strip().lower(), expr.strip()))
    m = re.fullmatch(r"(\d+) (\d+) \* \* (\*|1-5|\d)", e)
    if m:
        mi, h, dow = int(m.group(1)), int(m.group(2)), m.group(3)
        at = f"{h:02d}:{mi:02d}"
        if dow == "*":
            return f"Every day at {at}"
        if dow == "1-5":
            return f"Weekdays at {at}"
        return f"Every {['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][int(dow) % 7]} at {at}"
    m = re.fullmatch(r"(\d+) \* \* \* \*", e)
    if m:
        return f"Every hour at :{int(m.group(1)):02d}"
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", e)
    if m:
        return f"Every {m.group(1)} minutes"
    return e


# --- storage ------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else ""


def _parse(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load() -> list[dict]:
    with _LOCK:
        try:
            data = json.loads(read_text(SCHEDULES_FILE))
            return [s for s in data.get("schedules", []) if isinstance(s, dict)] if isinstance(data, dict) else []
        except (OSError, json.JSONDecodeError):
            return []


def _save(items: list[dict]) -> None:
    with _LOCK:
        atomic_write_text(SCHEDULES_FILE, json.dumps({"schedules": items}, indent=2))


def next_run(expr: str, after: datetime | None = None) -> datetime:
    """Next run in local time, returned as an aware UTC datetime."""
    local_after = (after or _now_utc()).astimezone().replace(tzinfo=None)
    nxt = Cron(expr).next_after(local_after)
    return nxt.astimezone().astimezone(timezone.utc)


def get(sid: str) -> dict | None:
    return next((s for s in load() if s["id"] == sid), None)


def save(data: dict) -> dict:
    with _LOCK:
        items = load()
        sid = data.get("id") or ""
        old = next((s for s in items if s["id"] == sid), None) if sid else None
        s = dict(old or {"id": f"sch-{secrets.token_hex(4)}", "created_at": _iso(_now_utc()), "enabled": True,
                         "last_run": "", "last_status": "", "last_session": ""})
        for k in ("name", "prompt", "root", "cron", "mode", "model", "project_id"):
            if k in data:
                s[k] = str(data[k] or "").strip()
        if "enabled" in data:
            s["enabled"] = bool(data["enabled"])
        if not s.get("prompt"):
            raise ValueError("Prompt is required")
        root = Path(os.path.expanduser(s.get("root") or ""))
        if not s.get("root") or not root.is_dir():
            raise ValueError(f"Folder not found: {s.get('root') or '(empty)'}")
        s["root"] = str(root.resolve())
        s["cron"] = Cron(s.get("cron") or "daily").expr
        s["mode"] = s.get("mode") if s.get("mode") in MODES else "edits"
        s["name"] = (s.get("name") or s["prompt"].splitlines()[0])[:80]
        s["updated_at"] = _iso(_now_utc())
        s["next_run"] = _iso(next_run(s["cron"])) if s["enabled"] else ""
        items = [x for x in items if x["id"] != s["id"]] + [s]
        _save(items)
        return s


def delete(sid: str) -> None:
    with _LOCK:
        _save([s for s in load() if s["id"] != sid])


def mark(sid: str, **fields) -> None:
    with _LOCK:
        items = load()
        for s in items:
            if s["id"] == sid:
                s.update(fields)
        _save(items)


def view(s: dict) -> dict:
    return {**s, "description": describe(s.get("cron", ""))}


# --- the scheduler loop -----------------------------------------------------------------

def _take_lock(owner: str) -> bool:
    """Heartbeat lock: the file holds owner + time; a stale one (crashed process) can be taken over."""
    try:
        cur = json.loads(read_text(LOCK_FILE))
    except (OSError, json.JSONDecodeError):
        cur = {}
    if cur.get("owner") not in (None, owner) and time.time() - float(cur.get("at", 0)) < LOCK_STALE:
        return False
    atomic_write_text(LOCK_FILE, json.dumps({"owner": owner, "pid": os.getpid(), "at": time.time()}))
    return True


def _release_lock(owner: str) -> None:
    try:
        if json.loads(read_text(LOCK_FILE)).get("owner") == owner:
            LOCK_FILE.unlink()
    except (OSError, json.JSONDecodeError):
        pass


class Scheduler:
    """Calls runner(schedule) -> session id for every due task. runner must not block for long
    (the desktop app starts the run on its own thread); the daemon runs one at a time."""

    def __init__(self, runner, check_every: float = CHECK_EVERY):
        self.runner = runner
        self.check_every = check_every
        self.owner = f"{os.getpid()}-{secrets.token_hex(3)}"
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, name="anvil-scheduler", daemon=True)
        self.thread.start()

    def loop(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    if _take_lock(self.owner):
                        self.tick()
                except Exception:
                    pass
                self.stop.wait(self.check_every)
        finally:
            _release_lock(self.owner)

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or _now_utc()
        fired = []
        for s in load():
            if not s.get("enabled", True):
                continue
            due = _parse(s.get("next_run", ""))
            if due is None:
                try:
                    mark(s["id"], next_run=_iso(next_run(s["cron"], now)))
                except ValueError:
                    pass
                continue
            if due > now:
                continue
            # Move next_run forward first, so a crash during the run can't make it fire in a loop.
            try:
                mark(s["id"], next_run=_iso(next_run(s["cron"], now)), last_run=_iso(now), last_status="running")
            except ValueError:
                mark(s["id"], enabled=False, last_status="bad schedule")
                continue
            fired.append(s["id"])
            try:
                session = self.runner(s)
                mark(s["id"], last_session=session or "", last_status="started")
            except Exception as e:
                mark(s["id"], last_status=f"failed: {e}"[:300])
        return fired
