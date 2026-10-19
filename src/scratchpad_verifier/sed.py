"""A small sed interpreter, enough to replay ``sed -i`` edits from a transcript.

It supports the commands agents use for in-place edits: ``s`` (any delimiter,
flags g, N, I and p), ``d``, ``p`` and GNU one-liner ``a``/``i``/``c``, with
line-number, ``$`` and ``/regex/`` addresses, ranges and ``!``. Regexes are
POSIX basic (or extended with -E/-r) and translated to Python's syntax.
Anything else returns None, so callers can fall back to "content unknown".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# POSIX character classes, as Python set members.
CLASSES = {
    "alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9", "upper": "A-Z", "lower": "a-z",
    "space": r"\s", "blank": r" \t", "punct": r"!-/:-@\[-`{-~", "xdigit": "0-9A-Fa-f",
    "word": r"\w", "cntrl": r"\x00-\x1f\x7f", "print": r"\x20-\x7e", "graph": r"\x21-\x7e",
}


class Unsupported(Exception):
    pass


def translate(pattern: str, extended: bool) -> str:
    """POSIX BRE/ERE (with GNU extensions) to a Python regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            n = pattern[i + 1]
            i += 2
            if n in "(){}+?|" and not extended:
                out.append(n)
            elif n == "n":
                out.append(r"\n")
            elif n == "t":
                out.append(r"\t")
            elif n in "<>":
                out.append(r"\b")
            elif n == "`":
                out.append(r"\A")
            elif n == "'":
                out.append(r"\Z")
            elif n.isdigit() or n in "wWsSbB":
                out.append("\\" + n)
            else:
                out.append(re.escape(n))
            continue
        if c == "[":
            j = i + 1
            negate = ""
            if pattern[j : j + 1] == "^":
                negate = "^"
                j += 1
            members: list[str] = []
            if pattern[j : j + 1] == "]":  # a leading ] is literal
                members.append(r"\]")
                j += 1
            while j < len(pattern) and pattern[j] != "]":
                if pattern.startswith("[:", j):
                    end = pattern.find(":]", j)
                    if end == -1 or pattern[j + 2 : end] not in CLASSES:
                        raise Unsupported(f"bracket expression in {pattern!r}")
                    members.append(CLASSES[pattern[j + 2 : end]])
                    j = end + 2
                else:
                    ch = pattern[j]
                    members.append("\\" + ch if ch in "\\[" else ch)
                    j += 1
            if j >= len(pattern):
                raise Unsupported(f"unterminated [ in {pattern!r}")
            out.append("[" + negate + "".join(members) + "]")
            i = j + 1
            continue
        if c in "(){}+?|" and not extended:
            out.append("\\" + c)
        elif c == "*" and (not out or out[-1] in ("^", "(", r"\(")):
            out.append(r"\*")  # a leading * is literal in BRE
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _replacement(text: str) -> list:
    """s/// replacement: literal strings, and ints for \\N and & group references."""
    parts: list = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            if n.isdigit():
                parts.append(int(n))
            elif n in "ULulE":
                raise Unsupported("case conversion in replacement")
            else:
                parts.append({"n": "\n", "t": "\t"}.get(n, n))
            i += 2
            continue
        parts.append(0 if c == "&" else c)
        i += 1
    return parts


@dataclass
class Address:
    kind: str           # "line", "last" or "regex"
    value: object = None

    def hit(self, lineno: int, line: str, last: bool) -> bool:
        if self.kind == "line":
            return lineno == self.value
        if self.kind == "last":
            return last
        return self.value.search(line) is not None


@dataclass
class Command:
    name: str
    start: Address | None = None
    end: Address | None = None
    negate: bool = False
    regex: re.Pattern | None = None
    replacement: list = field(default_factory=list)
    nth: int = 1
    every: bool = False
    print_: bool = False
    text: str = ""
    active: bool = False

    def selects(self, lineno: int, line: str, last: bool) -> bool:
        if self.start is None:
            hit = True
        elif self.end is None:
            hit = self.start.hit(lineno, line, last)
        elif self.active:
            hit = True
            if self.end.kind == "line":
                self.active = lineno < self.end.value
            else:
                self.active = not self.end.hit(lineno, line, last)
        else:
            hit = self.start.hit(lineno, line, last)
            if hit:
                if self.end.kind == "line":
                    self.active = lineno < self.end.value
                else:
                    self.active = not (self.end.kind == "last" and last)
        return hit != self.negate


