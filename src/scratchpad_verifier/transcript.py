"""Parse Claude Code .jsonl transcripts and replay file writes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

DEFAULT_PREFIXES = ("/tmp/",)
DEFAULT_MARKERS = ("/scratchpad/",)
FILE_TOOLS = {"Write", "Edit", "MultiEdit"}


@dataclass
class Operation:
    """One Write/Edit/MultiEdit tool call that targeted a file."""

    tool: str
    path: str
    tool_use_id: str
    input: dict
    timestamp: str | None = None
    source: str = ""
    is_error: bool = False
    error: str | None = None
    result: dict | None = None


@dataclass
class FileState:
    """Reconstructed state of one file after replaying its operations."""

    path: str
    content: str | None = None
    operations: list[Operation] = field(default_factory=list)
    # Content after each successful iteration, oldest first. If the first
    # operation edited a pre-existing file, its original content comes first.
    versions: list[str] = field(default_factory=list)

    @property
    def errored(self) -> list[Operation]:
        return [o for o in self.operations if o.is_error]
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.content is not None and not self.problems


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
                yield rec


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


def collect_operations(files: Iterable[Path]) -> list[Operation]:
    """Collect file-writing tool calls, matched with their results, in time order."""
    return [o for o in collect_calls(files) if o.tool in FILE_TOOLS and o.path]


def collect_calls(files: Iterable[Path]) -> list[Operation]:
    """Collect every tool call, matched with its result, in time order.

    ``path`` is the ``file_path`` input when the tool has one, else "".
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
                    name = block.get("name")
                    inp = block.get("input") or {}
                    if not isinstance(inp, dict):
                        inp = {"input": inp}
                    path = inp.get("file_path")
                    tid = block.get("id") or f"anon-{len(order)}"
                    if tid in ops:
                        continue
                    ops[tid] = Operation(
                        tool=str(name),
                        path=path if isinstance(path, str) else "",
                        tool_use_id=tid,
                        input=inp,
                        timestamp=rec.get("timestamp"),
                        source=str(file),
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
                    op.is_error = bool(block.get("is_error"))
                    if op.is_error:
                        op.error = _result_text(block.get("content")).strip()
                        if op.error.startswith("<tool_use_error>") and op.error.endswith("</tool_use_error>"):
                            op.error = op.error[len("<tool_use_error>") : -len("</tool_use_error>")]
                    if isinstance(tur, dict):
                        op.result = tur
    result = [ops[t] for t in order]
    # Stable sort keeps file order for records without timestamps.
    result.sort(key=lambda o: o.timestamp or "")
    return result


def path_matches(path: str, prefixes: Iterable[str], markers: Iterable[str]) -> bool:
    return any(path.startswith(p) for p in prefixes) or any(m in path for m in markers)


def _apply_edit(content: str, old: str, new: str, replace_all: bool) -> str | None:
    if old == "":
        return new if content == "" else None
    if old not in content:
        return None
    return content.replace(old, new) if replace_all else content.replace(old, new, 1)


def replay(ops: Iterable[Operation]) -> dict[str, FileState]:
    """Replay operations per file and return the reconstructed states."""
    states: dict[str, FileState] = {}
    for op in ops:
        st = states.setdefault(op.path, FileState(path=op.path))
        st.operations.append(op)
        if op.is_error:
            continue
        if op.tool == "Write":
            st.content = op.input.get("content", "")
            st.problems.clear()
            st.versions.append(st.content)
            continue

        if st.content is None:
            original = (op.result or {}).get("originalFile")
            if isinstance(original, str):
                st.content = original
                st.versions.append(original)
            else:
                st.problems.append(
                    f"{op.tool} before any Write and no original content recorded"
                )
                continue

        if op.tool == "Edit":
            edits = [op.input]
        else:  # MultiEdit
            edits = op.input.get("edits") or []
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
                break
        if content is not None:
            st.content = content
            st.versions.append(content)
    return states


def reconstruct(
    transcript: Path,
    prefixes: Iterable[str] = DEFAULT_PREFIXES,
    markers: Iterable[str] = DEFAULT_MARKERS,
    match_all: bool = False,
    include_subagents: bool = True,
) -> dict[str, FileState]:
    """Reconstruct files written during a session, filtered to the given paths."""
    prefixes, markers = tuple(prefixes), tuple(markers)
    ops = collect_operations(transcript_files(transcript, include_subagents))
    if not match_all:
        ops = [o for o in ops if path_matches(o.path, prefixes, markers)]
    return replay(ops)


def errored_calls(
    transcript: Path,
    states: dict[str, FileState] | None = None,
    include_subagents: bool = True,
) -> list[Operation]:
    """Every tool call that returned an error and is not already in ``states``.

    This covers Bash and all other tools, plus failed file writes to paths
    outside the scratchpad/tmp filter, so no error goes unseen.
    """
    covered = {o.tool_use_id for st in (states or {}).values() for o in st.operations}
    return [
        o
        for o in collect_calls(transcript_files(transcript, include_subagents))
        if o.is_error and o.tool_use_id not in covered
    ]


def tool_slug(tool: str) -> str:
    """Folder name for a tool: 'Bash' -> 'bash', 'mcp__github__x' -> 'mcp__github__x'."""
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in tool.lower())
    return slug.strip(".") or "unknown"


def output_dir_for(path: str) -> PurePosixPath:
    """Where a file goes under <session_id>/: scratchpad/..., tmp/... or other/..."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if "scratchpad" in parts[:-1]:
        idx = len(parts) - 1 - parts[::-1].index("scratchpad", 1)
        return PurePosixPath("scratchpad", *parts[idx + 1 : -1])
    if len(parts) > 1 and parts[0] == "/" and parts[1] == "tmp":
        return PurePosixPath("tmp", *parts[2:-1])
    rest = [p for p in parts[:-1] if p not in ("/",) and not p.endswith(":")]
    return PurePosixPath("other", *rest)


def versioned_names(path: str, count: int) -> list[str]:
    """a.sh with 3 versions -> ['a_00.sh', 'a_01.sh', 'a_02.sh']."""
    name = PurePosixPath(path.replace("\\", "/")).name
    stem, dot, ext = name.rpartition(".")
    if not stem:  # no extension, or a dotfile like ".env"
        stem, dot, ext = name, "", ""
    width = max(2, len(str(count - 1)))
    return [f"{stem}_{i:0{width}d}{dot}{ext}" for i in range(count)]
