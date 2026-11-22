"""Guards for running an agent on a real machine.

- dangerous_command(): reasons a shell command needs a human's explicit yes, every time.
  The command is parsed like a shell would (anvil/shellparse.py), not matched with raw regexes,
  so quoting, `$'\\x72m'`, `{rm,-rf,/}`, variables set earlier in the line, `sudo`/`env`/`xargs`
  wrappers, `bash -c`, `eval`, `cmd /c`, `powershell -Command`/`-EncodedCommand` and interpreter
  one-liners (`python -c`, `node -e`, `perl -e`, ...) are all looked through.
- protected_path(): files whose edits always need a human's yes (Linux, macOS and Windows).
- load_policy(): a root-owned policy file that caps the permission mode (and can require the sandbox).
- backup_file(): copies a file aside before the agent changes it.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR
from .shellparse import FETCH, SENTINELS, SUB, VAR, ParseError, expand, parse, parse_cmd

POLICY_FILE = Path(os.environ.get("ANVIL_POLICY_FILE", "/etc/anvil/policy.json"))
BACKUP_DIR = CONFIG_DIR / "backups"
MODES = ("ask", "edits", "auto")  # least to most permissive
MAX_DEPTH = 6
MAX_COMMAND_CHARS = 100_000

# --- paths -----------------------------------------------------------------------

_PROTECTED = [
    (r"^/$|^[a-z]:/?$", "the filesystem root"),
    (r"^/etc/caddy(/|$)", "the live Caddy config (edit a copy, then: sudo anvil-caddy-apply apply <file>)"),
    (r"^/etc/anvil(/|$)", "Anvil's own safety policy"),
    (r"^/etc/(sudoers|ssh/|passwd|shadow|gshadow|group|fstab|hosts|crontab|pam\.d|security/|ld\.so|profile|"
     r"bash\.bashrc|environment|resolv\.conf)", "a core system file"),
    (r"^/etc/(nginx|systemd|cron|init\.d|logrotate|apache2|httpd)", "a server config file"),
    (r"^/etc(/|$)", "a system config file"),
    (r"^/(boot|bin|sbin|lib|lib32|lib64|libx32|usr/(?!local(/|$))[^/]+|var/lib|proc/sys|sys)(/|$)",
     "an operating system file"),
    (r"^/dev/(?!(null|stdout|stderr|stdin|tty|zero|random|urandom)$|fd/|shm/|pts/)", "a device or raw disk"),
    (r"^/var/spool/cron|^/var/at/tabs", "the crontab"),
    (r"^/system(/|$)|^/library/(launchdaemons|launchagents|startupitems|keychains)(/|$)", "a macOS system or startup file"),
    (r"/library/(launchagents|keychains)(/|$)", "macOS login items or keychains"),
    (r"/\.ssh(/|$)", "SSH keys or config"),
    (r"/\.config/anvil(/|$)", "Anvil's own settings"),
    (r"/\.anvil(/|$)", "Anvil's project safety settings (.anvil/)"),
    (r"/\.(bashrc|bash_profile|bash_login|profile|zshrc|zprofile|zshenv|zlogin|config/fish/config\.fish)$",
     "a shell startup file (it runs at every login)"),
    (r"/\.(aws|gnupg|kube|docker|azure|config/gcloud)(/|$)|/\.(netrc|git-credentials|npmrc|pypirc)$", "stored credentials"),
    (r"/\.git/hooks(/|$)", "git hooks (they run code on every commit)"),
    (r"^[a-z]:/windows(/|$)", "a Windows system file"),
    (r"^[a-z]:/program files( \(x86\))?(/|$)|^[a-z]:/programdata/microsoft(/|$)", "installed programs"),
    (r"/start menu/programs/startup(/|$)", "a Windows startup folder"),
    (r"/ntuser\.dat", "the Windows user registry"),
]
_PROTECTED_RX = [(re.compile(p), what) for p, what in _PROTECTED]

# Reading these is risky too (keys, tokens, Anvil's own settings).
_SECRETS = [
    (r"/\.ssh/(?!known_hosts$)[^/]+$", "SSH keys or config"),
    (r"/\.config/anvil(/|$)|^/etc/anvil(/|$)", "Anvil's own settings or safety policy"),
    (r"^/etc/(shadow|gshadow|sudoers)", "system passwords or sudo rules"),
    (r"/\.(aws|gnupg|kube|azure|config/gcloud)(/|$)|/\.(netrc|git-credentials|pypirc)$|/\.docker/config\.json$",
     "stored credentials"),
]
_SECRETS_RX = [(re.compile(p), what) for p, what in _SECRETS]

_WIN_VARS = {
    "systemroot": "c:/windows", "windir": "c:/windows", "programfiles": "c:/program files",
    "programfiles(x86)": "c:/program files (x86)", "programdata": "c:/programdata",
    "systemdrive": "c:",
}
_UNKNOWN_CWD = "/\x01cwd"


def _home() -> str:
    return str(Path.home()).replace("\\", "/")


def _norm(path: str, cwd: str | None = None) -> str:
    """A lowercase, forward-slash, absolute form of path for matching against the rules."""
    p = str(path).strip().replace("\\", "/")
    home = _home()
    p = re.sub(r"^~(?=/|$)", home, p)
    p = re.sub(r"^(\$\{?home\}?|\$env:(userprofile|home)|%(userprofile|home)%)(?=/|$)", home, p, flags=re.I)

    def winvar(m):
        name = m.group(1) or m.group(2)
        name = name.lower()
        if name in ("appdata",):
            return home + "/appdata/roaming"
        if name in ("localappdata",):
            return home + "/appdata/local"
        return _WIN_VARS.get(name, m.group(0))
    p = re.sub(r"%([a-z()0-9_]+)%|\$env:([a-z()0-9_]+)", winvar, p, flags=re.I)
    windows = bool(re.match(r"^[a-zA-Z]:", p))
    if not windows and not p.startswith("/"):
        base = (cwd or _UNKNOWN_CWD).replace("\\", "/")
        if re.match(r"^[a-zA-Z]:", base):
            windows = True
        p = base.rstrip("/") + "/" + p
    if windows:
        drive, rest = p[:2], p[2:] or "/"
        p = drive + posixpath.normpath("/" + rest.lstrip("/"))
    else:
        p = posixpath.normpath(p)
        if p.startswith("//"):
            p = p[1:]
    p = p.lower()
    if p.startswith("/private/") and re.match(r"^/private/(etc|var|tmp)(/|$)", p):
        p = p[len("/private"):]  # macOS: /etc is /private/etc
    return p


def _match(rules, path: str, cwd: str | None = None) -> str | None:
    p = _norm(path, cwd)
    if _UNKNOWN_CWD in p:  # relative path, cwd unknown: only the rules that don't need a full path
        p = p.replace(_UNKNOWN_CWD, "")
        rules = [(rx, what) for rx, what in rules if not rx.pattern.startswith("^")]
    config_dir = str(CONFIG_DIR).replace("\\", "/").lower().rstrip("/")
    if rules is _PROTECTED_RX or rules is _SECRETS_RX:
        if p == config_dir or p.startswith(config_dir + "/"):
            return "Anvil's own settings"
    for rx, what in rules:
        if rx.search(p):
            return what
    return None


def protected_path(path) -> str | None:
    # Match on forward slashes on every OS: on Windows, str(path) uses backslashes.
    what = _match(_PROTECTED_RX, str(path)) or _match(_SECRETS_RX, str(path))
    return f"edits {what}" if what else None


# --- program names ----------------------------------------------------------------

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh", "busybox-sh"}
INTERPRETERS = {"python", "pypy", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "osascript",
                "awk", "gawk", "mawk", "nawk", "lua", "rscript", "julia", "tclsh"}
POWERSHELLS = {"powershell", "pwsh"}
DOWNLOADERS = {"curl", "wget", "fetch", "aria2c", "http", "https", "lwp-request", "invoke-webrequest", "iwr",
               "invoke-restmethod", "irm", "certutil", "bitsadmin", "scp", "rsync", "nc", "ncat", "socat"}
DECODERS = {"base64", "base32", "xxd", "uudecode", "openssl", "gpg", "gunzip", "zcat", "bunzip2", "xz", "rev"}
_HARMLESS_HOSTS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "man", "which", "type", "whereis",
                   "apropos", "help", "info", "git", "apt", "apt-get", "brew", "pip", "pip3", "npm", "yarn", "pnpm",
                   "dnf", "yum", "cat", "less", "head", "tail", "wc", "ls", "file", "stat", "sort", "uniq",
                   "cut", "tr", "true", "false", "test", "[", "[[", "hash", "alias", "unalias", "complete",
                   "command", "export", "readonly", "local", "declare", "set", "unset", "cd", "pushd", "popd",
                   "history", "jq", "awk", "sed"}
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time", "coproc"}
_SKIP_COMMANDS = {"fi", "done", "esac", "}", "for", "case", "in", "select", "function", "[[", "]]", "return",
                  "break", "continue", "exit", ";;"}


def progname(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower().strip("{}()&")
    name = re.sub(r"\.(exe|cmd|bat|com|ps1)$", "", name)
    m = re.match(r"^(python|pypy|perl|ruby|php|node|lua|pip|tclsh)[\d.]*$", name)
    return m.group(1) if m else name


def _short_flags(args) -> set[str]:
    out = set()
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            out |= set(a[1:])
    return out


def _long_flags(args) -> set[str]:
    return {a.split("=", 1)[0].lower() for a in args if a.startswith("--")}


def _operands(args, with_arg=()) -> list[str]:
    out, skip, ended = [], False, False
    for a in args:
        if skip:
            skip = False
            continue
        if ended or not a.startswith("-") or a == "-":
            out.append(a)
        elif a == "--":
            ended = True
        elif a in with_arg:
            skip = True
    return out


def _skip_opts(args, with_arg=(), stop_at_operand=True) -> list[str]:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if not a.startswith("-") or a == "-":
            break
        i += 2 if a in with_arg else 1
    return args[i:]


def _echo_output(argv) -> str | None:
    """What echo/printf with literal arguments prints, or None if it isn't that."""
    if not argv or any(s in a for a in argv for s in SENTINELS):
        return None
    name, args = progname(argv[0]), argv[1:]
    if name == "echo":
        interpret = False
        while args and re.fullmatch(r"-[neE]+", args[0]):
            interpret |= "e" in args[0]
            args = args[1:]
        text = " ".join(args)
        return _unescape(text) if interpret else text
    if name == "printf" and args:
        fmt = _unescape(args[0])
        rest = args[1:]
        if "%" in fmt:
            parts = iter(rest)
            fmt = re.sub(r"%[-+ #0-9.]*[sdbq]", lambda m: next(parts, ""), fmt)
            leftover = list(parts)
            return fmt + (" " + " ".join(leftover) if leftover else "")
        return fmt
    return None


