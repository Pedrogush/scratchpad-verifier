"""Best-effort static analysis of shell commands from Bash/PowerShell tool calls.

Nothing here executes anything. It recognizes the common ways an agent
creates files or runs code from a shell command:

* heredocs into files: ``cat > f <<'EOF'``, ``cat <<EOF > f``, ``tee f <<EOF``
* simple redirects: ``echo "text" > f``
* inline scripts: ``python - <<'EOF'``, ``python -c "..."``, ``node -e "..."``
* PowerShell: ``Set-Content``/``Out-File``/``Add-Content`` with here-strings
* in-place edits: ``sed -i`` (replayed by ``sed.py``)
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
from typing import Callable

from .sed import parse_in_place

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
    # The write may not have happened as recorded: it sits in an if/else branch,
    # a loop or after ||, or its content has shell expansions we can't resolve.
    uncertain: bool = False
    # Content with unresolved expansions: literal strings and Holes. None when
    # the content is fully known.
    parts: list | None = None
    conditional: bool = False  # it may not have run at all
    # An in-place edit (sed -i): new content = transform(old content).
    transform: Callable[[str], str] | None = None
    backup: str | None = None  # where the edit saved the old content (sed -i.bak)
    seg: int = 0  # which simple command of the shell command wrote it


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
    # Script files the command ran: (path, lang, cwd at the time, argv).
    executed: list[tuple] = field(default_factory=list)
    # Simple commands (by index, like ShellWrite.seg) that print whole files (cat f).
    observed: list[int] = field(default_factory=list)


def _is_windows(path: str | None) -> bool:
    return bool(path) and (bool(re.match(r"^[A-Za-z]:[\\/]", path)) or "\\" in path)


def resolve(path: str, cwd: str | None) -> str:
    path = path.strip()
    if path.startswith(("~", "$")) or not cwd:
        return path  # unknown home / variable: don't guess
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


# Stand-ins the analysis passes around in place of the real characters:
# a $ or ` the shell won't expand (single quotes, \$), and a whole $(...) or
# `...` command substitution, whose output we can't know.
QUOTED_DOLLAR, QUOTED_TICK, SUBST = "\x00", "\x01", "\x02"
UNEXPANDED = {"$": QUOTED_DOLLAR, "`": QUOTED_TICK}


def _subst_end(text: str, i: int) -> int | None:
    """Index just past the ) that closes a $( opened before text[i]."""
    depth, quote = 1, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _protect(text: str) -> str:
    """Mark the $ and ` the shell leaves alone, and replace command substitutions with SUBST."""
    out = []
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            out.append(UNEXPANDED.get(c, c))
            i += 1
            continue
        if c == "\\" and i + 1 < len(text):
            out.append(UNEXPANDED.get(text[i + 1], text[i : i + 2]))
            i += 2
            continue
        end = None
        if c == "$" and text[i + 1 : i + 2] == "(":
            end = _subst_end(text, i + 2)
        elif c == "`":
            end = text.find("`", i + 1) + 1 or None
        if end:
            out.append(SUBST)
            i = end
            continue
        if c == '"':
            quote = None if quote else '"'
        elif c == "'" and not quote:
            quote = "'"
        out.append(c)
        i += 1
    return "".join(out)


def _lit(text: str) -> str:
    """The text with the stand-ins turned back into what was written."""
    return text.replace(QUOTED_DOLLAR, "$").replace(QUOTED_TICK, "`").replace(SUBST, "$(...)")


HOLE_RE = re.compile(r"\x02|\$\{[^}\n]*\}|\$\([^)\n]*\)|\$(?:[A-Za-z_]\w*|[0-9?#$!@*-])|`[^`\n]*`")


class Hole(str):
    """An unresolved expansion ($r, $(...)) as written; the text it stands for is unknown."""


def _parts(text: str) -> list:
    """Split text at unresolved expansions: literal strings, and a Hole for each expansion."""
    parts: list = []
    pos = 0
    for m in HOLE_RE.finditer(text):
        if m.start() > pos:
            parts.append(_lit(text[pos : m.start()]))
        parts.append(Hole(_lit(m.group(0))))
        pos = m.end()
    if pos < len(text):
        parts.append(_lit(text[pos:]))
    return parts


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


REDIRECT_OP_RE = re.compile(r"[<>&|]*[<>][<>&|]*")


def _strip_redirects(args: list[str]) -> list[str]:
    """Arguments without redirections (> f, 2>&1, &> f, < f)."""
    out = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
        elif REDIRECT_OP_RE.fullmatch(a):
            skip = True
        elif a.isdigit() and i + 1 < len(args) and REDIRECT_OP_RE.fullmatch(args[i + 1]):
            continue  # the fd in 2>&1
        else:
            out.append(a)
    return out


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "a": "\a", "b": "\b",
           "f": "\f", "v": "\v", "e": "\x1b", '"': '"'}


