"""Plain-language terminal text: the reply language setting, approval wording, permission modes,
and short previews of long commands and outputs.

English stays the default. With "language": "hinglish" Anvil talks in simple Hinglish (Hindi in
Roman letters mixed with everyday English words) and the terminal UI uses the same."""

LANGUAGES = ("english", "hinglish")
MODES = ("ask", "edits", "auto")  # same order as safety.MODES: least to most permissive


def language_of(config) -> str:
    lang = str(getattr(config, "language", "") or "").strip().lower()
    return lang if lang in LANGUAGES else "english"


STYLE_PROMPTS = {
    "english": "",
    "hinglish": """
# How to talk to the user
The user prefers simple Hinglish: Hindi written in English (Roman) letters, mixed with everyday English words, the way a friendly senior explains things to a 14-year-old.
- Write every reply in Hinglish. Short sentences. No jargon. If a technical word is unavoidable, explain it in a few words the first time, e.g. "venv (is project ka alag Python)".
- Before each tool call, write one short line: kya kar rahe ho aur kyun. Example: "Ab tests chala raha hoon, taaki pata chale sab theek hai."
- For every bash call, fill `description` with one short Hinglish line saying what the command does in plain words, e.g. "OCR wale packages install kar raha hoon".
- End with a short wrap-up in 2-4 lines: kya hua, kya baaki hai, aur user ko kya karna hai (agar kuch). Avoid long bullet lists, tables and headings unless asked.
- If you need the user's OK or a choice, ask one simple question with clear options.
- Keep code, commands, file names, paths and error messages exactly as they are; don't translate them.
""",
}


def style_prompt(language: str) -> str:
    return STYLE_PROMPTS.get(language, "")


TEXT = {
    "english": {
        "run_title": "Run this command?",
        "browser_title": "Browser action?",
        "edit_title": "Change {path}?",
        "danger_title": "⚠ Risky · {title}",
        "keys": "[bold]y[/] yes · [bold]a[/] yes, and don't ask again this session · [bold]n[/] no · or type what to do instead",
        "keys_more": " · [bold]v[/] view all",
        "danger_keys": "[red bold]This could delete or break things.[/] Type [bold]yes[/] to allow it once, anything else to say no (or type what to do instead): ",
        "more_lines": "… (+{n} more lines)",
        "earlier_lines": "… ({n} earlier lines)",
        "more_lines_view": "… (+{n} more lines, press v to see all)",
        "denied": "Okay, not running it.",
        "interrupted": "[yellow]⏹ Stopped.[/] [dim]Tell the agent what to do instead, or continue.[/]",
        "ok": "done",
        "failed": "failed (exit code {code})",
        "always_blocked": "Can't skip the question for this: the permission limit on this machine is '{ceiling}'. "
                          "Anvil will keep asking (set by /etc/anvil/policy.json or permission_ceiling in your config).",
        "always_on_bash": "Okay. Commands in this session run without asking now. Risky ones (deleting files, force pushes) still ask.",
        "always_on_edit": "Okay. File edits in this session happen without asking now.",
        "always_on_other": "Okay. Won't ask again for this in this session.",
        "mode_ask": "ask before changes",
        "mode_edits": "file edits run by themselves, commands ask",
        "mode_auto": "full auto (risky commands still ask)",
        "mode_line": "mode: {mode} · Shift+Tab to change",
        "mode_set": "Mode: [bold]{mode}[/]",
        "mode_limited": "The permission limit on this machine is '{ceiling}', so modes above it are skipped.",
        "lang_set": "Language: [bold]{lang}[/]",
        "banner_hint": "/help for commands · Shift+Tab changes mode · Ctrl+C stops · Ctrl+D quits",
        "setup_lang": "Reply language: [bold]1[/] English  [bold]2[/] Hinglish (simple Hindi + English)",
    },
    "hinglish": {
        "run_title": "Ye command chalaun?",
        "browser_title": "Browser mein ye karun?",
        "edit_title": "{path} badlun?",
        "danger_title": "⚠ Dhyan do · {title}",
        "keys": "[bold]y[/] haan · [bold]a[/] haan, aur is session mein dobara mat poocho · [bold]n[/] nahi · ya likho kya karna hai",
        "keys_more": " · [bold]v[/] poora dekho",
        "danger_keys": "[red bold]Isse files delete ya kuch kharab ho sakta hai.[/] Sirf ek baar allow karne ke liye [bold]yes[/] likho, warna kuch aur (ya likho kya karna hai): ",
        "more_lines": "… (+{n} lines aur)",
        "earlier_lines": "… (upar {n} lines aur)",
        "more_lines_view": "… (+{n} lines aur, poora dekhne ke liye v dabao)",
        "denied": "Theek hai, nahi chalaya.",
        "interrupted": "[yellow]⏹ Ruk gaya.[/] [dim]Batao ab kya karna hai, ya 'continue' likho.[/]",
        "ok": "ho gaya",
        "failed": "fail hua (exit code {code})",
        "always_blocked": "Is machine pe permission limit '{ceiling}' hai, isliye Anvil har baar poochega. "
                          "(Ye /etc/anvil/policy.json ya config ke permission_ceiling se aata hai.)",
        "always_on_bash": "Theek hai. Is session mein ab commands bina pooche chalenge. Khatarnak wale (files delete, force push) phir bhi poochenge.",
        "always_on_edit": "Theek hai. Is session mein ab file edits bina pooche honge.",
        "always_on_other": "Theek hai. Is session mein iske liye dobara nahi poochunga.",
        "mode_ask": "har badlav se pehle poochega",
        "mode_edits": "file edits khud, commands pe poochega",
        "mode_auto": "sab khud karega (khatarnak cheezein phir bhi poochega)",
        "mode_line": "mode: {mode} · badalne ke liye Shift+Tab",
        "mode_set": "Mode: [bold]{mode}[/]",
        "mode_limited": "Is machine pe permission limit '{ceiling}' hai, isse upar wale modes nahi milenge.",
        "lang_set": "Bhasha: [bold]{lang}[/]",
        "banner_hint": "/help se commands · Shift+Tab se mode badlo · Ctrl+C se roko · Ctrl+D se band karo",
        "setup_lang": "Jawab kis bhasha mein? [bold]1[/] English  [bold]2[/] Hinglish (simple Hindi + English)",
    },
}


