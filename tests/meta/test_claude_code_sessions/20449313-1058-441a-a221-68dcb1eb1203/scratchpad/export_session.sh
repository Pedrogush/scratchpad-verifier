#!/usr/bin/env bash
# Copy the scratchpad (minus clone/, worktree/, .venv/) and this session's
# transcript into tests/meta/test_claude_code_sessions/ of the current directory.
set -euo pipefail

SESSION=20449313-1058-441a-a221-68dcb1eb1203
SP="$(cd "$(dirname "$0")" && pwd)"
DEST="$PWD/tests/meta/test_claude_code_sessions"
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

transcript="$(find "$CONFIG_DIR/projects" -name "$SESSION.jsonl" -print -quit)"
if [ -z "$transcript" ]; then
    echo "transcript $SESSION.jsonl not found under $CONFIG_DIR/projects" >&2
    exit 1
fi

mkdir -p "$DEST/$SESSION/scratchpad"
find "$SP" -mindepth 1 -maxdepth 1 \
    ! -name clone ! -name worktree ! -name .venv \
    -exec cp -a {} "$DEST/$SESSION/scratchpad/" \;
cp "$transcript" "$DEST/$SESSION.jsonl"

echo "copied scratchpad to $DEST/$SESSION/scratchpad:"
ls -A "$DEST/$SESSION/scratchpad"
echo "copied transcript $transcript -> $DEST/$SESSION.jsonl"
