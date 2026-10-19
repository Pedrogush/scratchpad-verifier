"""Parse Claude Code .jsonl transcripts and replay everything the agent wrote or ran."""

from __future__ import annotations

import fnmatch
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from .shell import InlineScript, analyze_bash, analyze_powershell, resolve, script_outputs

DEFAULT_PREFIXES = ("/tmp/",)
DEFAULT_MARKERS = ("/scratchpad/",)
FILE_TOOLS = {"Write", "Edit", "MultiEdit"}
SHELL_TOOLS = {"Bash": "sh", "PowerShell": "ps1"}

# Temp directories on other systems, matched against "/"-normalized paths.
_DRIVE = r"(?:[A-Za-z]:|/mnt/[A-Za-z]|/[A-Za-z])"
TEMP_RE = re.compile(
    r"^(?:/tmp/|/var/tmp/|(?:/private)?/var/folders/[^/]+/[^/]+/T/"
    rf"|{_DRIVE}/(?:Temp|tmp)/|{_DRIVE}/Users/[^/]+/AppData/Local/Temp/"
    rf"|{_DRIVE}/Windows/Temp/|\$\{{?(?:TMPDIR|TMP|TEMP)\}}?/)",
    re.I,
)
# Tool results meaning the command never ran.
NOT_RUN_RE = re.compile(
    r"doesn't want to proceed|has been denied|was rejected|permission to use .* denied"
    r"|interrupted by user|blocked by|ENAMETOOLONG|E2BIG|argument list too long",
    re.I,
)


@dataclass
class Operation:
    """One tool call, matched with its result."""

    tool: str
    path: str
    tool_use_id: str
    input: dict
    timestamp: str | None = None
    source: str = ""
    cwd: str | None = None
    is_error: bool = False
    error: str | None = None
    result: dict | None = None
    output: str = ""          # text of the tool result

    @property
    def ran(self) -> bool:
        """False when the call was denied or interrupted before running."""
        return not (self.is_error and self.error and NOT_RUN_RE.search(self.error))


@dataclass
class Version:
    content: str
    how: str                  # Write, Edit, MultiEdit, heredoc, echo, Set-Content, ...
    op: Operation
    note: str = ""


@dataclass
class FileState:
    """Reconstructed history of one file."""

    path: str
    content: str | None = None
    operations: list[Operation] = field(default_factory=list)
    history: list[Version] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    created: bool = False     # first known write created it (vs. editing an existing file)
    deleted: bool = False     # removed with rm / del / Remove-Item at some point
    category: str = "other"   # scratchpad, tmp, ran_from_root or other

    @property
    def versions(self) -> list[str]:
        return [v.content for v in self.history]

    @property
    def errored(self) -> list[Operation]:
        return [o for o in self.operations if o.is_error]

    @property
    def complete(self) -> bool:
        return self.content is not None and not self.problems


@dataclass
class ScriptRun:
    """Code the agent ran inline (python - <<EOF, node -e, ...)."""

    script: InlineScript
    op: Operation
    command_index: int


@dataclass
class Produced:
    path: str
    how: list[str] = field(default_factory=list)
    ops: list[Operation] = field(default_factory=list)
    deleted: bool = False
    category: str = "other"


@dataclass
class Session:
    session_id: str
    root: str | None                       # working directory the session started in
    calls: list[Operation]
    files: dict[str, FileState]
    commands: list[Operation]              # every Bash / PowerShell call, in order
    scripts: list[ScriptRun]
    # Files commands wrote whose content isn't in the transcript.
    produced: dict[str, "Produced"] = field(default_factory=dict)

    def errored_calls(self) -> list[Operation]:
        return [o for o in self.calls if o.is_error]


# --------------------------------------------------------------------------
# Reading transcripts