def _backslash_escapes(text: str) -> str:
    """echo -e / printf escapes; unknown ones stay as written, like bash."""
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(0)), text)


def _echo_content(args: list[str]) -> str | None:
    words = _strip_redirects(args)
    newline, escapes = True, False
    while words and re.fullmatch(r"-[neE]+", words[0]):
        for flag in words.pop(0)[1:]:
            if flag == "n":
                newline = False
            else:
                escapes = flag == "e"
    text = " ".join(words)
    if escapes:
        text = _backslash_escapes(text)
    return text + ("\n" if newline else "")


VAR_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
NO_TARGET = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
OPENERS = {"if", "while", "until", "for", "case", "select"}
CLOSERS = {"fi", "done", "esac"}
BRANCHES = {"then", "else", "elif", "do", "!"}
# Commands that practically never fail, so `cmd && write` after them still writes.
SAFE_GATES = {"", "cd", "pushd", "mkdir", "echo", "printf", "cat", "touch", "export", "true", ":"}
UNRESOLVED_RE = re.compile(r"\$[A-Za-z_{(0-9?#$!@*-]|`|\x02")


def _expand(text: str, env: dict[str, str]) -> str:
    """Substitute $VAR / ${VAR} for variables assigned earlier in the command."""
    return VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), text)


def _strip_keywords(words: list[str]) -> list[str]:
    i = 0
    while i < len(words) and words[i] in OPENERS | CLOSERS | BRANCHES:
        i += 1
    return words[i:]