def t(language: str, key: str, **kw) -> str:
    text = TEXT.get(language, TEXT["english"]).get(key) or TEXT["english"][key]
    return text.format(**kw) if kw else text


def mode_label(language: str, mode: str) -> str:
    return t(language, f"mode_{mode}")


def next_mode(current: str, ceiling: str) -> str:
    """The next permission mode in ask -> edits -> auto -> ask, skipping modes above the ceiling."""
    limit = MODES.index(ceiling) if ceiling in MODES else len(MODES) - 1
    options = MODES[:limit + 1]
    i = options.index(current) if current in options else -1
    return options[(i + 1) % len(options)]


def clip(text: str, max_lines: int, width: int = 220) -> tuple[str, int]:
    """First max_lines lines of text, each cut to width characters; also how many lines were left out."""
    lines = text.splitlines() or [""]
    shown = [ln if len(ln) <= width else ln[:width - 1] + "…" for ln in lines[:max_lines]]
    return "\n".join(shown), max(0, len(lines) - max_lines)


def clip_tail(text: str, max_lines: int, width: int = 220) -> tuple[str, int]:
    """Last max_lines non-empty lines (where results and errors usually are), and how many were left out."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    shown = [ln if len(ln) <= width else ln[:width - 1] + "…" for ln in lines[-max_lines:]]
    return "\n".join(shown), max(0, len(lines) - max_lines)


def one_line(text: str, width: int) -> str:
    """Text squeezed onto one line: newlines become ' ⏎ ', cut to width characters."""
    flat = " ⏎ ".join(ln.strip() for ln in str(text).splitlines() if ln.strip())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def bash_summary(language: str, result: str) -> tuple[str, str, bool]:
    """(status word, output without the trailing exit-code line, failed?) for a bash tool result."""
    lines = result.rstrip().splitlines()
    if lines and lines[-1].startswith("[exit code "):
        code = lines[-1][len("[exit code "):-1].strip()
        body = "\n".join(lines[:-1]).strip()
        if code == "0":
            return t(language, "ok"), body, False
        return t(language, "failed", code=code), body, True
    return "", result, False


def ask_key(message, keys: dict[str, str], default: str = "yes") -> str:
    """Ask with single-key answers: pressing one of `keys` (on an empty line) answers at once,
    without Enter. Typing anything else starts a free-text answer, sent with Enter. Enter on an
    empty line gives `default`, Esc gives "no". Ctrl+C / Ctrl+D raise KeyboardInterrupt."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()
    empty = Condition(lambda: get_app().current_buffer.text == "")

    def bind(key: str, value: str):
        @kb.add(key, filter=empty)
        def _(event):
            event.app.exit(result=value)

    for key, value in keys.items():
        bind(key, value)
        if key.isalpha() and key.upper() not in keys:
            bind(key.upper(), value)

    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result="no")

    try:
        ans = PromptSession(key_bindings=kb).prompt(message)
    except EOFError:
        raise KeyboardInterrupt
    return parse_answer(ans, keys, default)


def parse_answer(ans: str, keys: dict[str, str] | None = None, default: str = "yes") -> str:
    """Map a typed approval answer to yes / always / no / one of `keys`' values, or the free text."""
    ans = (ans or "").strip()
    keys = keys or {}
    if ans in keys.values():
        return ans
    if ans.lower() in keys:
        return keys[ans.lower()]
    low = ans.lower()
    if low == "":
        return default
    if low in ("y", "yes", "haan", "ha", "han", "ok"):
        return "yes"
    if low in ("a", "always"):
        return "always"
    if low in ("n", "no", "nahi", "na"):
        return "no"
    return ans