def iter_records(path: Path) -> Iterator[dict]:
    """Yield JSON objects from a .jsonl file, skipping blank or broken lines."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield _normalize_record(rec)


def _normalize_record(rec: dict) -> dict:
    """Accept SDK/stream-json events too: {"assistant": {"internal_anthropic_catchall": {...}}}."""
    if "type" not in rec:
        for kind in ("assistant", "user"):
            inner = rec.get(kind)
            if isinstance(inner, dict):
                body = inner.get("internal_anthropic_catchall", inner)
                if isinstance(body, dict) and "message" in body:
                    rec = dict(body, type=kind)
                    rec.setdefault("timestamp", rec.get("created_at"))
                break
    if "toolUseResult" not in rec and "tool_use_result" in rec:
        rec["toolUseResult"] = rec["tool_use_result"]
    return rec


def transcript_files(path: Path, include_subagents: bool = True) -> list[Path]:
    """The main transcript plus any subagent transcripts stored beside it."""
    files = [path]
    if include_subagents:
        sub_dir = path.with_suffix("") / "subagents"
        if sub_dir.is_dir():
            files.extend(sorted(sub_dir.glob("*.jsonl")))
    return files


def _content_blocks(rec: dict) -> list:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _clean_error(text: str) -> str:
    text = text.strip()
    if text.startswith("<tool_use_error>") and text.endswith("</tool_use_error>"):
        text = text[len("<tool_use_error>") : -len("</tool_use_error>")]
    return text


def collect_calls(files: Iterable[Path]) -> list[Operation]:
    """Collect every tool call, matched with its result, in time order.

    ``path`` is the ``file_path`` (or ``notebook_path``) input when present.
    """
    ops: dict[str, Operation] = {}
    order: list[str] = []
    for file in files:
        for rec in iter_records(file):
            rtype = rec.get("type")
            if rtype == "assistant":
                for block in _content_blocks(rec):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    if not isinstance(inp, dict):
                        inp = {"input": inp}
                    path = inp.get("file_path") or inp.get("notebook_path")
                    tid = block.get("id") or f"anon-{len(order)}"
                    if tid in ops:
                        continue
                    ops[tid] = Operation(
                        tool=str(block.get("name")),
                        path=path if isinstance(path, str) else "",
                        tool_use_id=tid,
                        input=inp,
                        timestamp=rec.get("timestamp"),
                        source=str(file),
                        cwd=rec.get("cwd"),
                    )
                    order.append(tid)
            elif rtype == "user":
                tur = rec.get("toolUseResult")
                for block in _content_blocks(rec):
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    op = ops.get(block.get("tool_use_id"))
                    if op is None:
                        continue
                    if op.cwd is None:
                        op.cwd = rec.get("cwd")
                    op.is_error = bool(block.get("is_error"))
                    op.output = _result_text(block.get("content"))
                    if op.is_error:
                        op.error = _clean_error(op.output)
                    if isinstance(tur, dict):
                        op.result = tur
    result = [ops[t] for t in order]
    # Stable sort keeps file order for records without timestamps.
    result.sort(key=lambda o: o.timestamp or "")
    return result


def collect_operations(files: Iterable[Path]) -> list[Operation]:
    """Only the Write/Edit/MultiEdit calls."""
    return [o for o in collect_calls(files) if o.tool in FILE_TOOLS and o.path]


def session_root(files: Iterable[Path]) -> str | None:
    """The cwd of the first record that has one: where the session started."""
    for file in files:
        for rec in iter_records(file):
            if isinstance(rec.get("cwd"), str):
                return rec["cwd"]
        break
    return None


# --------------------------------------------------------------------------
# Replaying


def _apply_edit(content: str, old: str, new: str, replace_all: bool) -> str | None:
    if old == "":
        return new if content == "" else None
    if old not in content:
        return None
    return content.replace(old, new) if replace_all else content.replace(old, new, 1)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _apply_file_tool(st: FileState, op: Operation, base: str | None = None) -> None:
    if op.tool == "Write":
        if not st.history:
            kind = (op.result or {}).get("type")
            st.created = kind != "update" and (op.result or {}).get("originalFile") is None
        st.content = op.input.get("content", "")
        st.problems.clear()
        st.history.append(Version(st.content, "Write", op))
        return

    if st.content is None:
        original = (op.result or {}).get("originalFile")
        if not isinstance(original, str):
            original = base  # content seen by an earlier full Read
        if isinstance(original, str):
            st.content = original
            st.history.append(Version(original, "original", op, "content before the first edit"))
        else:
            st.problems.append(f"{op.tool} before any Write and no original content recorded")
            return

    edits = [op.input] if op.tool == "Edit" else (op.input.get("edits") or [])
    content = st.content
    for edit in edits:
        content = _apply_edit(
            content,
            edit.get("old_string", ""),
            edit.get("new_string", ""),
            bool(edit.get("replace_all")),
        )
        if content is None:
            st.problems.append(f"{op.tool} old_string not found ({op.tool_use_id})")
            return
    st.content = content
    st.history.append(Version(content, op.tool, op))


def _mark_deleted(files: dict[str, FileState], target: str) -> None:
    t = _norm(target).rstrip("/")
    glob = any(c in t for c in "*?[")
    for path, st in files.items():
        p = _norm(path)
        if p == t or p.startswith(t + "/") or (glob and fnmatch.fnmatch(p, t)):
            st.deleted = True


def replay(ops: Iterable[Operation]) -> dict[str, FileState]:
    """Replay Write/Edit/MultiEdit operations per file (no shell analysis)."""
    states: dict[str, FileState] = {}
    for op in ops:
        st = states.setdefault(op.path, FileState(path=op.path))
        st.operations.append(op)
        if not op.is_error:
            _apply_file_tool(st, op)
    return states


def _guess_home(root: str | None, calls: list[Operation]) -> str | None:
    """The user's home directory, from the session cwd or paths the agent used."""
    rx = re.compile(r"^((?:[A-Za-z]:|/mnt/[a-z]|/[a-z])/Users/[^/]+|/home/[^/]+|/Users/[^/]+|/root)(?:/|$)")
    candidates = [root or ""] + [o.path for o in calls if o.path]
    for c in candidates:
        m = rx.match(c.replace("\\", "/"))
        if m:
            return m.group(1)
    return None