def analyze_bash(
    command: str, cwd: str | None, env: dict[str, str] | None = None, succeeded: bool = False
) -> ShellEffects:
    """Static effects of a bash command. `succeeded`: the command exited 0, so its
    last `a && b && c` chain ran to the end."""
    writes: list[ShellWrite] = []
    scripts: list[InlineScript] = []
    deletes: list[str] = []
    copies: list[tuple[str, str, bool]] = []
    produced: list[tuple[str, str]] = []
    executed: list[tuple] = []
    made_dirs: set[str] = set()
    cur = cwd
    env = dict(env or {})

    def path(word: str) -> str:
        word = _expand(word, env)
        if word.startswith("~/") and env.get("HOME"):
            word = env["HOME"].rstrip("/") + word[1:]
        return resolve(_lit(word), cur)

    flat, bodies = _extract_heredocs(command)
    segs = _segments(_split_words(_newlines_to_semicolons(_protect(flat))))
    skip_seg = -1
    depth = 0          # inside if / loop / case bodies
    after_or = False   # later in a chain that had ||
    gated = False      # later in a chain, after `cmd &&` where cmd could have failed
    last_chain = max([0] + [i + 1 for i, (_, sp) in enumerate(segs[:-1]) if sp not in ("&&", "||", "|")])
    observed: list[int] = []
    stamped = 0
    for n, (words, sep) in enumerate(segs):
        for w in writes[stamped:]:
            w.seg = n - 1
        stamped = len(writes)
        prev_sep = segs[n - 1][1] if n else ""
        if prev_sep not in ("&&", "||", "|"):
            after_or = gated = False
        elif prev_sep == "||":
            after_or = True
        elif prev_sep == "&&" and _program(_strip_keywords(segs[n - 1][0]))[0] not in SAFE_GATES:
            gated = True
        if n == skip_seg:
            continue
        # Control keywords: strip them and remember that what follows may not run.
        header = False
        while words and words[0] in OPENERS | CLOSERS | BRANCHES:
            kw = words.pop(0)
            if kw in OPENERS:
                depth += 1
                header = kw in ("for", "case", "select")
            elif kw in CLOSERS:
                depth = max(0, depth - 1)
        if header:
            continue
        conditional = depth > 0 or after_or or (gated and not (succeeded and n >= last_chain))
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
                if conditional:  # value depends on which branch ran
                    env.pop(m.group(1), None)
                else:
                    env[m.group(1)] = _expand(m.group(2), env)
            continue

        words = [w if MARKER_RE.match(w) else _expand(w, env) for w in words]
        prog, args = _program(words)
        if prog in ("cd", "pushd") and args:
            cur = path(args[-1])
            continue
        for out_path in _flag_outputs(words):
            produced.append((path(out_path), f"output of {prog or 'command'} ({out_path})"))

        heredoc = next((int(m.group(1)) for w in words if (m := MARKER_RE.match(w))), None)
        target, append = _redirect_target(words)
        if target in NO_TARGET or (target and target.startswith("/dev/")):
            target = None
        if prog == "cat" and heredoc is None and target is None and sep != "|" and _plain_args(args):
            observed.append(n)
        lang = _interp_lang(prog)
        if heredoc is not None:
            body, expanded = bodies[heredoc]
            if prog == "tee":
                target, append = _tee_target(words)
            elif prog == "cat" and target is None and sep == "|" and n + 1 < len(segs):
                target, append = _tee_target(segs[n + 1][0])
                skip_seg = n + 1
            if prog in ("cat", "tee", "") and target and target not in NO_TARGET:
                parts = _parts(body) if expanded and UNRESOLVED_RE.search(body) else None
                writes.append(ShellWrite(path(target), body, append, expanded, "heredoc",
                                         conditional or parts is not None, parts, conditional))
                continue
            if lang:
                scripts.append(InlineScript(lang, body, prog, "heredoc"))
                argv = ["-"] + [_lit(_expand(a, env)) for a in _plain_args(args) if a != "-" and not MARKER_RE.match(a)]
                produced.extend(
                    (path(p), f"written by {prog} script") for p in script_outputs(body, lang, argv)
                )
            if target:
                produced.append((path(target), f"output of {prog}"))
            continue

        if lang and INLINE_FLAGS.get(lang) not in args:
            plain = [a for a in _plain_args(args) if a != "-"]
            if plain and "-m" not in args and not MARKER_RE.match(plain[0]):
                executed.append((path(plain[0]), lang, cur, [path(a) for a in plain]))
        elif not lang and prog and re.search(r"\.(py|sh|js)$", words[0]) and "=" not in words[0]:
            ext = words[0].rsplit(".", 1)[-1]
            script = path(words[0])
            executed.append((script, {"py": "py", "sh": "sh", "js": "js"}[ext], cur,
                             [script] + [path(a) for a in _plain_args(words[1:])]))
        if lang and INLINE_FLAGS.get(lang) in args:
            k = args.index(INLINE_FLAGS[lang])
            if k + 1 < len(args):
                code = _lit(args[k + 1]).rstrip("\n") + "\n"
                scripts.append(InlineScript(lang, code, prog, "inline"))
                argv = ["-c"] + [_lit(_expand(a, env)) for a in _plain_args(args[k + 2 :])]
                produced.extend((path(p), f"written by {prog} -c") for p in script_outputs(code, lang, argv))
        if closes_group and target:
            produced.append((path(target), "output of a command group"))
            continue
        if prog in ("echo", "printf") and target:
            content = _echo_content(args) if prog == "echo" else _printf_content(args)
            if content is not None:
                expanded = any("$" in a for a in args)
                content = _expand(content, env)
                parts = _parts(content) if UNRESOLVED_RE.search(content) else None
                writes.append(ShellWrite(path(target), _lit(content), append, expanded, prog,
                                         conditional or parts is not None, parts, conditional))
                continue
        if target:
            produced.append((path(target), f"output of {prog or 'command'}" + (" (appended)" if append else "")))
        if prog in ("rm", "unlink", "rmdir"):
            for a in _plain_args(args):
                deletes.append(path(a))
        elif prog == "git" and args[:1] == ["rm"]:
            for a in _plain_args(args[1:]):
                deletes.append(path(a))
        elif prog == "git" and args[:2] == ["worktree", "remove"]:
            for a in _plain_args(args[2:]):
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
        elif prog == "sed" and (sed := parse_in_place([_lit(a) for a in _strip_redirects(args)])):
            # A script that still has shell expansions in it can't be replayed.
            raw_script = " ".join(args)
            edit = None if UNRESOLVED_RE.search(raw_script) else sed.edit
            for f in sed.files:
                target = path(f)
                if edit is None:
                    produced.append((target, "edited in place by sed -i"))
                    continue
                writes.append(ShellWrite(target, "", how="sed -i", uncertain=conditional,
                                         conditional=conditional, transform=edit,
                                         backup=target + sed.backup_suffix if sed.backup_suffix else None))
        elif prog == "touch":
            for f in _plain_args(args):
                writes.append(ShellWrite(path(f), "", False, False, "touch", conditional, conditional=conditional))
    for w in writes[stamped:]:
        w.seg = len(segs) - 1
    return ShellEffects(writes, scripts, deletes, cur, copies, produced, executed, observed)


