#!/usr/bin/env bash
# Copy the scratchpad (minus clone/worktree/.venv) and the session transcript into the repo's test fixtures.
set -euo pipefail
SID=3c707265-2be6-4ac9-a1ca-5200fd8a21b2
SP=/tmp/scratchpad-verifier-meta/$SID/scratchpad
DEST=tests/meta/test_claude_code_sessions/$SID
CFG=${CLAUDE_CONFIG_DIR:-$HOME/.claude}

mkdir -p "$DEST/scratchpad"
tar -C "$SP" --exclude=./clone --exclude=./worktree --exclude=./.venv -cf - . | tar -C "$DEST/scratchpad" -xf -

transcript=$(find "$CFG/projects" -name "$SID.jsonl" -print -quit)
[ -n "$transcript" ] || { echo "transcript not found under $CFG/projects" >&2; exit 1; }
cp "$transcript" "$DEST.jsonl"

echo "copied scratchpad to $DEST/scratchpad/ and transcript to $DEST.jsonl"