def _full_read(op: Operation) -> str | None:
    """File content from a Read result that covered the whole file."""
    r = op.result or {}
    f = r.get("file") if r.get("type") == "text" else None
    if not isinstance(f, dict) or not isinstance(f.get("content"), str):
        return None
    if (f.get("startLine") or 1) == 1 and f.get("numLines") == f.get("totalLines"):
        return f["content"]
    return None


_PATH_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-~+@:/\\")


def _scratchpad_mentions(text: str) -> list[str]:
    """Absolute .../scratchpad/... file paths written in a block of text."""
    found: list[str] = []
    i = text.find("scratchpad")
    while i != -1:
        end = i + len("scratchpad")
        if i > 0 and text[i - 1] in "/\\" and end < len(text) and text[end] in "/\\":
            a = i
            while a > 0 and text[a - 1] in _PATH_CHARS:
                a -= 1
            b = end
            while b < len(text) and text[b] in _PATH_CHARS and text[b] != ":":
                b += 1
            path = text[a:b].rstrip(".:,")
            last = path.replace("\\", "/").rsplit("/", 1)[-1]
            absolute = path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path)
            if absolute and "." in last.strip(".") and ":" not in path[2:] and path not in found:
                found.append(path)
        i = text.find("scratchpad", end)
    return found


def _is_windows_root(root: str | None) -> bool:
    return bool(root) and bool(re.match(r"^[A-Za-z]:[\\/]", root))


