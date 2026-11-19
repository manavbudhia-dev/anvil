import os
import tempfile
import time
from pathlib import Path


def _replace(src: str, dst: Path) -> None:
    # On Windows, replacing a file fails while another thread or process has it open for reading.
    # Such reads are brief, so retry for up to ~2 seconds.
    for attempt in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 39:
                raise
            time.sleep(0.05)


def read_text(path: Path) -> str:
    """Read a file that may be replaced concurrently (retries Windows sharing errors)."""
    for attempt in range(40):
        try:
            return Path(path).read_text(encoding="utf-8")
        except PermissionError:
            if os.name != "nt" or attempt == 39:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def atomic_write_text(path: Path, text: str, private: bool = False) -> None:
    """Write a file so readers see either the old or the new content, never a half-written file.

    The data goes to a temporary file in the same folder, which then replaces the target in one
    step. With private=True the file is readable only by its owner from the moment it exists
    (no window where an API key is world-readable).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if private and os.name != "nt":
            os.fchmod(fd, 0o600)
        elif not private and os.name != "nt":
            os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