SCRIPT_OUTPUT_RES = {
    "py": [
        # open('x', 'w') / open("x", mode="a")
        re.compile(r"\bopen\(\s*[rf]?(['\"])([^'\"\n]+)\1\s*,\s*(?:mode\s*=\s*)?(['\"])[^'\"]*[wax][^'\"]*\3"),
        # Path('x').write_text(...)
        re.compile(r"\bPath\(\s*[rf]?(['\"])([^'\"\n]+)\1\s*\)\.(?:write_text|write_bytes|open\(\s*['\"][wa])"),
        # df.to_csv('x'), plt.savefig('x'), wb.save('x'), ...
        re.compile(r"\.(?:to_csv|to_json|to_excel|to_parquet|to_html|savefig|save|SaveFile|write_image|"
                   r"write_html|dump_to|screenshot|save_screenshot|imwrite|copyfile|copy2?|move)"
                   r"\(\s*(?:path\s*=\s*)?[rf]?(['\"])([^'\"\n]+)\1"),
        re.compile(r"\bshutil\.(?:copyfile|copy2?|move)\([^,\n]+,\s*[rf]?(['\"])([^'\"\n]+)\1"),
    ],
    "js": [
        re.compile(r"\b(?:writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream)\(\s*(['\"`])([^'\"`\n]+)\1"),
    ],
}


