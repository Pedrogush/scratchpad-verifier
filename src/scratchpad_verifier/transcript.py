"""Parse Claude Code .jsonl transcripts and replay file writes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
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
    result: dict | None = None


@dataclass
class FileState:
    """Reconstructed state of one file after replaying its operations."""

    path: str
    content: str | None = None
    operations: list[Operation] = field(default_factory=list)
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


def collect_operations(files: Iterable[Path]) -> list[Operation]:
    """Collect file-writing tool calls, matched with their results, in time order."""
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
                    if name not in FILE_TOOLS or not isinstance(inp.get("file_path"), str):
                        continue
                    tid = block.get("id") or f"anon-{len(order)}"
                    if tid in ops:
                        continue
                    ops[tid] = Operation(
                        tool=name,
                        path=inp["file_path"],
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
    """Replay operations per file and return the reconstructed final states."""
    states: dict[str, FileState] = {}
    for op in ops:
        st = states.setdefault(op.path, FileState(path=op.path))
        st.operations.append(op)
        if op.is_error:
            continue
        if op.tool == "Write":
            st.content = op.input.get("content", "")
            st.problems.clear()
            continue

        if st.content is None:
            original = (op.result or {}).get("originalFile")
            if isinstance(original, str):
                st.content = original
            else:
                st.problems.append(
                    f"{op.tool} before any Write and no original content recorded"
                )
                continue

        if op.tool == "Edit":
            edits = [op.input]
        else:  # MultiEdit
            edits = op.input.get("edits") or []
        for edit in edits:
            new = _apply_edit(
                st.content,
                edit.get("old_string", ""),
                edit.get("new_string", ""),
                bool(edit.get("replace_all")),
            )
            if new is None:
                st.problems.append(f"{op.tool} old_string not found ({op.tool_use_id})")
                break
            st.content = new
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
