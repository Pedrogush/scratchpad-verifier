"""Best-effort static analysis of shell commands from Bash/PowerShell tool calls.

Nothing here executes anything. It recognizes the common ways an agent
creates files or runs code from a shell command:

* heredocs into files: ``cat > f <<'EOF'``, ``cat <<EOF > f``, ``tee f <<EOF``
* simple redirects: ``echo "text" > f``
* inline scripts: ``python - <<'EOF'``, ``python -c "..."``, ``node -e "..."``
* PowerShell: ``Set-Content``/``Out-File``/``Add-Content`` with here-strings
* deletions: ``rm``, ``del``, ``Remove-Item``

Unquoted heredocs (``<<EOF``) would be expanded by the shell; their body is
returned verbatim, with ``expanded=True`` so callers can flag it.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from dataclasses import dataclass, field

INTERPRETERS = {
    "python": "py", "python3": "py", "py": "py", "pypy3": "py",
    "node": "js", "deno": "ts", "bun": "js", "ts-node": "ts", "tsx": "ts",
    "bash": "sh", "sh": "sh", "zsh": "sh",
    "ruby": "rb", "perl": "pl", "php": "php",
    "powershell": "ps1", "pwsh": "ps1",
    "sqlite3": "sql", "psql": "sql", "mysql": "sql",
}
INLINE_FLAGS = {"py": "-c", "js": "-e", "ts": "-e", "rb": "-e", "pl": "-e", "php": "-r", "sh": "-c"}
WRAPPERS = {"sudo", "env", "time", "nohup", "exec", "command", "timeout", "xargs"}

HEREDOC_RE = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))"
)
MARKER_RE = re.compile(r"^__HEREDOC_(\d+)__$")
SEPARATORS = {"&&", "||", ";", "|", "&", ";;"}


@dataclass
class ShellWrite:
    path: str          # as written in the command, resolved against cwd
    content: str
    append: bool = False
    expanded: bool = False  # body may contain unexpanded $vars / $(...)
    how: str = ""


@dataclass
class InlineScript:
    lang: str          # file extension: py, js, sh, ...
    code: str
    interpreter: str
    how: str = ""


@dataclass
class ShellEffects:
    writes: list[ShellWrite]
    scripts: list[InlineScript]
    deletes: list[str]
    cwd: str | None    # cwd after any leading `cd`
    copies: list[tuple[str, str, bool]] = field(default_factory=list)  # (src, dst, is_move)
    # Files a command wrote whose content isn't in the transcript: (path, how).
    produced: list[tuple[str, str]] = field(default_factory=list)
    # Script files the command ran: (path, lang, cwd at the time).
    executed: list[tuple[str, str, str | None]] = field(default_factory=list)


def _is_windows(path: str | None) -> bool:
    return bool(path) and (bool(re.match(r"^[A-Za-z]:[\\/]", path)) or "\\" in path)


def resolve(path: str, cwd: str | None) -> str:
    path = path.strip()
    if path.startswith("~") or not cwd:
        return path
    if path.startswith("/"):  # POSIX or git-bash (/c/...) absolute path
        return posixpath.normpath(path)
    if _is_windows(cwd) or _is_windows(path):
        if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
            return ntpath.normpath(path)
        return ntpath.normpath(ntpath.join(cwd, path))
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path))


def _split_words(text: str) -> list[str]:
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="<>|&;")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return text.split()


def _newlines_to_semicolons(text: str) -> str:
    """Turn unquoted newlines into ';' (and drop backslash-newline continuations)."""
    out = []
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < len(text) and text[i + 1] == "\n":
            out.append(" ")
            i += 2
            continue
        elif c == "\n":
            out.append(" ; ")
            i += 1
            continue
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;"):
            # comment to end of line
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _segments(words: list[str]) -> list[tuple[list[str], str]]:
    """Split tokens into simple commands, each with the separator that follows it."""
    segs: list[tuple[list[str], str]] = []
    cur: list[str] = []
    for w in words:
        if w in SEPARATORS:
            segs.append((cur, w))
            cur = []
        else:
            cur.append(w)
    segs.append((cur, ""))
    return [(s, sep) for s, sep in segs if s]


def _extract_heredocs(command: str) -> tuple[str, list[tuple[str, bool]]]:
    """Remove heredoc bodies, replacing each operator with a __HEREDOC_n__ token."""
    lines = command.split("\n")
    out_lines: list[str] = []
    bodies: list[tuple[str, bool]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        ops = list(HEREDOC_RE.finditer(line))
        j = i + 1
        for m in ops:
            delim = m.group(2) or m.group(3) or m.group(4)
            strip_tabs = m.group(1) == "-"
            body = []
            while j < len(lines):
                cand = lines[j].lstrip("\t") if strip_tabs else lines[j]
                if cand.rstrip("\r") == delim:
                    j += 1
                    break
                body.append(cand)
                j += 1
            bodies.append(("\n".join(body) + "\n" if body else "", m.group(4) is not None))

        def repl(m: re.Match, start=len(bodies) - len(ops)) -> str:
            repl.n += 1
            return f" __HEREDOC_{start + repl.n - 1}__ "

        repl.n = 0
        out_lines.append(HEREDOC_RE.sub(repl, line))
        i = j
    return "\n".join(out_lines), bodies


def _program(words: list[str]) -> tuple[str, list[str]]:
    """Strip wrappers / VAR=x assignments and return (program, args)."""
    words = [w for w in words if not MARKER_RE.match(w)] + [w for w in words if MARKER_RE.match(w)]
    i = 0
    while i < len(words):
        w = words[i]
        if "=" in w and not w.startswith("-") and re.match(r"^[A-Za-z_]\w*=", w):
            i += 1
        elif w in WRAPPERS:
            i += 1
        elif w == "uv" and i + 1 < len(words) and words[i + 1] == "run":
            i += 2
        else:
            break
    if i >= len(words):
        return "", []
    prog = words[i].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    return prog, words[i + 1 :]


def _interp_lang(prog: str) -> str | None:
    if prog in INTERPRETERS:
        return INTERPRETERS[prog]
    if re.match(r"^python\d+(\.\d+)?$", prog):
        return "py"
    return None


def _redirect_target(words: list[str]) -> tuple[str | None, bool]:
    """stdout redirect target (ignores 2> and >&2 style redirects)."""
    for i, w in enumerate(words):
        if w in (">", ">>", ">|") and i + 1 < len(words):
            prev = words[i - 1] if i else ""
            nxt = words[i + 1]
            if prev in ("2",) or nxt.startswith("&") or nxt == "&":
                continue
            return nxt, w == ">>"
    return None, False


def _tee_target(words: list[str]) -> tuple[str | None, bool]:
    prog, args = _program(words)
    if prog != "tee":
        return None, False
    append = False
    for a in args:
        if a in ("-a", "--append"):
            append = True
        elif not a.startswith("-"):
            return a, append
    return None, False


def _cd_target(segment: str) -> str | None:
    words = _split_words(segment)
    if len(words) >= 2 and words[0] in ("cd", "pushd", "Set-Location", "sl"):
        return words[-1]
    return None


def _echo_content(args: list[str]) -> str | None:
    newline = True
    out = []
    for a in args:
        if a in (">", ">>", ">|"):
            break
        if not out and a == "-n":
            newline = False
        elif not out and a in ("-e", "-E"):
            continue
        else:
            out.append(a)
    return " ".join(out) + ("\n" if newline else "")


VAR_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
NO_TARGET = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")


def _expand(text: str, env: dict[str, str]) -> str:
    """Substitute $VAR / ${VAR} for variables assigned earlier in the command."""
    return VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), text)


def analyze_bash(command: str, cwd: str | None, env: dict[str, str] | None = None) -> ShellEffects:
    writes: list[ShellWrite] = []
    scripts: list[InlineScript] = []
    deletes: list[str] = []
    copies: list[tuple[str, str, bool]] = []
    produced: list[tuple[str, str]] = []
    executed: list[tuple[str, str, str | None]] = []
    made_dirs: set[str] = set()
    cur = cwd
    env = dict(env or {})

    def path(word: str) -> str:
        return resolve(_expand(word, env), cur)

    flat, bodies = _extract_heredocs(command)
    segs = _segments(_split_words(_newlines_to_semicolons(flat)))
    skip_seg = -1
    for n, (words, sep) in enumerate(segs):
        if n == skip_seg:
            continue
        # A ( ... ) or { ...; } group: its redirect applies to the whole group,
        # so we can't know the content.
        closes_group = any(
            (w.endswith(")") and "(" not in w) or w in ("}", ")")
            for w in words[: next((i for i, w in enumerate(words) if w in (">", ">>")), len(words))]
        )
        words = [w.lstrip("({") if i == 0 else w for i, w in enumerate(words)]
        words = [w.rstrip(")") if (w.endswith(")") and "(" not in w) else w for w in words]
        words = [w for w in words if w not in ("", ")", "}")]
        if not words:
            continue
        # Variable assignments (S=/x, export S=/x) before or instead of a command.
        lead = words[1:] if words[0] in ("export", "local", "declare", "readonly") else words
        assigns = [m for w in lead if (m := ASSIGN_RE.match(w))]
        if assigns and len(assigns) == len(lead):
            for m in assigns:
                env[m.group(1)] = _expand(m.group(2), env)
            continue

        prog, args = _program(words)
        if prog in ("cd", "pushd") and args:
            cur = path(args[-1])
            continue

        heredoc = next((int(m.group(1)) for w in words if (m := MARKER_RE.match(w))), None)
        target, append = _redirect_target(words)
        if target in NO_TARGET or (target and target.startswith("/dev/")):
            target = None
        lang = _interp_lang(prog)
        if heredoc is not None:
            body, expanded = bodies[heredoc]
            if prog == "tee":
                target, append = _tee_target(words)
            elif prog == "cat" and target is None and sep == "|" and n + 1 < len(segs):
                target, append = _tee_target(segs[n + 1][0])
                skip_seg = n + 1
            if prog in ("cat", "tee", "") and target and target not in NO_TARGET:
                writes.append(ShellWrite(path(target), body, append, expanded, "heredoc"))
                continue
            if lang:
                scripts.append(InlineScript(lang, body, prog, "heredoc"))
                produced.extend((resolve(p, cur), f"written by {prog} script") for p in script_outputs(body, lang))
            if target:
                produced.append((path(target), f"output of {prog}"))
            continue

        if lang and INLINE_FLAGS.get(lang) not in args:
            plain = [a for a in _plain_args(args) if a != "-"]
            if plain and "-m" not in args and not MARKER_RE.match(plain[0]):
                executed.append((path(plain[0]), lang, cur))
        elif not lang and prog and re.search(r"\.(py|sh|js)$", words[0]) and "=" not in words[0]:
            ext = words[0].rsplit(".", 1)[-1]
            executed.append((path(_expand(words[0], env)), {"py": "py", "sh": "sh", "js": "js"}[ext], cur))
        if lang and INLINE_FLAGS.get(lang) in args:
            k = args.index(INLINE_FLAGS[lang])
            if k + 1 < len(args):
                code = args[k + 1].rstrip("\n") + "\n"
                scripts.append(InlineScript(lang, code, prog, "inline"))
                produced.extend((resolve(p, cur), f"written by {prog} -c") for p in script_outputs(code, lang))
        if closes_group and target:
            produced.append((path(target), "output of a command group"))
            continue
        if prog in ("echo", "printf") and target:
            content = _echo_content(args) if prog == "echo" else _printf_content(args)
            if content is not None:
                expanded = any("$" in a for a in args)
                writes.append(ShellWrite(path(target), _expand(content, env), append, expanded, prog))
                continue
        if target:
            produced.append((path(target), f"output of {prog or 'command'}" + (" (appended)" if append else "")))
        if prog in ("rm", "unlink", "rmdir"):
            for a in _plain_args(args):
                deletes.append(path(a))
        elif prog == "mkdir":
            for d in _plain_args(args):
                made_dirs.add(path(d))
        elif prog in ("cp", "mv"):
            plain = _plain_args(args)
            if len(plain) >= 2:
                dst = path(plain[-1])
                if plain[-1].endswith(("/", "\\", ".")) or len(plain) > 2 or dst in made_dirs:
                    dst = dst.rstrip("/\\") + "/"
                for src in plain[:-1]:
                    copies.append((path(src), dst, prog == "mv"))
        elif prog == "tee":
            t, _ = _tee_target(words)
            if t:
                produced.append((path(t), "output of tee"))
        elif prog == "sed" and any(a.startswith("-i") for a in args):
            plain = _plain_args([a for a in args if not a.startswith("-")])
            for f in plain[1:]:
                produced.append((path(f), "edited in place by sed -i"))
        elif prog == "touch":
            for f in _plain_args(args):
                writes.append(ShellWrite(path(f), "", False, False, "touch"))
    return ShellEffects(writes, scripts, deletes, cur, copies, produced, executed)


SCRIPT_OUTPUT_RES = {
    "py": [
        # open('x', 'w') / open("x", mode="a")
        re.compile(r"\bopen\(\s*[rf]?(['\"])([^'\"\n]+)\1\s*,\s*(?:mode\s*=\s*)?(['\"])[^'\"]*[wax][^'\"]*\3"),
        # Path('x').write_text(...)
        re.compile(r"\bPath\(\s*[rf]?(['\"])([^'\"\n]+)\1\s*\)\.(?:write_text|write_bytes|open\(\s*['\"][wa])"),
        # df.to_csv('x'), plt.savefig('x'), wb.save('x'), ...
        re.compile(r"\.(?:to_csv|to_json|to_excel|to_parquet|to_html|savefig|save|write_image|write_html|dump_to)\(\s*[rf]?(['\"])([^'\"\n]+)\1"),
    ],
    "js": [
        re.compile(r"\b(?:writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream)\(\s*(['\"`])([^'\"`\n]+)\1"),
    ],
}


PY_CONST_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*=\s*(?:Path\()?\s*[rf]?(['\"])([^'\"\n]+)\2\s*\)?[ \t]*(?:#.*)?$", re.M)
PY_NAME_OUTPUT_RES = [
    re.compile(r"\bopen\(\s*([A-Za-z_]\w*)\s*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax]"),
    re.compile(r"\.(?:to_csv|to_json|to_excel|to_parquet|to_html|savefig|save|write_image|write_html)\(\s*([A-Za-z_]\w*)\s*[,)]"),
    re.compile(r"\b([A-Za-z_]\w*)\.(?:write_text|write_bytes)\("),
    re.compile(r"\bPath\(\s*([A-Za-z_]\w*)\s*\)\.(?:write_text|write_bytes)"),
]


def script_outputs(code: str, lang: str) -> list[str]:
    """File paths a script writes to (best effort: literals and simple constants)."""
    found: list[str] = []

    def add(p: str | None) -> None:
        if p and p not in found and not p.startswith("/dev/") and "{" not in p:
            found.append(p)

    for rx in SCRIPT_OUTPUT_RES.get(lang, []):
        for m in rx.finditer(code):
            add(m.group(2))
    if lang == "py":
        consts = {m.group(1): m.group(3) for m in PY_CONST_RE.finditer(code)}
        for rx in PY_NAME_OUTPUT_RES:
            for m in rx.finditer(code):
                value = consts.get(m.group(1))
                if value and ("/" in value or "\\" in value or "." in value):
                    add(value)
    return found


def _plain_args(args: list[str]) -> list[str]:
    """Positional arguments, without options or redirections."""
    out = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in (">", ">>", "<", "2", "1"):
            skip = a in (">", ">>", "<")
        elif not a.startswith("-"):
            out.append(a)
    return out


def _printf_content(args: list[str]) -> str | None:
    words = [a for a in args if a not in (">", ">>")]
    if not words:
        return None
    fmt = words[0]
    if "%" in fmt.replace("%%", ""):
        return None  # needs formatting we don't emulate
    return fmt.replace("%%", "%").replace("\\n", "\n").replace("\\t", "\t")


PS_HERESTRING_RE = re.compile(r"@(['\"])\r?\n(.*?)\r?\n\1@", re.S)
PS_WRITE_RE = re.compile(
    r"(Set-Content|Add-Content|Out-File)\b(?P<args>[^\n|;]*)", re.I,
)


def _ps_path(args: str) -> str | None:
    m = re.search(r"-(?:Path|FilePath|LiteralPath)\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))", args, re.I)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    m = re.match(r"\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s-]\S*))", args)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    return None


def analyze_powershell(command: str, cwd: str | None) -> ShellEffects:
    writes: list[ShellWrite] = []
    scripts: list[InlineScript] = []
    deletes: list[str] = []
    cur = cwd

    # Replace here-strings with placeholders so the rest can be scanned per line.
    strings: list[tuple[str, bool]] = []

    def stash(m: re.Match) -> str:
        strings.append((m.group(2) + "\n", m.group(1) == '"'))
        return f" __HERESTRING_{len(strings) - 1}__ "

    flat = PS_HERESTRING_RE.sub(stash, command)
    for stmt in re.split(r"[;\n]", flat):
        target = _cd_target(stmt.strip())
        if target:
            cur = resolve(target, cur)
        m = PS_WRITE_RE.search(stmt)
        if m:
            path = _ps_path(m.group("args"))
            hs = re.search(r"__HERESTRING_(\d+)__", stmt)
            if path and hs:
                body, expanded = strings[int(hs.group(1))]
                writes.append(
                    ShellWrite(resolve(path, cur), body, m.group(1).lower() == "add-content", expanded, m.group(1))
                )
        rm = re.match(r"\s*(Remove-Item|rm|del|ri)\s+(.*)", stmt, re.I)
        if rm:
            path = _ps_path(rm.group(2))
            if path:
                deletes.append(resolve(path, cur))
        py = re.search(r"\b(python3?|py|node)(?:\.exe)?\s+-\s*$", stmt.split("|")[-1], re.I)
        hs = re.search(r"__HERESTRING_(\d+)__\s*\|", stmt)
        if hs and py:
            scripts.append(InlineScript(_interp_lang(py.group(1).lower()) or "txt",
                                        strings[int(hs.group(1))][0], py.group(1), "herestring"))
    return ShellEffects(writes, scripts, deletes, cur)