PY_CONST_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*=\s*(?:Path\()?\s*[rf]?(['\"])([^'\"\n]+)\2\s*\)?[ \t]*(?:#.*)?$", re.M)
PY_ARG = r"([A-Za-z_]\w*|sys\.argv\[\d+\])"
PY_NAME_OUTPUT_RES = [
    re.compile(r"\bopen\(\s*" + PY_ARG + r"\s*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax]"),
    re.compile(r"\.(?:to_csv|to_json|to_excel|to_parquet|to_html|savefig|save|SaveFile|write_image|write_html|"
               r"screenshot|save_screenshot|imwrite)\(\s*(?:path\s*=\s*)?" + PY_ARG + r"\s*[,)]"),
    re.compile(r"\bshutil\.(?:copyfile|copy2?|move)\([^,\n]+,\s*" + PY_ARG + r"\s*[,)]"),
    re.compile(r"\b([A-Za-z_]\w*)\.(?:write_text|write_bytes)\("),
    re.compile(r"\bPath\(\s*" + PY_ARG + r"\s*\)\.(?:write_text|write_bytes)"),
]
PY_ARGV_ASSIGN_RE = re.compile(r"^[ \t]*([A-Za-z_][\w, \t]*?)\s*=\s*(?:Path\()?\s*sys\.argv\[(\d+)?(:?)(\d*)\]", re.M)


def script_outputs(code: str, lang: str, argv: list[str] | None = None) -> list[str]:
    """File paths a script writes to (best effort: literals, constants, sys.argv)."""
    found: list[str] = []
    argv = argv or []

    def add(p: str | None) -> None:
        if p and p not in found and not p.startswith("/dev/") and "{" not in p:
            found.append(p)

    for rx in SCRIPT_OUTPUT_RES.get(lang, []):
        for m in rx.finditer(code):
            add(m.group(2))
    if lang == "py":
        consts = {m.group(1): m.group(3) for m in PY_CONST_RE.finditer(code)}
        for i, a in enumerate(argv):
            consts[f"sys.argv[{i}]"] = a
        # dst = sys.argv[2]  /  src, dst = sys.argv[1:3]  /  src, dst = sys.argv[1:]
        for m in PY_ARGV_ASSIGN_RE.finditer(code):
            names = [n.strip() for n in m.group(1).split(",") if n.strip()]
            start = int(m.group(2) or 0)
            values = argv[start:] if m.group(3) else argv[start : start + 1]
            for name, value in zip(names, values):
                consts[name] = value
        for rx in PY_NAME_OUTPUT_RES:
            for m in rx.finditer(code):
                value = consts.get(m.group(1))
                if value and ("/" in value or "\\" in value or "." in value):
                    add(value)
    return found


# Flags that name a file a program writes. --path only counts for commands
# that look like they save something (screenshot --path x.png).
OUTPUT_FLAGS = {"-o", "--output", "--out", "--outfile", "--output-file", "--out-file", "--save", "--save-to",
                "--save-path", "--screenshot", "--dest", "--destination", "--report", "--junitxml", "--log-file"}
SAVE_WORDS = re.compile(r"screenshot|save|export|dump|write|snapshot|capture|render", re.I)


def _flag_outputs(words: list[str]) -> list[str]:
    out = []
    saving = any(SAVE_WORDS.search(w) for w in words if not w.startswith("-"))
    for i, w in enumerate(words):
        flag, eq, value = w.partition("=")
        if flag in OUTPUT_FLAGS or (flag == "--path" and saving):
            if not eq:
                value = words[i + 1] if i + 1 < len(words) else ""
            if value and not value.startswith("-") and not re.fullmatch(r"[<>&|]+", value) \
                    and "." in value.rsplit("/", 1)[-1]:
                out.append(value)
    return out


def _plain_args(args: list[str]) -> list[str]:
    """Positional arguments, without options or redirections."""
    out = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
        elif re.fullmatch(r"[<>&|]+", a):
            skip = True  # >, >>, <, >&, &> and the target that follows
        elif a.isdigit() and i + 1 < len(args) and re.fullmatch(r"[<>&]+", args[i + 1]):
            continue  # the fd in 2>&1
        elif not a.startswith("-"):
            out.append(a)
    return out


def _printf_content(args: list[str]) -> str | None:
    """printf with %s, %b, %d, %i and %%; the format repeats while arguments remain."""
    words = _strip_redirects(args)
    if words[:1] == ["--"]:
        words = words[1:]
    if not words or words[0].startswith("-"):
        return None  # printf -v VAR writes no output
    fmt, rest = words[0], words[1:]
    pieces = re.split(r"(%[%sbdi])", fmt)
    if any("%" in p for p in pieces[::2]):
        return None  # widths, precision or other conversions we don't emulate
    takes_args = any(p != "%%" for p in pieces[1::2])
    out = []
    while True:
        for i, p in enumerate(pieces):
            if i % 2 == 0:
                out.append(_backslash_escapes(p))
            elif p == "%%":
                out.append("%")
            else:
                arg = rest.pop(0) if rest else ("0" if p in ("%d", "%i") else "")
                if p in ("%d", "%i") and not re.fullmatch(r"[+-]?\d+", arg):
                    return None
                out.append(_backslash_escapes(arg) if p == "%b" else arg)
        if not rest or not takes_args:
            return "".join(out)


PS_HERESTRING_RE = re.compile(r"@(['\"])\r?\n(.*?)\r?\n\1@", re.S)
PS_WRITE_RE = re.compile(
    r"(Set-Content|Add-Content|Out-File)\b(?P<args>[^\n|;]*)", re.I,
)


PS_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][\w:]*)\}|(env:[A-Za-z_]\w*|[A-Za-z_]\w*))")
PS_ASSIGN_RE = re.compile(r"^\s*\$([A-Za-z_]\w*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')\s*$")
PS_WRITERS = {"set-content": False, "add-content": True, "out-file": False, "tee-object": False}
PS_COPIERS = {"copy-item": False, "cp": False, "copy": False, "cpi": False,
              "move-item": True, "mv": True, "move": True, "mi": True}