def _unescape(text: str) -> str:
    try:
        return text.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


# --- interpreter one-liners ---------------------------------------------------------

_CODE_RULES = {
    "python": [
        (r"\bshutil\.rmtree|\brmtree\s*\(|\bos\.(remove|unlink|rmdir|removedirs)\b|\.(unlink|rmdir)\s*\(|send2trash", "deletes files"),
        (r"\bos\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*|fork)\b|\bsubprocess\b|\bpty\.spawn|commands\.getoutput", "runs shell commands"),
        (r"\b(exec|eval|compile)\s*\(|__import__\s*\(|\bbase64\b|codecs\.decode|marshal\.loads|pickle\.loads|zlib\.decompress", "runs hidden or dynamically built code"),
        (r"\bos\.kill\b|\bos\.killpg\b", "kills processes"),
    ],
    "node": [
        (r"\b(rm|rmdir|unlink)(Sync)?\s*\(|\bfs-extra\b|\bremove(Sync)?\s*\(|\brimraf\b", "deletes files"),
        (r"child_process|\bexec(File)?(Sync)?\s*\(|\bspawn(Sync)?\s*\(|process\.binding|Deno\.(run|Command)|Bun\.spawn", "runs shell commands"),
        (r"\beval\s*\(|new\s+Function|\bFunction\s*\(|base64|\batob\s*\(|\bvm\.run", "runs hidden or dynamically built code"),
        (r"process\.kill", "kills processes"),
    ],
    "perl": [
        (r"\bunlink\b|\brmtree\b|\bremove_tree\b|\brmdir\b", "deletes files"),
        (r"\bsystem\b|\bexec\b|`|\bqx\b|\bopen\s*\(?[^;]*\|", "runs shell commands"),
        (r"\beval\b|decode_base64|MIME::Base64|\bpack\s*\(", "runs hidden or dynamically built code"),
        (r"\bkill\b", "kills processes"),
    ],
    "ruby": [
        (r"FileUtils\.(rm\w*|remove\w*)|File\.(delete|unlink)|Dir\.(rmdir|delete|unlink)|\.rmtree", "deletes files"),
        (r"\bsystem\s*[\( '\"]|\bexec\s*[\( '\"]|`|%x|\bspawn\b|Open3|IO\.popen|\bfork\b", "runs shell commands"),
        (r"\beval\b|instance_eval|class_eval|Base64|\.unpack1?\b", "runs hidden or dynamically built code"),
        (r"Process\.kill", "kills processes"),
    ],
    "php": [
        (r"\b(unlink|rmdir)\s*\(", "deletes files"),
        (r"\b(system|exec|shell_exec|passthru|popen|proc_open|pcntl_exec)\s*\(|`", "runs shell commands"),
        (r"\beval\s*\(|base64_decode|gzinflate|str_rot13|\bassert\s*\(|create_function", "runs hidden or dynamically built code"),
    ],
    "awk": [(r"\bsystem\s*\(|\|\s*getline|print[^;]*\|\s*\"", "runs shell commands")],
    "osascript": [(r"do\s+shell\s+script", "runs shell commands"),
                  (r"administrator\s+privileges", "asks for administrator rights")],
    "lua": [(r"os\.remove", "deletes files"), (r"os\.execute|io\.popen", "runs shell commands"),
            (r"\bload(string)?\s*\(", "runs hidden or dynamically built code")],
    "rscript": [(r"\b(unlink|file\.remove)\s*\(", "deletes files"), (r"\bsystem2?\s*\(|\bshell\s*\(", "runs shell commands"),
                (r"\beval\s*\(\s*parse", "runs hidden or dynamically built code")],
    "julia": [(r"\brm\s*\(", "deletes files"), (r"\brun\s*\(|`", "runs shell commands"),
              (r"\beval\s*\(|Meta\.parse", "runs hidden or dynamically built code")],
    "tclsh": [(r"file\s+delete", "deletes files"), (r"\bexec\b", "runs shell commands"), (r"\beval\b", "runs hidden or dynamically built code")],
    "sql": [(r"\b(drop\s+(table|database|schema)|truncate\s+(table\s+)?\w)|\bdelete\s+from\s+[\w.\"`]+\s*(;|$)", "deletes database data")],
}
_CODE_RX = {lang: [(re.compile(p, re.I if lang == "sql" else 0), what) for p, what in rules]
            for lang, rules in _CODE_RULES.items()}