def canonical(path: str, windows: bool) -> str:
    """One spelling per file: forward slashes, and on Windows C:/ for git-bash /c/."""
    p = path.replace("\\", "/")
    if windows:
        m = re.match(r"^/(?:mnt/)?([A-Za-z])(/.*)?$", p)
        if m:
            p = f"{m.group(1).upper()}:{m.group(2) or '/'}"
        elif re.match(r"^[a-z]:/", p):
            p = p[0].upper() + p[1:]
    while "//" in p[1:]:
        p = p[0] + p[1:].replace("//", "/")
    return p


def _is_dir_target(dst: str, files: dict) -> bool:
    if dst.endswith("/") or dst in (".", "..") or dst.endswith("/."):
        return True
    prefix = dst.rstrip("/") + "/"
    return any(k.startswith(prefix) for k in files)


def analyze(transcript: Path, include_subagents: bool = True) -> Session:
    """Replay file tools and shell commands from a transcript, in order."""
    tfiles = transcript_files(transcript, include_subagents)
    calls = collect_calls(tfiles)
    root = session_root(tfiles)
    windows = _is_windows_root(root) or any(re.match(r"^[A-Za-z]:\\", o.path) for o in calls if o.path)
    if root:
        root = canonical(root, windows)
    files: dict[str, FileState] = {}
    produced: dict[str, Produced] = {}
    commands: list[Operation] = []
    scripts: list[ScriptRun] = []
    reads: dict[str, str] = {}
    home = _guess_home(root, calls)
    own_scratchpad = f"/{transcript.stem}/scratchpad/"
    env = {"HOME": home} if home else {}

    def state(path: str) -> FileState:
        key = canonical(path, windows)
        return files.setdefault(key, FileState(path=key))

    def mark_produced(path: str, how: str, op: Operation) -> None:
        key = canonical(path, windows)
        pr = produced.setdefault(key, Produced(path=key))
        pr.how.append(how)
        pr.ops.append(op)
        if key in files:
            files[key].problems.append(f"later changed by a command ({how}); final content unknown")

    for op in calls:
        if op.tool == "Read" and op.path and not op.is_error:
            content = _full_read(op)
            if content is None:
                continue
            key = canonical(op.path, windows)
            reads[key] = content
            st = files.get(key)
            # A full Read shows the real content: if a command changed the file
            # in a way we couldn't follow, record what the agent saw.
            if st is not None and st.history and st.content != content:
                st.content = content
                st.problems.clear()
                st.history.append(Version(content, "Read", op, "content observed by Read"))
            continue

        if op.tool in FILE_TOOLS and op.path:
            st = state(op.path)
            st.operations.append(op)
            if not op.is_error:
                if st.deleted and op.tool == "Write":
                    st.created = True
                _apply_file_tool(st, op, reads.get(st.path))
            continue

        if op.tool not in SHELL_TOOLS:
            continue
        commands.append(op)
        command = op.input.get("command")
        if not isinstance(command, str) or not op.ran:
            continue
        analyzer = analyze_powershell if op.tool == "PowerShell" else analyze_bash
        try:
            if analyzer is analyze_bash:
                effects = analyzer(command, op.cwd or root, env, succeeded=not op.is_error)
            else:
                effects = analyzer(command, op.cwd or root, env)
        except Exception:  # never let one odd command break the whole replay
            continue
        for w in effects.writes:
            st = state(w.path)
            st.operations.append(op)
            note = "shell expansion not applied" if w.expanded else ""
            if w.append:
                if st.content is None and len(st.operations) == 1 and own_scratchpad in st.path:
                    # The session's own scratchpad starts empty, so a first append creates the file.
                    st.created = True
                    st.content = ""
                if st.content is None:
                    st.problems.append("appended to a file whose earlier content is unknown")
                    st.content = ""
                    note = (note + "; " if note else "") + "earlier content unknown"
                st.content = st.content + w.content
            else:
                if not st.history:
                    st.created = True
                st.content = w.content
                st.problems.clear()  # overwritten: earlier unknowns no longer matter
            if w.uncertain:
                st.problems.append(
                    f"{w.how} write in a branch, loop or with unresolved shell variables; may not match"
                )
                note = (note + "; " if note else "") + "may not have run as recorded"
            st.history.append(Version(st.content, w.how, op, note))
        for src, dst, move in getattr(effects, "copies", []):
            src_key = canonical(src, windows)
            dst_c = canonical(dst, windows)
            sources = [k for k in files if k == src_key or fnmatch.fnmatch(k, src_key)] if any(
                c in src_key for c in "*?[") else [src_key]
            for key in sources:
                target = dst_c.rstrip("/") + "/" + key.rsplit("/", 1)[-1] if _is_dir_target(dst_c, files) or len(sources) > 1 else dst_c
                srcst = files.get(key)
                how = "mv" if move else "cp"
                if move and srcst is not None:
                    srcst.deleted = True
                if srcst is not None and srcst.content is not None:
                    st = state(target)
                    st.operations.append(op)
                    if not st.history:
                        st.created = True
                    st.content = srcst.content
                    st.history.append(Version(st.content, how, op, f"{how} from {key}"))
                else:
                    mark_produced(target, f"{how} from {key}", op)
        for s_ in effects.scripts:
            scripts.append(ScriptRun(s_, op, len(commands) - 1))
        # Script files run from disk (python build.py, bash x.sh): what they write.
        for script_path, lang, run_cwd, argv in getattr(effects, "executed", []):
            st = files.get(canonical(script_path, windows))
            if st is None or st.content is None:
                continue
            name = st.path.rsplit("/", 1)[-1]
            if lang == "sh":
                try:
                    inner = analyze_bash(st.content, run_cwd, env)
                except Exception:
                    continue
                outputs = [w.path for w in inner.writes] + [p for p, _ in inner.produced]
            else:
                outputs = [resolve(p, run_cwd) for p in script_outputs(st.content, lang, argv)]
            for out_path in outputs:
                mark_produced(out_path, f"written by {name}", op)
        for path, how in getattr(effects, "produced", []):
            mark_produced(path, how, op)
        for d in effects.deletes:
            d = canonical(d, windows)
            _mark_deleted(files, d)
            for key, pr in produced.items():
                if key == d or key.startswith(d.rstrip("/") + "/") or fnmatch.fnmatch(key, d):
                    pr.deleted = True

    # Files programs created that we only know from their output (ls, logs,
    # "saved to ..."): scratchpad paths that appear in tool results.
    for op in commands:
        for mentioned in _scratchpad_mentions(op.output):
            key = canonical(mentioned, windows)
            if key not in files and key not in produced:
                mark_produced(key, "seen in command output", op)
    # ...and paths the model wrote in its messages, or that background-task
    # notifications report (their commands may live in another transcript).
    for file in tfiles:
        for rec in iter_records(file):
            texts: list[tuple[str, str]] = []
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            if rec.get("type") == "assistant":
                texts = [(b.get("text") or "", "mentioned by the model") for b in _content_blocks(rec)
                         if isinstance(b, dict) and b.get("type") == "text"]
            elif rec.get("type") == "user" and isinstance(msg.get("content"), str):
                if "<task-notification>" in msg["content"]:
                    texts = [(html.unescape(msg["content"]), "mentioned in a task notification")]
            elif rec.get("type") == "queue-operation" and rec.get("operation") == "enqueue":
                if isinstance(rec.get("content"), str) and "<task-notification>" in rec["content"]:
                    texts = [(html.unescape(rec["content"]), "mentioned in a task notification")]
            for text, how in texts:
                for mentioned in _scratchpad_mentions(text):
                    key = canonical(mentioned, windows)
                    if key not in files and key not in produced:
                        produced[key] = Produced(path=key, how=[how])

    for st in files.values():
        st.category = categorize(st, root)
    for pr in produced.values():
        pr.category = categorize(FileState(path=pr.path, created=True, deleted=pr.deleted), root)
    return Session(transcript.stem, root, calls, files, commands, scripts, produced)