class _Parser:
    def __init__(self, script: str, extended: bool):
        self.s = script
        self.i = 0
        self.extended = extended

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def compile(self, pattern: str, flags: int = 0) -> re.Pattern:
        if pattern == "":
            raise Unsupported("empty regex (reuses the last one)")
        try:
            return re.compile(translate(pattern, self.extended), flags)
        except re.error as e:
            raise Unsupported(str(e)) from None

    def delimited(self, delim: str, in_regex: bool) -> str:
        """Text up to the next unescaped delim; an escaped delim is that literal character."""
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < len(self.s):
                n = self.s[self.i + 1]
                if n == delim and not in_regex:
                    out.append(n)
                elif n == delim:
                    # Literal delimiter; a bracket keeps \| or \( from turning special.
                    out.append("\\" + n if n in "]^" else f"[{n}]")
                elif n == "\n" and not in_regex:
                    out.append("\n")
                else:
                    out.append(c + n)
                self.i += 2
                continue
            if c == delim:
                self.i += 1
                return "".join(out)
            if c == "\n" and in_regex:
                break
            out.append(c)
            self.i += 1
        raise Unsupported("unterminated s command or address")

    def address(self) -> Address | None:
        c = self.peek()
        if c.isdigit():
            m = re.compile(r"\d+").match(self.s, self.i)
            self.i = m.end()
            return Address("line", int(m.group()))
        if c == "$":
            self.i += 1
            return Address("last")
        if c in ("/", "\\"):
            if c == "\\":
                self.i += 1
            delim = self.s[self.i]
            self.i += 1
            pattern = self.delimited(delim, True)
            flags = 0
            if self.peek() == "I":
                self.i += 1
                flags = re.I
            return Address("regex", self.compile(pattern, flags))
        return None

    def text_argument(self) -> str:
        """GNU a/i/c text: `a text` or `a\\` + newline + text, to the end of the line."""
        while self.peek() in (" ", "\t"):
            self.i += 1
        if self.peek() == "\\":
            self.i += 1
            if self.peek() == "\n":
                self.i += 1
        out = []
        while self.i < len(self.s) and self.s[self.i] != "\n":
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < len(self.s):
                out.append(self.s[self.i + 1])
                self.i += 2
                continue
            out.append(c)
            self.i += 1
        return "".join(out)

    def commands(self) -> list[Command]:
        cmds: list[Command] = []
        while True:
            while self.peek() and self.peek() in " \t\n;":
                self.i += 1
            if not self.peek():
                return cmds
            start = self.address()
            end = None
            if start is not None and self.peek() == ",":
                self.i += 1
                end = self.address()
                if end is None:
                    raise Unsupported("range end")
            while self.peek() in (" ", "\t"):
                self.i += 1
            negate = self.peek() == "!"
            if negate:
                self.i += 1
                while self.peek() in (" ", "\t"):
                    self.i += 1
            name = self.peek()
            self.i += 1
            cmd = Command(name, start, end, negate)
            if name == "s":
                delim = self.peek()
                if not delim or delim in "\n\\":
                    raise Unsupported("s delimiter")
                self.i += 1
                pattern = self.delimited(delim, True)
                cmd.replacement = _replacement(self.delimited(delim, False))
                flags = 0
                while self.peek() and self.peek() not in " \t\n;}":
                    f = self.peek()
                    if f == "g":
                        cmd.every = True
                    elif f == "p":
                        cmd.print_ = True
                    elif f in "iI":
                        flags |= re.I
                    elif f.isdigit():
                        m = re.compile(r"\d+").match(self.s, self.i)
                        cmd.nth = int(m.group())
                        self.i = m.end()
                        continue
                    else:
                        raise Unsupported(f"s flag {f!r}")
                    self.i += 1
                cmd.regex = self.compile(pattern, flags)
                groups = cmd.regex.groups
                if any(isinstance(p, int) and p > groups for p in cmd.replacement):
                    raise Unsupported("reference to a missing group")
            elif name in ("a", "i", "c"):
                cmd.text = self.text_argument()
            elif name not in ("d", "p"):
                raise Unsupported(f"command {name!r}")
            cmds.append(cmd)


