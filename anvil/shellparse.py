"""Small parsers for POSIX shell and cmd.exe command lines, for the safety checks.

They don't run anything. They split a command line into pipelines of simple commands the way
the shell would: quotes are removed, `$'\\x72m'` is decoded, `$(...)`, backticks and `<(...)`
are kept as nested sources (the safety checks look inside them), heredoc bodies and here-strings
are attached to their command, and redirections are listed separately.

Expansion (`expand`) substitutes variables the command line itself set (`X=rm; $X -rf /`),
does word splitting and simple brace expansion (`{rm,-rf,/}`), and marks anything that is only
known at run time with a sentinel so the checks can treat it as unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Sentinels left in expanded words for values only known at run time.
VAR = "\x00v"    # an unset/unknown variable
SUB = "\x00s"    # output of a command substitution we can't predict
FETCH = "\x00f"  # output of a substitution that downloads or decodes something
SENTINELS = (VAR, SUB, FETCH)


class ParseError(ValueError):
    pass


# A word is a list of segments: (kind, text, quoted). kind: "lit", "var" or "cmd".
Seg = tuple
Word = list


@dataclass
class Heredoc:
    delimiter: str
    expand: bool          # unquoted delimiter: $(...) inside the body runs
    strip_tabs: bool
    body: str = ""


@dataclass
class Command:
    words: list = field(default_factory=list)          # list[Word]
    redirects: list = field(default_factory=list)      # list[(op, Word)]
    heredoc: Heredoc | None = None
    herestring: Word | None = None

    def nested_sources(self) -> list[str]:
        """Every $(...), `...` and <(...) source in this command (they run too)."""
        out = []
        words = list(self.words) + [w for _, w in self.redirects]
        if self.herestring:
            words.append(self.herestring)
        for w in words:
            out += [text for kind, text, _ in w if kind == "cmd"]
        if self.heredoc and self.heredoc.expand:
            out += [text for kind, text, _ in _dq_segments(self.heredoc.body) if kind == "cmd"]
        return out


_META = set(" \t\n;&|()<>")
_OPS = ("&&", "||", ";;", "|&", "|", "&", ";", "(", ")", "\n")
_REDIR = re.compile(r"(\d*)(&>>|&>|>>|>\||>&|<<<|<<-|<<|<>|<&|>|<)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]|[@*#?$!-]")


def match_paren(s: str, i: int) -> int:
    """Index of the ')' matching the '(' at s[i], skipping quoted text."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ParseError("unterminated quote")
            i = j + 1
            continue
        if c == '"' or c == "`":
            i = _skip_quoted(s, i, c)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("unbalanced parentheses")


def _skip_quoted(s: str, i: int, q: str) -> int:
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1
        if q == '"' and s.startswith("$(", i):
            i = match_paren(s, i + 1) + 1
            continue
        i += 1
    raise ParseError("unterminated quote")


_ANSI = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
         "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(s: str, i: int) -> tuple[str, int]:
    """Decode $'...' starting at the quote at s[i]. Returns (text, index after closing quote)."""
    out, i = [], i + 1
    while i < len(s):
        c = s[i]
        if c == "'":
            return "".join(out), i + 1
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in _ANSI:
                out.append(_ANSI[nxt])
                i += 2
            elif nxt == "x":
                m = re.match(r"[0-9a-fA-F]{1,2}", s[i + 2:])
                out.append(chr(int(m.group(), 16)) if m else "x")
                i += 2 + (len(m.group()) if m else 0)
            elif nxt in "uU":
                m = re.match(r"[0-9a-fA-F]{1,8}" if nxt == "U" else r"[0-9a-fA-F]{1,4}", s[i + 2:])
                out.append(chr(int(m.group(), 16)) if m else nxt)
                i += 2 + (len(m.group()) if m else 0)
            elif nxt in "01234567":
                m = re.match(r"[0-7]{1,3}", s[i + 1:])
                out.append(chr(int(m.group(), 8)))
                i += 1 + len(m.group())
            elif nxt == "c" and i + 2 < len(s):
                out.append(chr(ord(s[i + 2]) & 0x1F))
                i += 3
            else:
                out.append("\\" + nxt)
                i += 2
            continue
        out.append(c)
        i += 1
    raise ParseError("unterminated quote")