_WRITE_HINT = re.compile(r"open\s*\([^)]*['\"][wax+]|write_text|write_bytes|writeFile|appendFile|createWriteStream|"
                         r"file_put_contents|File\.write|copyfile|shutil\.(copy|move)|os\.(rename|replace|chmod|chown)|"
                         r"\b(chmod|rename|copyFile)\w*\s*\(|>>?\s*['\"]?/", re.I)
_STRING_LIT = re.compile(r"\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'")
_LANG = {"python": "python", "pypy": "python", "node": "node", "nodejs": "node", "deno": "node", "bun": "node",
         "perl": "perl", "ruby": "ruby", "php": "php", "osascript": "osascript", "awk": "awk", "gawk": "awk",
         "mawk": "awk", "nawk": "awk", "lua": "lua", "rscript": "rscript", "julia": "julia", "tclsh": "tclsh"}
_SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "clickhouse-client", "cqlsh", "mongosh", "duckdb"}

# --- PowerShell -----------------------------------------------------------------

_PS_FETCH = re.compile(r"downloadstring|downloadfile|downloaddata|invoke-webrequest|\biwr\b|invoke-restmethod|\birm\b|"
                       r"start-bitstransfer|net\.webclient", re.I)
_PS_RUN = re.compile(r"invoke-expression|\biex\b|\[scriptblock\]::create|\|\s*&|&\s*\(", re.I)