# --------------------------------------------------------------------------
# Classifying and naming output


def _under(path: str, root: str | None) -> bool:
    if not root:
        return False
    p, r = _norm(path), _norm(root).rstrip("/")
    if re.match(r"^[A-Za-z]:/", r):
        p, r = p.lower(), r.lower()
    return p.startswith(r + "/")


def categorize(st: FileState, root: str | None) -> str:
    p = _norm(st.path)
    if "/scratchpad/" in p:
        return "scratchpad"
    if TEMP_RE.match(p):
        return "tmp"
    if st.created and st.deleted and _under(p, root):
        return "ran_from_root"
    return "other"


def path_matches(path: str, prefixes: Iterable[str], markers: Iterable[str]) -> bool:
    p = _norm(path)
    return any(p.startswith(_norm(x)) for x in prefixes) or any(m in p for m in markers)


def select(
    session: Session,
    prefixes: Iterable[str] = (),
    markers: Iterable[str] = (),
    match_all: bool = False,
) -> dict[str, FileState]:
    """Files in scratchpad/tmp/ran_from_root, plus 'other' files the filters match."""
    prefixes, markers = tuple(prefixes), tuple(markers)
    return {
        p: st
        for p, st in session.files.items()
        if st.category != "other" or match_all or path_matches(p, prefixes, markers)
    }