def _dollar(s: str, i: int, quoted: bool) -> tuple[Seg | None, int]:
    """Parse a $-expansion at s[i]. Returns (segment or None for a literal '$', next index)."""
    if s.startswith("$((", i):
        j = match_paren(s, i + 1)
        return ("lit", s[i:j + 1], True), j + 1
    if s.startswith("$(", i):
        j = match_paren(s, i + 1)
        return ("cmd", s[i + 2:j], quoted), j + 1
    if s.startswith("${", i):
        j = s.find("}", i + 2)
        if j < 0:
            raise ParseError("unterminated ${")
        inner = s[i + 2:j].lstrip("!#")
        m = _NAME.match(inner)
        name = m.group() if m else inner
        # ${X:-$(cmd)} etc.: the default can run a command too
        segs = [("var", name, quoted)]
        rest = inner[len(name):]
        if "$(" in rest or "`" in rest:
            segs += [x for x in _dq_segments(rest) if x[0] == "cmd"]
        return segs if len(segs) > 1 else segs[0], j + 1
    m = _NAME.match(s, i + 1)
    if m:
        return ("var", m.group(), quoted), m.end()
    return None, i + 1


def _backtick(s: str, i: int) -> tuple[str, int]:
    out, i = [], i + 1
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] in "`\\$":
            out.append(s[i + 1])
            i += 2
            continue
        if c == "`":
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise ParseError("unterminated backtick")