class _Analyzer:
    def __init__(self, cwd: str | None):
        self.reasons: list[str] = []
        self.cwd = str(cwd).replace("\\", "/") if cwd else None
        self.env: dict[str, str] = {"IFS": " "}

    def add(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    # --- POSIX shell -----------------------------------------------------------

    def shell(self, src: str, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            self.add("nests commands too deeply to check")
            return
        try:
            pipelines = parse(src)
        except ParseError as e:
            self.add(f"could not be parsed safely ({e})")
            return
        for pipeline in pipelines:
            argvs = []
            for cmd in pipeline:
                for inner in cmd.nested_sources():
                    self.shell(inner, depth + 1)
                argv = self._expand_all(cmd.words)
                prefix_env = {}
                while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
                    k, v = argv.pop(0).split("=", 1)
                    prefix_env[k] = v
                if not argv:
                    self.env.update(prefix_env)
                for op, target in cmd.redirects:
                    for path in self._expand_all([target]):
                        self._redirect(op, path)
                stdin = None
                if cmd.heredoc is not None:
                    stdin = cmd.heredoc.body
                elif cmd.herestring is not None:
                    stdin = " ".join(self._expand_all([cmd.herestring]))
                argv = self.command(argv, depth, stdin=stdin)
                argvs.append(argv)
            self._pipeline(argvs, depth)

    def _expand_all(self, words) -> list[str]:
        out = []
        for i, w in enumerate(words):
            for text, glob in expand(w, self.env, self._sub_value):
                if i == 0 and glob and not out:
                    text = VAR + text  # a wildcard as the command name: treat as unknown
                out.append(text)
        return out

    def _sub_value(self, src: str) -> str:
        """Predicted output of $(src): echo/printf literals are known, downloads are flagged."""
        try:
            pipelines = parse(src)
        except ParseError:
            return SUB
        names = []
        for pipe in pipelines:
            for cmd in pipe:
                argv = self._unwrap(self._expand_all(cmd.words), 0, nested=False)
                if argv:
                    names.append(progname(argv[0]))
        if any(n in DOWNLOADERS or n in DECODERS for n in names):
            return FETCH
        if len(pipelines) == 1 and len(pipelines[0]) == 1:
            out = _echo_output(self._expand_all(pipelines[0][0].words))
            if out is not None:
                return out
        return SUB

    def _redirect(self, op: str, path: str) -> None:
        if any(s in path for s in SENTINELS):
            return
        if re.match(r"^/dev/(tcp|udp)/", path):
            self.add("opens a raw network connection from the shell (/dev/tcp)")
            return
        if op in ("<", "<&"):
            self._read(path)
        else:
            self._write(path)

    def _write(self, path: str) -> None:
        if not path or any(s in path for s in SENTINELS) or path.lower() in ("nul", "con", "/dev/null"):
            return
        what = _match(_PROTECTED_RX, path, self.cwd) or _match(_SECRETS_RX, path, self.cwd)
        if what:
            self.add(f"writes to {what} ({path})")

    def _read(self, path: str) -> None:
        if not path or any(s in path for s in SENTINELS):
            return
        if not ("/" in path or "\\" in path or path.startswith("~") or "%" in path):
            return
        what = _match(_SECRETS_RX, path, self.cwd)
        if what:
            self.add(f"reads or changes {what} ({path})")

    def _pipeline(self, argvs, depth: int) -> None:
        names = [progname(a[0]) if a else "" for a in argvs]
        for k in range(1, len(argvs)):
            argv, name = argvs[k], names[k]
            if not argv:
                continue
            upstream = names[:k]
            reads_stdin = self._reads_stdin(name, argv)
            if not reads_stdin:
                continue
            if any(n in DOWNLOADERS or n in DECODERS for n in upstream):
                self.add("runs code it downloads or decodes on the fly (e.g. curl … | sh)")
                continue
            literal = _echo_output(argvs[k - 1]) if k == 1 else None
            if literal is not None:
                if name in SHELLS or name in ("ssh",):
                    self.shell(literal, depth + 1)
                elif name in POWERSHELLS or name in ("iex", "invoke-expression"):
                    self.powershell(literal, depth + 1)
                elif name in _LANG:
                    self.code(_LANG[name], literal, depth + 1)
                elif name in _SQL_CLIENTS:
                    self.code("sql", literal, depth + 1)
            elif name in SHELLS or name in POWERSHELLS or name in ("iex", "invoke-expression"):
                self.add("pipes text into a shell, so what runs can't be checked")

    def _reads_stdin(self, name: str, argv) -> bool:
        args = argv[1:]
        if name in SHELLS:
            if any(re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a) for a in args):
                return False
            ops = _operands(args, {"-o", "+o", "-O", "+O"})
            return not ops or "-s" in args or ops[0] == "-"
        if name in POWERSHELLS:
            return any(a.lower() in ("-command", "-c", "-") for a in args) and (args[-1] == "-" or len(args) <= 2)
        if name in ("iex", "invoke-expression"):
            return True
        if name in _LANG:
            ops = _operands(args)
            return (not ops or ops[0] == "-") and not any(re.fullmatch(r"-[a-zA-Z]*[ceEr]", a) for a in args)
        if name in _SQL_CLIENTS:
            return True
        return False

    # --- one command ------------------------------------------------------------

    def command(self, argv: list[str], depth: int, stdin: str | None = None, scan_suffix: bool = True) -> list[str]:
        """Check one simple command (already expanded). Returns it with wrappers removed."""
        while argv and argv[0] in _KEYWORDS:
            argv = argv[1:]
        if not argv or argv[0] in _SKIP_COMMANDS:
            return []
        argv = self._unwrap(argv, depth)
        if not argv:
            return []
        if any(s in argv[0] for s in SENTINELS):
            if FETCH in argv[0]:
                self.add("runs code it downloads or decodes on the fly (e.g. curl … | sh)")
            else:
                self.add("runs a command whose name is hidden in a variable, substitution or wildcard")
            return argv
        name, args = progname(argv[0]), argv[1:]
        low = [a.lower() for a in args]

        if name in ("cd", "pushd", "chdir"):
            target = args[0] if args else _home()
            self.cwd = None if (target == "-" or any(s in target for s in SENTINELS)) else _norm(target, self.cwd)
            return argv
        if name in ("export", "declare", "typeset", "readonly", "local"):
            for a in args:
                if "=" in a and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
                    k, v = a.split("=", 1)
                    self.env[k] = v
            return argv
        if name == "unset":
            for a in args:
                self.env.pop(a, None)
            return argv

        if FETCH in " ".join(args) and (name in SHELLS or name in POWERSHELLS or name in _LANG
                                         or name in ("eval", "source", ".", "iex", "invoke-expression")):
            self.add("runs code it downloads or decodes on the fly (e.g. curl … | sh)")

        self._rules(name, args, low, depth, stdin)

        for a in args:
            self._read(a)

        if scan_suffix and name not in _HARMLESS_HOSTS:
            # A command embedded in another one (docker exec … rm -rf, kubectl exec -- python -c …).
            for i in range(1, len(argv)):
                inner = progname(argv[i])
                if inner in _EMBEDDED and inner != name:
                    self.command(argv[i:], depth + 1, scan_suffix=False)
                    break
        return argv

    def _unwrap(self, argv: list[str], depth: int, nested: bool = True) -> list[str]:
        """Strip wrappers like sudo, env, nohup, timeout, xargs. Wrappers that take a whole
        command string (watch, su -c, ssh host cmd) are checked here and return []."""
        for _ in range(20):
            if not argv:
                return argv
            n, rest = progname(argv[0]), argv[1:]
            if n in ("sudo", "doas", "pkexec", "run0"):
                rest = _skip_opts(rest, {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user",
                                         "--group", "--chdir", "--prompt", "--role", "--type", "--host"})
            elif n == "env":
                out, i = [], 0
                while i < len(rest):
                    a = rest[i]
                    if a in ("-u", "--unset", "-C", "--chdir"):
                        i += 2
                    elif a in ("-S", "--split-string") or a.startswith("--split-string="):
                        s = a.split("=", 1)[1] if "=" in a else (rest[i + 1] if i + 1 < len(rest) else "")
                        out = s.split() + rest[i + (1 if "=" in a else 2):]
                        break
                    elif a.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
                        i += 1
                    else:
                        out = rest[i:]
                        break
                rest = out
            elif n in ("nice", "renice"):
                rest = _skip_opts(rest, {"-n", "--adjustment"})
            elif n == "ionice":
                rest = _skip_opts(rest, {"-c", "-n", "-p", "-t", "--class", "--classdata"})
            elif n == "timeout":
                rest = _skip_opts(rest, {"-s", "-k", "--signal", "--kill-after"})[1:]
            elif n in ("nohup", "setsid", "unbuffer", "builtin", "torsocks", "proxychains", "proxychains4", "chronic",
                       "caffeinate", "catchsegv", "time", "stdbuf", "start", "call"):
                rest = _skip_opts(rest, {"-o", "-e", "-i", "-a"})
                if n == "start" and rest and rest[0] == "":
                    rest = rest[1:]
            elif n == "exec":
                rest = _skip_opts(rest, {"-a"})
            elif n == "command":
                if rest[:1] in (["-v"], ["-V"]):
                    return []
                rest = _skip_opts(rest)
            elif n in ("chroot", "flock", "taskset", "sg", "chrt"):
                if n == "sg" and "-c" in rest:
                    self.shell(rest[rest.index("-c") + 1] if rest.index("-c") + 1 < len(rest) else "", depth + 1)
                    return []
                rest = _skip_opts(rest, {"-w", "-E", "-c"})[1:]
            elif n == "xargs":
                rest = _skip_opts(rest, {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--max-procs",
                                         "--delimiter", "--arg-file", "--replace"})
            elif n == "busybox":
                pass
            elif n in ("strace", "ltrace"):
                rest = _skip_opts(rest, {"-o", "-e", "-p", "-s", "-u", "-E", "-a"})
            elif n == "watch":
                rest = _skip_opts(rest, {"-n", "--interval", "-d", "--differences"})
                if rest and nested:
                    self.shell(" ".join(rest), depth + 1)
                return []
            elif n in ("su", "runuser", "script"):
                if "-c" in rest or "--command" in rest:
                    flag = "-c" if "-c" in rest else "--command"
                    i = rest.index(flag)
                    if i + 1 < len(rest) and nested:
                        self.shell(rest[i + 1], depth + 1)
                    return []
                if n == "runuser" and "--" in rest:
                    rest = rest[rest.index("--") + 1:]
                else:
                    return []
            elif n in ("ssh", "wsl"):
                rest = _skip_opts(rest, {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-e",
                                         "-m", "-O", "-Q", "-S", "-W", "-w", "-B", "-d", "--distribution", "-u", "--user",
                                         "--cd"} if n == "ssh" else {"-d", "--distribution", "-u", "--user", "--cd"})
                if n == "ssh":
                    rest = rest[1:]  # the host
                if rest and nested:
                    if n == "wsl" and rest[0] in ("-e", "--exec"):
                        return rest[1:]
                    self.shell(" ".join(rest), depth + 1)
                    return []
                return [] if n == "wsl" else argv
            elif n == "parallel":
                rest = _skip_opts(rest, {"-j", "-S", "--jobs"})
                if ":::" in rest:
                    rest = rest[:rest.index(":::")]
            else:
                return argv
            if not rest:
                return []
            argv = rest
        return argv

    def _rules(self, name: str, args: list[str], low: list[str], depth: int, stdin: str | None) -> None:
        short, long_ = _short_flags(args), _long_flags(args)
        add = self.add
        win_flags = {a for a in low if a.startswith("/")}

        # Deleting and destroying
        if name == "rm":
            if short & set("rRf") or long_ & {"--recursive", "--force"}:
                add("deletes files recursively or forcefully (rm -r/-f)")
            for p in _operands(args):
                self._write(p)
        elif name in ("unlink", "rmdir", "shred", "truncate", "srm", "wipe"):
            if name in ("shred", "srm", "wipe"):
                add("destroys disks or filesystems" if any(a.startswith("/dev/") for a in args)
                    else "permanently destroys file contents (shred)")
            for p in _operands(args, {"-s", "--size", "-n", "-r"}):
                self._write(p)
        elif name == "find":
            if "-delete" in low:
                add("deletes files found by find")
            for flag in ("-exec", "-execdir", "-ok", "-okdir"):
                idxs = [i for i, a in enumerate(low) if a == flag]
                for i in idxs:
                    sub = []
                    for a in args[i + 1:]:
                        if a in (";", "+", "\\;"):
                            break
                        sub.append(a)
                    if sub:
                        if progname(sub[0]) in ("rm", "shred", "unlink", "rmdir", "srm"):
                            add("deletes files found by find")
                        self.command([s.replace("{}", "found-file") for s in sub], depth + 1)
        elif name in ("wipefs", "fdisk", "sfdisk", "parted", "gdisk", "sgdisk", "cfdisk", "blkdiscard", "mkswap",
                      "diskpart", "format", "format-volume", "clear-disk", "initialize-disk", "remove-partition",
                      "diskutil") or name.startswith("mkfs"):
            if name != "diskutil" or any(a in low for a in ("erasedisk", "erasevolume", "partitiondisk",
                                                              "zerodisk", "secureerase", "reformat")):
                add("destroys disks or filesystems")
        elif name in ("zpool", "zfs", "lvremove", "vgremove", "pvremove", "cryptsetup", "mdadm"):
            if any(a in low for a in ("destroy", "luksformat", "erase", "--zero-superblock", "lvremove")) or name in ("lvremove", "vgremove", "pvremove"):
                add("destroys disks or filesystems")
        elif name == "dd":
            for a in args:
                if a.startswith("of="):
                    add("writes raw data with dd")
                    self._write(a[3:])
        elif name in ("cipher",) and any(a.startswith("/w") for a in low):
            add("permanently destroys free disk space contents (cipher /w)")
        elif name in ("vssadmin", "wbadmin") and "delete" in low:
            add("deletes backups or shadow copies")
        elif name == "wmic" and "shadowcopy" in low and "delete" in low:
            add("deletes backups or shadow copies")

        # Windows deletion
        elif name in ("del", "erase") and (win_flags & {"/s", "/q", "/f"}):
            add("deletes files recursively or forcefully (del /s /q /f)")
        elif name in ("rd",) and "/s" in win_flags:
            add("deletes folders recursively (rd /s)")
        elif name in ("remove-item", "ri"):
            if any(a.startswith("-r") or a.startswith("-fo") for a in low):
                add("deletes files recursively or forcefully (Remove-Item -Recurse/-Force)")
            for p in _operands(args, {"-path", "-literalpath"}):
                self._write(p)
        elif name == "robocopy" and win_flags & {"/mir", "/purge"}:
            add("deletes files at the destination (robocopy /mir)")

        # System state
        elif name in ("ufw", "firewall-cmd", "nft", "pfctl", "new-netfirewallrule", "set-netfirewallprofile",
                      "remove-netfirewallrule", "disable-netfirewallrule", "set-netfirewallrule") or name.startswith(("iptables", "ip6tables")):
            add("changes the firewall")
        elif name == "netsh" and any(a in ("firewall", "advfirewall") for a in low):
            add("changes the firewall")
        elif name == "oci" and low[:1] == ["network"]:
            add("changes Oracle Cloud networking")
        elif name in ("shutdown", "reboot", "poweroff", "halt", "stop-computer", "restart-computer") or \
                (name in ("init", "telinit") and low[:1] in (["0"], ["6"])):
            add("shuts down or reboots the machine")
        elif name == "systemctl":
            if any(a in low for a in ("poweroff", "reboot", "halt", "kexec")):
                add("shuts down or reboots the machine")
            elif any(a in low for a in ("stop", "disable", "mask", "kill", "isolate")):
                add("stops or disables a service")
        elif (name == "service" and any(a in low for a in ("stop",))) or \
                (name == "launchctl" and any(a in low for a in ("unload", "bootout", "remove", "disable"))) or \
                (name == "sc" and any(a in low for a in ("stop", "delete", "config"))) or \
                name in ("stop-service",) or (name == "set-service" and "disabled" in low):
            add("stops or disables a service")
        elif name in ("userdel", "usermod", "useradd", "adduser", "deluser", "groupdel", "passwd", "chpasswd", "visudo",
                      "remove-localuser", "new-localuser", "set-localuser", "add-localgroupmember") or \
                (name == "dscl" and any(a in low for a in ("-delete", "-create", "-passwd", "-append"))) or \
                (name == "net" and low[:1] and low[0] in ("user", "localgroup")):
            add("changes users or passwords")
        elif name in ("chmod", "chown", "chgrp", "chattr", "setfacl", "icacls", "takeown"):
            recursive = "R" in short or "--recursive" in long_ or (win_flags & {"/t", "/r"})
            world = any(re.fullmatch(r"0?[0-7]?777|[ao]?\+[rwx]*w[rwx]*|a=rwx", a) for a in low) or "everyone" in " ".join(low)
            setuid = any(re.fullmatch(r"[0-7]?[4-7][0-7]{3}|[ug]?\+s", a) for a in low) and name == "chmod"
            if (name in ("chmod", "chown", "chgrp", "icacls", "takeown") and recursive) or world:
                add("changes permissions recursively or world-writable")
            if setuid:
                add("sets the setuid bit (programs that run as their owner)")
            for p in _operands(args, {"--reference"})[1 if name in ("chmod", "chown", "chgrp") else 0:]:
                self._write(p)
        elif name == "git":
            self._git(args, low)
        elif name in ("docker", "podman", "nerdctl"):
            sub = [a for a in low if not a.startswith("-")]
            if sub[:1] and sub[0] in ("rm", "rmi", "kill") or sub[:2] in (["system", "prune"], ["volume", "rm"], ["volume", "prune"],
                                                                         ["network", "rm"], ["container", "prune"], ["container", "rm"],
                                                                         ["image", "prune"], ["image", "rm"], ["builder", "prune"]) or \
                    (sub[:2] == ["compose", "down"] and ("-v" in low or "--volumes" in low)):
                add("deletes containers, images or volumes")
            if sub[:1] and sub[0] in ("run", "create", "exec") and (
                    "--privileged" in low or any(re.match(r"^/:|^/:/", a) for a in args) or
                    any(a in ("--pid=host", "--net=host", "--network=host") for a in low) or
                    any(re.match(r"^(-v|--volume)=?/:", a) for a in args)):
                add("runs a privileged container or mounts the host's root")
        elif name in ("kubectl", "helm", "terraform", "pulumi", "tofu"):
            if any(a in ("delete", "uninstall", "destroy", "-destroy", "drain") for a in low):
                add("deletes cloud or cluster resources")
        elif name in ("aws", "gcloud", "az", "doctl", "flyctl", "heroku", "vercel"):
            if any(a in ("delete", "rm", "rb", "terminate-instances", "destroy", "delete-bucket", "remove") or
                   a.startswith("delete-") for a in low):
                add("deletes cloud or cluster resources")
        elif name == "kill" and (any(re.fullmatch(r"-(9|kill|sigkill)", a) for a in low) or
                                 any(low[i] == "-s" and low[i + 1:i + 2] in (["kill"], ["9"], ["sigkill"]) for i in range(len(low)))):
            add("force-kills processes")
        elif name in ("killall", "pkill", "killall5", "skill") or (name == "taskkill" and "/f" in win_flags) or \
                (name in ("stop-process", "spps") and any(a.startswith("-f") for a in low)):
            add("force-kills processes")
        elif name in ("apt", "apt-get", "aptitude", "dnf", "yum", "apk", "zypper", "brew", "snap", "port", "choco",
                      "winget", "scoop", "pkg", "flatpak"):
            if any(a in ("remove", "purge", "autoremove", "erase", "del", "uninstall", "rm", "zap") for a in low):
                add("uninstalls packages")
        elif name == "pacman" and any(re.fullmatch(r"-R\w*", a) for a in args):
            add("uninstalls packages")
        elif name == "crontab":
            if "r" in short:
                add("deletes the crontab")
            elif _operands(args, {"-u"}):
                add("replaces the crontab")
        elif name in ("schtasks",) and any(a in ("/create", "/delete", "/change") for a in low):
            add("creates or changes scheduled tasks")
        elif name in ("reg", "remove-itemproperty", "set-itemproperty", "new-itemproperty", "regedit"):
            if name == "regedit" or any(a in ("delete", "add", "import", "restore", "load") for a in low) or name != "reg":
                add("changes the Windows registry")
        elif name == "bcdedit":
            add("changes the boot configuration")
        elif name in ("set-mppreference", "add-mppreference"):
            add("turns off or weakens Windows Defender")
        elif name == "set-executionpolicy":
            add("changes the PowerShell execution policy")
        elif name in ("mount", "umount") and args:
            add("mounts or unmounts filesystems")
        elif name == "sysctl" and ("w" in short or any("=" in a for a in args)):
            add("changes kernel settings")
        elif name in ("insmod", "rmmod") or (name == "modprobe" and ("r" in short or "--remove" in long_)):
            add("loads or unloads kernel modules")
        elif name in ("nc", "ncat", "netcat") and ("e" in short or "c" in short or "--exec" in long_ or "--sh-exec" in long_):
            add("opens a network shell (nc -e)")
        elif name == "socat" and any(re.search(r"\bexec:|\bsystem:", a, re.I) for a in args):
            add("opens a network shell (socat exec)")
        elif name in ("dropdb", "dropuser"):
            add("deletes database data")
        elif name in ("rsync",) and any(a.startswith("--delete") for a in low):
            add("deletes files at the destination (rsync --delete)")
        elif name in ("eval",):
            text = " ".join(args)
            if VAR in text:
                add("runs code built at run time (eval of a variable)")
            self.shell(text.replace(VAR, "").replace(SUB, ""), depth + 1)
        elif name in ("source", "."):
            for a in args:
                if re.search(r"\s", a):
                    self.shell(a, depth + 1)

        # Writes through common file tools
        if name in ("tee",):
            for p in _operands(args):
                self._write(p)
        elif name in ("cp", "mv", "install", "ln", "rsync", "scp", "copy", "move", "xcopy", "robocopy",
                      "copy-item", "move-item", "cpi", "mi", "ditto"):
            ops = _operands(args, {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "-g",
                                   "-e", "--rsh", "-destination", "-path"})
            target = next((args[i + 1] for i, a in enumerate(args) if a in ("-t", "--target-directory", "-destination")
                           and i + 1 < len(args)), None)
            if target:
                self._write(target)
            elif ops:
                self._write(ops[-1])
            if name in ("mv", "move", "move-item", "mi"):
                for p in (ops if target else ops[:-1]):
                    self._write(p)
        elif name in ("sed", "perl", "ruby") and ("i" in short or any(a.startswith("-i") for a in args) or "--in-place" in long_):
            ops = _operands(args, {"-e", "-f", "--expression", "--file"})
            if name == "sed" and not any(a in ("-e", "--expression", "-f", "--file") for a in args):
                ops = ops[1:]
            if name != "sed":
                ops = [o for o in ops if o not in _code_args(args)]
            for p in ops:
                self._write(p)
        elif name in ("curl", "wget"):
            for i, a in enumerate(args):
                if a in ("-o", "--output", "-O" if name == "wget" else "--output-document", "--output-document") and i + 1 < len(args):
                    self._write(args[i + 1])
        elif name in ("set-content", "add-content", "out-file", "new-item", "ac", "ni"):
            for p in _operands(args, {"-value", "-encoding", "-itemtype"}):
                self._write(p)

        # Code inside shells and interpreters
        if name in SHELLS:
            for i, a in enumerate(args):
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a):
                    rest = [x for x in args[i + 1:]]
                    if rest:
                        self.shell(rest[0], depth + 1)
                    break
            else:
                ops = _operands(args, {"-o", "+o", "-O", "+O"})
                if stdin is not None and (not ops or ops[0] == "-" or "-s" in args):
                    self.shell(stdin, depth + 1)
                for a in ops[:1]:
                    if re.search(r"\s", a):
                        self.shell(a, depth + 1)
        elif name == "ssh" and stdin is not None:
            self.shell(stdin, depth + 1)
        elif name in ("cmd",):
            for i, a in enumerate(low):
                if a in ("/c", "/k", "/r"):
                    self.cmd_exe(" ".join(args[i + 1:]), depth + 1)
                    break
                if re.fullmatch(r"/[ck].+", a):
                    self.cmd_exe(args[i][2:] + " " + " ".join(args[i + 1:]), depth + 1)
                    break
        elif name in POWERSHELLS:
            self._powershell_args(args, depth, stdin)
        elif name in ("iex", "invoke-expression"):
            self.add("runs code built at run time (Invoke-Expression)")
            self.powershell(" ".join(args), depth + 1)
        elif name in _LANG:
            lang = _LANG[name]
            code = _interpreter_code(name, args)
            if code is None and stdin is not None and self._reads_stdin(name, [name] + args):
                code = stdin
            if code is not None:
                self.code(lang, code, depth + 1)
        elif name in _SQL_CLIENTS:
            text = " ".join(args)
            self.code("sql", text + ("\n" + stdin if stdin else ""), depth + 1)

    def _git(self, args, low) -> None:
        rest = _skip_opts(args, {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
        if not rest:
            return
        sub, sargs = rest[0].lower(), rest[1:]
        slow = [a.lower() for a in sargs]
        short = _short_flags(sargs)
        bad = False
        if sub == "push":
            bad = ("f" in short or "d" in short or any(a.startswith(("--force", "--mirror", "--delete", "--prune")) for a in slow)
                   or any(a.startswith("+") or a.startswith(":") for a in _operands(sargs, {"-o", "--push-option"})))
        elif sub == "reset":
            bad = "--hard" in slow
        elif sub == "clean":
            bad = "f" in short or "--force" in slow
        elif sub == "branch":
            bad = "D" in short or ("--delete" in slow and ("--force" in slow or "f" in short))
        elif sub in ("checkout", "restore"):
            bad = ("f" in short and sub == "checkout") or "--force" in slow or (
                "." in sargs and ("--" in sargs or sub == "restore" or sargs == ["."]))
        elif sub == "stash":
            bad = bool(slow[:1]) and slow[0] in ("drop", "clear")
        elif sub in ("filter-branch", "filter-repo"):
            bad = True
        elif sub == "reflog":
            bad = bool(slow[:1]) and slow[0] in ("expire", "delete")
        elif sub == "gc":
            bad = any(a.startswith("--prune=now") for a in slow)
        elif sub == "update-ref":
            bad = "-d" in slow
        if bad:
            self.add("rewrites or discards git history/work")

    # --- code in other languages -----------------------------------------------------

    def code(self, lang: str, src: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.add("nests commands too deeply to check")
            return
        label = {"node": "JavaScript", "osascript": "AppleScript", "rscript": "R", "tclsh": "Tcl", "sql": "SQL"}.get(lang, lang.capitalize())
        runs_shell = False
        for rx, what in _CODE_RX.get(lang, []):
            if rx.search(src):
                self.add(what if lang == "sql" else f"runs {label} code that {what}")
                runs_shell |= what == "runs shell commands"
        literals = [a if a is not None else b for a, b in _STRING_LIT.findall(src)]
        literals = [_unescape(x) for x in literals]
        if lang in ("perl", "ruby"):
            literals += [a or b for a, b in re.findall(r"`([^`]*)`|qx\{([^}]*)\}", src)]
        if runs_shell:
            for lit in literals:
                if re.search(r"\s", lit) or progname(lit) in _EMBEDDED:
                    self.shell(lit, depth + 1)
        writes = bool(_WRITE_HINT.search(src))
        for lit in literals:
            if "/" in lit or "\\" in lit or lit.startswith("~"):
                if writes:
                    self._write(lit)
                self._read(lit)

    def _powershell_args(self, args, depth, stdin) -> None:
        with_value = ("-executionpolicy", "-ep", "-ex", "-windowstyle", "-w", "-inputformat", "-outputformat",
                      "-version", "-workingdirectory", "-wd", "-configurationname", "-settingsfile")
        i = 0
        while i < len(args):
            a = args[i].lower()
            if a in ("-encodedcommand", "-enc", "-e", "-ec", "-en", "-encoded") or (len(a) > 3 and "-encodedcommand".startswith(a)):
                self.add("runs hidden (base64-encoded) PowerShell")
                if i + 1 < len(args):
                    try:
                        decoded = base64.b64decode(args[i + 1] + "===").decode("utf-16-le", errors="replace")
                        self.powershell(decoded, depth + 1)
                    except (ValueError, UnicodeDecodeError):
                        pass
                return
            if a in ("-command", "-c", "-co", "-com") or (len(a) > 3 and "-command".startswith(a)):
                rest = " ".join(args[i + 1:])
                if rest.strip() == "-" and stdin:
                    rest = stdin
                self.powershell(rest, depth + 1)
                return
            if a in ("-file", "-f"):
                return
            if a in with_value:
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            self.powershell(" ".join(args[i:]), depth + 1)  # Windows PowerShell's default is -Command
            return
        if stdin:
            self.powershell(stdin, depth + 1)

    def powershell(self, src: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.add("nests commands too deeply to check")
            return
        if _PS_FETCH.search(src) and _PS_RUN.search(src):
            self.add("runs code it downloads or decodes on the fly (e.g. iwr … | iex)")
        if re.search(r"frombase64string", src, re.I) and _PS_RUN.search(src):
            self.add("runs hidden (base64-encoded) PowerShell")
        # The backtick is PowerShell's escape character: `r`m is rm.
        cleaned = re.sub(r"`(.)", r"\1", src)
        cleaned = cleaned.replace("{", " { ").replace("}", " ; } ")
        saved_env = self.env
        self.env = {}
        try:
            pipelines = parse(cleaned)
        except ParseError:
            pipelines = []
            for part in re.split(r"[;|\n]", cleaned):
                self.command(part.split(), depth + 1)
        for pipeline in pipelines:
            argvs = []
            for cmd in pipeline:
                for inner in cmd.nested_sources():
                    self.powershell(inner, depth + 1)
                argv = [a.replace(VAR, "$") for a in self._expand_all(cmd.words)]
                argv = [a for a in argv if a not in ("{", "}")]
                for op, target in cmd.redirects:
                    for path in self._expand_all([target]):
                        self._redirect(op, path)
                if argv and argv[0].startswith("$") and "=" in argv[1:2]:
                    argv = argv[2:]
                argvs.append(self.command(argv, depth + 1))
            self._pipeline(argvs, depth)
        self.env = saved_env

    # --- cmd.exe -----------------------------------------------------------------

    def cmd_exe(self, src: str, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            self.add("nests commands too deeply to check")
            return
        for pipeline in parse_cmd(src):
            argvs = []
            for cmd in pipeline:
                for op, target in cmd.redirects:
                    self._redirect(op, target)
                words = cmd.words
                while words and words[0].lower() in ("if", "not", "exist", "@echo", "for", "do") :
                    words = words[1:]
                argv = self._cmd_command(words, depth)
                argvs.append(argv)
            self._pipeline(argvs, depth)

    def _cmd_command(self, words, depth) -> list[str]:
        if not words:
            return []
        if words[0].startswith("@"):
            words = [words[0][1:]] + words[1:]
        argv = self.command(list(words), depth + 1)
        if not argv:
            # "for %i in (*) do del /q %i": look for a dangerous command later in the line
            for i in range(1, len(words)):
                if progname(words[i]) in _EMBEDDED:
                    return self.command(words[i:], depth + 1)
        return argv


_EMBEDDED = (SHELLS | INTERPRETERS | POWERSHELLS | {
    "rm", "shred", "unlink", "dd", "mkfs", "wipefs", "fdisk", "find", "chmod", "chown", "kill", "killall", "pkill",
    "systemctl", "shutdown", "reboot", "poweroff", "halt", "iptables", "ufw", "userdel", "passwd", "git", "eval",
    "del", "erase", "rd", "format", "diskpart", "reg", "cmd", "remove-item", "iex", "invoke-expression", "curl",
    "wget", "truncate", "crontab", "docker", "kubectl", "terraform", "psql", "mysql", "sqlite3"})


def _code_args(args) -> set[str]:
    out = set()
    for i, a in enumerate(args):
        if re.fullmatch(r"-[a-zA-Z]*[eE]", a) and i + 1 < len(args):
            out.add(args[i + 1])
    return out


def _interpreter_code(name: str, args: list[str]) -> str | None:
    """The inline program of an interpreter call (python -c, node -e, perl -e, awk '...'), or None."""
    if name in ("awk", "gawk", "mawk", "nawk"):
        ops = _operands(args, {"-F", "-v", "-f", "--field-separator", "--assign", "--file"})
        return None if "-f" in args else (ops[0] if ops else None)
    if name in ("deno",) and args[:1] == ["eval"]:
        return " ".join(args[1:])
    flags = {"python": r"-[a-zA-Z]*c", "pypy": r"-[a-zA-Z]*c", "node": r"-e|--eval|-p|--print", "nodejs": r"-e|--eval|-p|--print",
             "bun": r"-e|--eval|-p|--print", "perl": r"-[a-zA-Z]*[eE]", "ruby": r"-[a-zA-Z]*e", "php": r"-r",
             "osascript": r"-e", "lua": r"-e", "rscript": r"-e", "julia": r"-e", "tclsh": r"$^"}.get(name)
    if not flags:
        return None
    code = []
    for i, a in enumerate(args):
        if re.fullmatch(flags, a) and i + 1 < len(args):
            code.append(args[i + 1])
        elif name in ("python", "pypy") and re.fullmatch(r"-c.+", a):
            code.append(a[2:])
        elif a.startswith(("--eval=", "--print=")):
            code.append(a.split("=", 1)[1])
        elif name in ("python", "pypy") and (a == "-m" or not a.startswith("-")) and not code:
            break  # a module or a script file: nothing inline
    return "\n".join(code) if code else None


_RELOAD_RULES = [
    (re.compile(r"\bcaddy\s+reload\b|\bsystemctl\s+(reload|restart)\s+caddy\b", re.I),
     re.compile(r"\bcaddy\s+validate\b|anvil-caddy-apply", re.I),
     "reloads Caddy without running caddy validate first (use: sudo anvil-caddy-apply apply <file>)"),
    (re.compile(r"\bnginx\s+-s\s+reload\b|\bsystemctl\s+(reload|restart)\s+nginx\b", re.I),
     re.compile(r"\bnginx\s+-t\b", re.I),
     "reloads nginx without running nginx -t first"),
]


def dangerous_command(command: str, cwd=None, shell: str | None = None) -> list[str]:
    """Reasons this command needs a human's yes every time ([] if none).

    cwd: the directory it runs in (for relative paths). shell: "bash" or "cmd"; by default the
    shell Anvil uses on this OS (cmd.exe on Windows, where both syntaxes are checked)."""
    if len(command) > MAX_COMMAND_CHARS:
        return ["is too long to check safely"]
    shell = shell or ("cmd" if os.name == "nt" else "bash")
    a = _Analyzer(cwd)
    a.shell(command)
    if shell == "cmd":
        b = _Analyzer(cwd)
        b.cmd_exe(command)
        for r in b.reasons:
            a.add(r)
        if any(r.startswith("could not be parsed") for r in a.reasons) and len(b.reasons) == 0:
            # Valid cmd.exe syntax (e.g. an odd number of ' in "echo it's") isn't a POSIX parse error.
            a.reasons = [r for r in a.reasons if not r.startswith("could not be parsed")]
    for action, check, reason in _RELOAD_RULES:
        if action.search(command) and not check.search(command):
            a.add(reason)
    return a.reasons


# --- backups and policy ------------------------------------------------------------

BACKUP_NAME_RE = re.compile(r"(Caddyfile|\.conf$|\.service$|\.timer$|\.env$|\.ya?ml$|\.toml$|\.ini$|nginx)", re.I)


def should_back_up(path: Path, inside_project: bool) -> bool:
    return path.is_file() and (not inside_project or bool(BACKUP_NAME_RE.search(path.name)))


def backup_file(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = BACKUP_DIR / stamp / str(path.resolve()).lstrip("/\\").replace(":", "")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    _prune()
    return dest


def _prune(keep: int = 300) -> None:
    try:
        dirs = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir())
        for d in dirs[:-keep]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def load_policy() -> dict:
    """System-wide limits set by the server admin. The agent's user can't write this file."""
    try:
        data = json.loads(POLICY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def mode_ceiling(config_ceiling: str = "") -> str:
    """The most permissive mode allowed: the stricter of the policy file and the user's config."""
    candidates = [m for m in (load_policy().get("permission_ceiling"), config_ceiling,
                              os.environ.get("ANVIL_PERMISSION_CEILING")) if m in MODES]
    return min(candidates, key=MODES.index) if candidates else "auto"


def allowed(mode: str, ceiling: str) -> bool:
    return MODES.index(mode) <= MODES.index(ceiling)