PS_REMOVERS = {"remove-item", "rm", "del", "ri", "erase", "rmdir", "rd"}


def _ps_split(text: str) -> list[str]:
    """Split on ; and newlines outside quotes."""
    out, cur, quote = [], [], None
    for c in text:
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c in ";\n":
            out.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    out.append("".join(cur))
    return [s for s in out if s.strip()]


def _ps_words(text: str) -> list[str]:
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="<>|")
        lex.whitespace_split = True
        lex.escape = ""  # backslashes are path separators in PowerShell
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return text.split()


def _ps_param(args: list[str], *names: str) -> str | None:
    lowered = [a.lower() for a in args]
    for n in names:
        if n in lowered:
            i = lowered.index(n)
            if i + 1 < len(args):
                return args[i + 1]
    return None


def _ps_positional(args: list[str]) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a.lower() in ("-encoding", "-value", "-itemtype", "-filter", "-include", "-exclude", "-pattern")
        elif not re.fullmatch(r"[<>|*0-9&]+", a):
            out.append(a)
    return out


def analyze_powershell(command: str, cwd: str | None, env: dict[str, str] | None = None) -> ShellEffects:
    writes: list[ShellWrite] = []
    scripts: list[InlineScript] = []
    deletes: list[str] = []
    copies: list[tuple[str, str, bool]] = []
    produced: list[tuple[str, str]] = []
    executed: list[tuple] = []
    cur = cwd
    env = dict(env or {})

    strings: list[tuple[str, bool]] = []

    def stash(m: re.Match) -> str:
        strings.append((m.group(2) + "\n", m.group(1) == '"'))
        return f" __HERESTRING_{len(strings) - 1}__ "

    def expand(text: str) -> str:
        return PS_VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), text)

    def path(word: str) -> str:
        return resolve(expand(word), cur)

    observed: list[int] = []
    stamped = 0
    flat = PS_HERESTRING_RE.sub(stash, command)
    for n, stmt in enumerate(_ps_split(flat)):
        for w in writes[stamped:]:
            w.seg = n - 1
        stamped = len(writes)
        m = PS_ASSIGN_RE.match(stmt)
        if m:
            value = m.group(2) if m.group(2) is not None else m.group(3)
            env[m.group(1)] = expand(value) if m.group(2) is not None else value
            continue
        pipeline = [p for p in re.split(r"\|(?![|])", stmt)]
        for k, part in enumerate(pipeline):
            words = _ps_words(part)
            if words and words[0] == "&":
                words = words[1:]
            if not words:
                continue
            words = [expand(w) if not w.startswith("__HERESTRING_") else w for w in words]
            prog = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
            if prog.endswith(".exe"):
                prog = prog[:-4]
            args = words[1:]
            hs = next((int(x.group(1)) for w in words if (x := re.match(r"__HERESTRING_(\d+)__", w))), None)
            prev_hs = None
            if k > 0:
                prev = re.search(r"__HERESTRING_(\d+)__", pipeline[k - 1])
                prev_hs = int(prev.group(1)) if prev else None

            if prog in ("get-content", "gc", "cat", "type") and k == len(pipeline) - 1 and _ps_positional(args):
                observed.append(n)
            if prog in ("cd", "set-location", "sl", "push-location", "pushd") and args:
                cur = path(_ps_positional(args)[-1] if _ps_positional(args) else args[-1])
                continue
            if prog in PS_WRITERS:
                target = _ps_param(args, "-path", "-filepath", "-literalpath") or next(iter(_ps_positional(args)), None)
                if target:
                    src = hs if hs is not None else prev_hs
                    if src is not None:
                        body, expanded = strings[src]
                        writes.append(ShellWrite(path(target), body, PS_WRITERS[prog], expanded, words[0]))
                    else:
                        produced.append((path(target), f"output of {words[0]}"))
                continue
            if prog in PS_COPIERS:
                src = _ps_param(args, "-path", "-literalpath")
                dst = _ps_param(args, "-destination")
                pos = _ps_positional(args)
                src = src or (pos[0] if pos else None)
                dst = dst or (pos[1] if len(pos) > 1 else None)
                if src and dst:
                    copies.append((path(src), path(dst), PS_COPIERS[prog]))
                continue
            if prog in PS_REMOVERS:
                target = _ps_param(args, "-path", "-literalpath") or next(iter(_ps_positional(args)), None)
                if target:
                    deletes.append(path(target))
                continue
            lang = _interp_lang(prog)
            if lang:
                flag = INLINE_FLAGS.get(lang)
                if "-" in args and prev_hs is not None:
                    scripts.append(InlineScript(lang, strings[prev_hs][0], prog, "herestring"))
                    produced.extend((path(p), f"written by {prog} script") for p in script_outputs(strings[prev_hs][0], lang))
                elif flag in args:
                    i = args.index(flag)
                    if i + 1 < len(args):
                        code = args[i + 1].rstrip("\n") + "\n"
                        scripts.append(InlineScript(lang, code, prog, "inline"))
                        produced.extend((path(p), f"written by {prog} -c") for p in script_outputs(code, lang))
                elif "-m" not in args:
                    pos = _ps_positional(args)
                    if pos:
                        executed.append((path(pos[0]), lang, cur, [path(a) for a in pos]))
            for out_path in _flag_outputs(words):
                produced.append((path(out_path), f"output of {prog} ({out_path})"))
            # Redirects: > file, >> file, *> file, 2> file
            for i, w in enumerate(words):
                if w in (">", ">>") and i + 1 < len(words):
                    tgt = words[i + 1]
                    if tgt.lower() not in ("$null", "nul") and not tgt.startswith("&"):
                        produced.append((path(tgt), f"output of {prog}" + (" (appended)" if w == ">>" else "")))
    for w in writes[stamped:]:
        w.seg = n
    return ShellEffects(writes, scripts, deletes, cur, copies, produced, executed, observed)