def _dq_segments(text: str) -> list:
    """Segments of text as if it were inside double quotes (for heredoc bodies)."""
    segs, buf, i = [], [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in '$`"\\\n':
            buf.append(text[i + 1])
            i += 2
        elif c == "$":
            try:
                seg, i = _dollar(text, i, True)
            except ParseError:
                buf.append(c)
                i += 1
                continue
            if seg is None:
                buf.append("$")
            else:
                if buf:
                    segs.append(("lit", "".join(buf), True))
                    buf = []
                segs += seg if isinstance(seg, list) else [seg]
        elif c == "`":
            try:
                src, i = _backtick(text, i)
            except ParseError:
                buf.append(c)
                i += 1
                continue
            if buf:
                segs.append(("lit", "".join(buf), True))
                buf = []
            segs.append(("cmd", src, True))
        else:
            buf.append(c)
            i += 1
    if buf:
        segs.append(("lit", "".join(buf), True))
    return segs


class _Lexer:
    def __init__(self, s: str):
        self.s, self.i = s, 0
        self.pending: list[Heredoc] = []

    def word(self) -> Word:
        s, segs, buf = self.s, [], []

        def flush(quoted=False):
            if buf:
                segs.append(("lit", "".join(buf), quoted))
                buf.clear()

        while self.i < len(s):
            c = s[self.i]
            if c in _META:
                if c in "<>" and s.startswith("(", self.i + 1):
                    pass  # process substitution inside a word
                else:
                    break
            if c in "<>" and s.startswith("(", self.i + 1):
                flush()
                j = match_paren(s, self.i + 1)
                segs.append(("cmd", s[self.i + 2:j], False))
                self.i = j + 1
            elif c == "\\":
                flush()
                if s.startswith("\n", self.i + 1):
                    self.i += 2
                    continue
                if self.i + 1 < len(s):
                    segs.append(("lit", s[self.i + 1], True))
                self.i += 2
            elif c == "'":
                flush()
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("unterminated quote")
                segs.append(("lit", s[self.i + 1:j], True))
                self.i = j + 1
            elif s.startswith("$'", self.i):
                flush()
                text, self.i = _ansi_c(s, self.i + 1)
                segs.append(("lit", text, True))
            elif c == '"' or s.startswith('$"', self.i):
                flush()
                start = self.i + (2 if c == "$" else 1)
                end = _skip_quoted(s, start - 1, '"')
                segs += _dq_segments(s[start:end - 1]) or [("lit", "", True)]
                self.i = end
            elif c == "$":
                flush()
                seg, self.i = _dollar(s, self.i, False)
                if seg is None:
                    buf.append("$")
                else:
                    segs += seg if isinstance(seg, list) else [seg]
            elif c == "`":
                flush()
                src, self.i = _backtick(s, self.i)
                segs.append(("cmd", src, False))
            else:
                buf.append(c)
                self.i += 1
        flush()
        return segs

    def heredoc_bodies(self) -> None:
        s = self.s
        for hd in self.pending:
            lines = []
            while self.i < len(s):
                j = s.find("\n", self.i)
                line = s[self.i:] if j < 0 else s[self.i:j]
                self.i = len(s) if j < 0 else j + 1
                check = line.lstrip("\t") if hd.strip_tabs else line
                if check == hd.delimiter:
                    break
                lines.append(check)
            hd.body = "\n".join(lines)
        self.pending = []

    def tokens(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in " \t":
                self.i += 1
                continue
            if c == "\\" and s.startswith("\n", self.i + 1):
                self.i += 2
                continue
            if c == "#":
                j = s.find("\n", self.i)
                self.i = len(s) if j < 0 else j
                continue
            if c == "\n":
                self.i += 1
                yield ("op", "\n")
                self.heredoc_bodies()
                continue
            if c in "<>" and s.startswith("(", self.i + 1):
                yield ("word", self.word())
                continue
            m = _REDIR.match(s, self.i)
            if m:
                op = m.group(2)
                self.i = m.end()
                while self.i < len(s) and s[self.i] in " \t":
                    self.i += 1
                if op in ("<<", "<<-"):
                    raw_start = self.i
                    w = self.word()
                    raw = s[raw_start:self.i]
                    hd = Heredoc("".join(t for _, t, _ in w), expand=not any(q in raw for q in "'\"\\"),
                                 strip_tabs=op == "<<-")
                    self.pending.append(hd)
                    yield ("heredoc", hd)
                elif op == "<<<":
                    yield ("herestring", self.word())
                else:
                    w = self.word()
                    text = "".join(t for _, t, _ in w)
                    if op in (">&", "<&") and (re.fullmatch(r"\d+-?|-", text) or not w):
                        continue  # fd duplication, not a file
                    yield ("redir", op, w)
                continue
            for op in _OPS:
                if s.startswith(op, self.i):
                    self.i += len(op)
                    yield ("op", op)
                    break
            else:
                yield ("word", self.word())
        if self.pending:
            self.heredoc_bodies()


def parse(src: str) -> list[list[Command]]:
    """Split a POSIX shell command line into pipelines of simple commands."""
    pipelines: list[list[Command]] = []
    pipe: list[Command] = []
    cmd = Command()

    def end_cmd():
        nonlocal cmd
        if cmd.words or cmd.redirects or cmd.heredoc or cmd.herestring:
            pipe.append(cmd)
        cmd = Command()

    def end_pipe():
        nonlocal pipe
        end_cmd()
        if pipe:
            pipelines.append(pipe)
        pipe = []

    for tok in _Lexer(src).tokens():
        kind = tok[0]
        if kind == "word":
            cmd.words.append(tok[1])
        elif kind == "redir":
            cmd.redirects.append((tok[1], tok[2]))
        elif kind == "heredoc":
            cmd.heredoc = tok[1]
        elif kind == "herestring":
            cmd.herestring = tok[1]
        elif tok[1] in ("|", "|&"):
            end_cmd()
        else:
            end_pipe()
    end_pipe()
    return pipelines


# --- expansion ---------------------------------------------------------------

_BRACE = re.compile(r"^(.*?)\{([^{}]*,[^{}]*)\}(.*)$", re.S)


def _brace(text: str, depth: int = 0) -> list[str]:
    m = _BRACE.match(text)
    if not m or depth > 4:
        return [text]
    out = []
    for alt in m.group(2).split(","):
        out += _brace(m.group(1) + alt + m.group(3), depth + 1)
    return out


def expand(word: Word, env: dict, sub_value=None) -> list[tuple[str, bool]]:
    """Expand one word into fields. Returns [(text, has_unquoted_glob)].

    env maps known variable names to values. sub_value(src) returns the predicted output of a
    command substitution, or one of the sentinels."""
    if all(k == "lit" and not q for k, _, q in word):
        text = "".join(t for _, t, _ in word)
        return [(t, bool(re.search(r"[*?\[]", t))) for t in _brace(text)]
    fields: list[list] = [["", False]]
    started = False
    for kind, text, quoted in word:
        if kind == "lit":
            fields[-1][0] += text
            fields[-1][1] |= (not quoted) and bool(re.search(r"[*?\[]", text))
            started = True
            continue
        if kind == "var":
            value = env.get(text)
            if value is None:
                fields[-1][0] += VAR
                started = True
                continue
        else:
            value = sub_value(text) if sub_value else SUB
        if quoted or value in SENTINELS:
            fields[-1][0] += value
            started = True
            continue
        parts = value.split()
        if not parts:
            if value and started:
                fields.append(["", False])  # ${IFS} on its own splits words
                started = False
            continue
        if value[:1].isspace() and started:
            fields.append(["", False])
        fields[-1][0] += parts[0]
        for p in parts[1:]:
            fields.append([p, False])
        if value[-1:].isspace():
            fields.append(["", False])
            started = False
        else:
            started = True
    return [(t, g) for t, g in fields if t or started]


# --- cmd.exe -------------------------------------------------------------------

@dataclass
class CmdCommand:
    words: list = field(default_factory=list)       # list[str]
    redirects: list = field(default_factory=list)   # list[(op, str)]


def parse_cmd(src: str) -> list[list[CmdCommand]]:
    """Split a cmd.exe command line into pipelines. Handles "quotes", ^ escapes, & && || |,
    ( ) blocks and redirections. %VARS% are left as written."""
    pipelines, pipe, cmd = [], [], CmdCommand()
    buf, quoted, i = [], False, 0
    pending_redir = None

    def end_word():
        nonlocal buf, pending_redir
        if buf:
            w = "".join(buf)
            if pending_redir:
                cmd.redirects.append((pending_redir, w))
                pending_redir = None
            else:
                cmd.words.append(w)
            buf = []

    def end_cmd():
        nonlocal cmd
        end_word()
        if cmd.words or cmd.redirects:
            pipe.append(cmd)
        cmd = CmdCommand()

    def end_pipe():
        nonlocal pipe
        end_cmd()
        if pipe:
            pipelines.append(pipe)
        pipe = []

    while i < len(src):
        c = src[i]
        if quoted:
            if c == '"':
                quoted = False
            else:
                buf.append(c)
            i += 1
            continue
        if c == '"':
            quoted = True
            i += 1
        elif c == "^" and i + 1 < len(src):
            buf.append(src[i + 1])
            i += 2
        elif c in " \t,;=" and not (c in ",;=" and buf):
            end_word()
            i += 1
        elif c in "\r\n":
            end_pipe()
            i += 1
        elif src.startswith("&&", i) or src.startswith("||", i):
            end_pipe()
            i += 2
        elif c == "&":
            end_pipe()
            i += 1
        elif c == "|":
            end_cmd()
            i += 1
        elif c in "()" and not buf:
            end_pipe()
            i += 1
        elif c == ")":
            end_pipe()
            i += 1
        else:
            m = re.match(r"(\d?)(>>|>|<)(&\d)?", src[i:]) if (c in "<>" or (c.isdigit() and src[i + 1:i + 2] in "<>" and not buf)) else None
            if m:
                end_word()
                i += m.end()
                if not m.group(3):
                    pending_redir = m.group(2)
            else:
                buf.append(c)
                i += 1
    end_pipe()
    return pipelines