def reconstruct(
    transcript: Path,
    prefixes: Iterable[str] = DEFAULT_PREFIXES,
    markers: Iterable[str] = DEFAULT_MARKERS,
    match_all: bool = False,
    include_subagents: bool = True,
) -> dict[str, FileState]:
    """Reconstruct files written during a session (file tools and shell writes)."""
    return select(analyze(transcript, include_subagents), prefixes, markers, match_all)


def tool_slug(tool: str) -> str:
    """Folder name for a tool: 'Bash' -> 'bash', 'mcp__github__x' -> 'mcp__github__x'."""
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in tool.lower())
    return slug.strip(".") or "unknown"


def output_dir_for(path: str, category: str | None = None, root: str | None = None) -> PurePosixPath:
    """Where a file goes under <session_id>/, e.g. scratchpad/sub, tmp/build, ran_from_root/x."""
    norm = _norm(path)
    parts = PurePosixPath(norm).parts
    if category is None:
        category = "scratchpad" if "scratchpad" in parts[:-1] else "tmp" if TEMP_RE.match(norm) else "other"
    if category == "scratchpad" and "scratchpad" in parts[:-1]:
        idx = len(parts) - 1 - parts[::-1].index("scratchpad", 1)
        return PurePosixPath("scratchpad", *parts[idx + 1 : -1])
    if category == "tmp":
        m = TEMP_RE.match(norm)
        if m:
            rest = PurePosixPath(norm[m.end():]).parts[:-1]
            return PurePosixPath("tmp", *rest)
    if category == "ran_from_root" and root and _under(norm, root):
        rel = norm[len(_norm(root).rstrip("/")) + 1 :]
        return PurePosixPath("ran_from_root", *PurePosixPath(rel).parts[:-1])
    rest = [p for p in parts[:-1] if p not in ("/",) and not p.endswith(":")]
    return PurePosixPath("other", *rest)


def versioned_names(path: str, count: int) -> list[str]:
    """a.sh with 3 versions -> ['a_00.sh', 'a_01.sh', 'a_02.sh']."""
    name = PurePosixPath(_norm(path)).name
    stem, dot, ext = name.rpartition(".")
    if not stem:  # no extension, or a dotfile like ".env"
        stem, dot, ext = name, "", ""
    width = max(2, len(str(count - 1)))
    return [f"{stem}_{i:0{width}d}{dot}{ext}" for i in range(count)]