def _substitute(cmd: Command, line: str) -> tuple[str, bool]:
    count = 0
    replaced = False

    def repl(m: re.Match) -> str:
        nonlocal count, replaced
        count += 1
        if count < cmd.nth or (count > cmd.nth and not cmd.every):
            return m.group(0)
        replaced = True
        return "".join(p if isinstance(p, str) else (m.group(p) or "") for p in cmd.replacement)

    return cmd.regex.sub(repl, line), replaced


def run(commands: list[Command], content: str) -> str:
    lines = content.split("\n")
    trailing = content.endswith("\n")
    if trailing or content == "":
        lines.pop()
    out: list[str] = []
    for n, line in enumerate(lines, 1):
        last = n == len(lines)
        before: list[str] = []
        after: list[str] = []
        deleted = False
        for cmd in commands:
            if not cmd.selects(n, line, last):
                continue
            if cmd.name == "s":
                line, replaced = _substitute(cmd, line)
                if replaced and cmd.print_:
                    out.append(line + "\n")
            elif cmd.name == "p":
                out.append(line + "\n")
            elif cmd.name == "a":
                after.append(cmd.text + "\n")
            elif cmd.name == "i":
                out.append(cmd.text + "\n")
            elif cmd.name in ("d", "c"):
                if cmd.name == "c" and not (cmd.end is not None and cmd.active):
                    before.append(cmd.text + "\n")  # with a range, once at its end
                deleted = True
                break
        out.extend(before)
        if not deleted:
            out.append(line + "\n")
        out.extend(after)
    text = "".join(out)
    if not trailing and text.endswith("\n") and lines and not any(c.name in "aic" for c in commands):
        text = text[:-1]  # sed keeps a missing final newline
    return text


@dataclass
class InPlaceEdit:
    edit: Callable[[str], str] | None   # None: a script we can't replay
    files: list[str]
    backup_suffix: str = ""


def parse_in_place(args: list[str]) -> InPlaceEdit | None:
    """Parse `sed` arguments; None unless it edits files in place (-i)."""
    scripts: list[str] = []
    positional: list[str] = []
    in_place = False
    suffix = ""
    extended = False
    supported = True
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            positional.extend(args[i:])
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name == "--in-place":
                in_place, suffix = True, value
            elif name in ("--regexp-extended",):
                extended = True
            elif name == "--expression":
                if not eq and i < len(args):
                    value = args[i]
                    i += 1
                scripts.append(value)
            elif name not in ("--posix", "--separate", "--unbuffered", "--follow-symlinks"):
                supported = False
            continue
        if a.startswith("-") and len(a) > 1:
            for k, flag in enumerate(a[1:], 1):
                if flag == "i":
                    in_place, suffix = True, a[k + 1 :]
                    if not suffix and i < len(args) and args[i] == "":
                        i += 1  # BSD sed -i '' (no backup)
                    break
                if flag == "e":
                    rest = a[k + 1 :]
                    if not rest and i < len(args):
                        rest = args[i]
                        i += 1
                    scripts.append(rest)
                    break
                if flag in "Er":
                    extended = True
                elif flag not in "su":
                    supported = False  # -n, -z, -f, ...
            continue
        positional.append(a)
    if not in_place:
        return None
    if not scripts and positional:
        scripts.append(positional.pop(0))
    edit = None
    if supported and scripts:
        try:
            commands = _Parser("\n".join(scripts), extended).commands()
        except Unsupported:
            commands = None
        if commands is not None:
            def edit(content: str, script: str = "\n".join(scripts)) -> str:
                # Fresh commands each time: ranges keep state while running.
                return run(_Parser(script, extended).commands(), content)
    return InPlaceEdit(edit, positional, suffix)
