#!/usr/bin/env bash
# Record real Claude Code sessions for tests/meta/test_claude_code_sessions.py.
#
# Each mode starts a fresh headless session (`claude -p`) with its own empty
# scratchpad directory. The session clones this repo
# into its scratchpad, makes random edits with hypothesis, and copies its
# scratchpad and transcript into tests/meta/test_claude_code_sessions/. After
# the session exits, the scratchpad and the complete transcript are copied
# again, so the snapshot is exactly what was on disk when the session ended,
# and a readable <id>.txt rendering of the transcript is written next to it.
#
# Usage: tests/meta/record_test_session.sh [scripts|inline ...]   (default: both)
#
# Environment overrides:
#   CLAUDE            claude executable              (default: claude)
#   PYTHON            python executable              (default: python3, then python)
#   REPO_URL          repository the session clones  (default: GitHub origin)
#   CLAUDE_ARGS       extra arguments for claude, e.g. "--model sonnet"
#
# The sessions run Bash and file tools without asking (--allowedTools), in
# this repository, on your machine. Run it only in a checkout you're happy
# for an agent to work in. Lines that look like secrets (TOKEN=, KEY=, emails,
# API keys) are redacted from the saved transcript, but still review the
# recordings before committing them.
set -euo pipefail

REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
OUT="$REPO_ROOT/tests/meta/test_claude_code_sessions"
CLAUDE=${CLAUDE:-claude}
PYTHON=${PYTHON:-$(command -v python3 || command -v python)}
REPO_URL=${REPO_URL:-https://github.com/Pedrogush/scratchpad-verifier}
TOOLS="Bash Read Write Edit MultiEdit Glob Grep"

modes=("$@")
[ ${#modes[@]} -eq 0 ] && modes=(scripts inline)
mkdir -p "$OUT"

prompt_for() {
  local mode=$1 sid=$2 sp=$3 how
  case $mode in
    scripts) how="For steps 1, 3 and 5, write the script as a file in your scratchpad and then run that file with a separate command. Use a different way of writing for each script, so every path you use to create and change files gets exercised:
- step 1 script: create it with a Bash heredoc (cat > file <<'EOF'), then change it once with sed -i.
- step 3 script: create it with the Write tool, then change it at least twice with the Edit tool (for example to add a print or a check) before its final run.
- step 5 script: create it with the Write tool, then rewrite it fully once with the Write tool.
Also copy the step 3 script to a backup file in the scratchpad with cp, then delete the backup with rm." ;;
    inline) how="Run this inline, don't run a script: do steps 1, 3 and 5 by running the code directly in the Bash tool (plain shell commands, python - <<'EOF' heredocs, or python -c), never by saving a script file and executing it. Steps 2, 4 and 6 are simply running that inline code." ;;
    *) echo "unknown mode: $mode (use scripts or inline)" >&2; exit 2 ;;
  esac
  cat <<EOF
This is a recorded test session for the scratchpad-verifier tool. It checks that the tool can rebuild, from your transcript, every file you leave in your scratchpad directory.

Your scratchpad for this session is SP=$sp (it exists and is empty). Use exactly this directory, even if your instructions name a different scratchpad.

Do these steps in order:
1. Write a script that git clones $REPO_URL into SP/clone and then adds a git worktree of that clone at SP/worktree on a new branch named meta-test.
2. Run that script.
3. Write a script that deletes and edits files at random inside SP/worktree using the hypothesis library: use hypothesis strategies to choose 5 to 15 random tracked files and, for each, either delete it, append random text, or replace a random line. Use @settings(database=None) so hypothesis writes no cache, and print what it changed. Run Python with \`uv run --with hypothesis python\` if uv is installed, otherwise install hypothesis into a virtualenv at SP/.venv.
4. Run that script, and then show \`git -C SP/worktree status --short\`.
5. Write a script that copies SP (leaving out SP/clone, SP/worktree and SP/.venv) to tests/meta/test_claude_code_sessions/$sid/scratchpad/ in the current directory, and copies your session transcript to tests/meta/test_claude_code_sessions/$sid.jsonl. Your session id is $sid; the transcript is the file named $sid.jsonl under ~/.claude/projects/ (or \$CLAUDE_CONFIG_DIR/projects/).
6. Run that script.

$how

Rules:
- Keep every file you create inside SP, apart from the copies made in step 5.
- After each step, add one line to SP/run_log.md saying what you did and whether it worked.
- Don't read, change or commit anything else in the current repository.
- Don't print environment variables, credentials or anything outside SP and this repository.
EOF
}

for mode in "${modes[@]}"; do
  sid=$("$PYTHON" -c 'import uuid; print(uuid.uuid4())')
  # A fresh, empty scratchpad whose path names the session, like Claude Code's own.
  sp="${TMPDIR:-/tmp}/scratchpad-verifier-meta/$sid/scratchpad"
  mkdir -p "$sp"
  command -v cygpath >/dev/null && sp=$(cygpath -m "$sp")  # git-bash: C:/... form
  echo "== recording $mode session $sid in $sp"
  # Unset the variables a parent Claude Code session sets, so this one starts clean.
  (cd "$REPO_ROOT" && env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_ENTRYPOINT \
    "$CLAUDE" -p "$(prompt_for "$mode" "$sid" "$sp")" --session-id "$sid" --output-format json \
    --allowedTools $TOOLS ${CLAUDE_ARGS:-} < /dev/null) > "$OUT/$sid.result.json" || echo "!! claude exited with $?" >&2

  "$PYTHON" - "$OUT" "$sid" "$mode" "$sp" <<'PYEOF'
"""Snapshot the scratchpad and transcript after the session ended."""
import datetime, json, os, re, shutil, subprocess, sys
from pathlib import Path

out, sid, mode, scratchpad = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
result_file = out / f"{sid}.result.json"
try:
    result = json.loads(result_file.read_text(encoding="utf-8"))
except ValueError:
    result = {}
result_file.unlink(missing_ok=True)

config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
transcripts = sorted((config / "projects").glob(f"*/{sid}.jsonl"))
if not transcripts:
    sys.exit(f"!! no transcript {sid}.jsonl under {config / 'projects'}")
transcript = transcripts[0]

dest = out / sid
excluded = []

def ignore(directory, names):
    """Leave out git checkouts, virtualenvs and caches: programs wrote them, not the agent."""
    skip = []
    for name in names:
        p = Path(directory, name)
        if p.is_dir() and ((p / ".git").exists() or (p / "pyvenv.cfg").exists()
                           or name in ("__pycache__", ".hypothesis", ".git")):
            skip.append(name)
            excluded.append(p.relative_to(scratchpad).as_posix())
    return skip

if os.path.isdir(scratchpad):
    shutil.rmtree(dest / "scratchpad", ignore_errors=True)
    shutil.copytree(scratchpad, dest / "scratchpad", ignore=ignore, symlinks=True)
    source = "after session exit"
elif (dest / "scratchpad").is_dir():
    source = "copied by the session (scratchpad was gone after exit)"
else:
    sys.exit(f"!! scratchpad for {sid} not found; nothing to compare")

# Redact secrets in case the session printed its environment anyway.
SECRET_LINE = re.compile(
    r"(?im)^([\w.-]*(?:token|secret|key|passw|email|uuid|auth|credential|cookie)[\w.-]*)=.*$")
SECRET_VALUE = re.compile(r"sk-ant-[\w-]+|gh[pousr]_\w{20,}|[\w.+-]+@[\w-]+\.[\w.]+")

def scrub(value):
    if isinstance(value, str):
        return SECRET_VALUE.sub("[redacted]", SECRET_LINE.sub(r"\1=[redacted]", value))
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value

# Drop the whole output of commands that list the environment.
ENV_COMMAND = re.compile(r"(^|[;&|(\s])(env|printenv|export -p|set)(\s*($|[|;&>]))")
records = [json.loads(line) for line in open(transcript, encoding="utf-8") if line.strip()]
env_ids = {
    b.get("id") for r in records for b in ((r.get("message") or {}).get("content") or [])
    if isinstance(b, dict) and b.get("type") == "tool_use"
    and ENV_COMMAND.search(str((b.get("input") or {}).get("command", "")))
}
REMOVED = "[environment listing removed from the recording]"
for r in records:
    for b in (r.get("message") or {}).get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in env_ids:
            b["content"] = REMOVED
            if isinstance(r.get("toolUseResult"), dict):
                r["toolUseResult"] = {"stdout": REMOVED, "stderr": "", "interrupted": False}
records = [scrub(r) for r in records]
with open(out / f"{sid}.jsonl", "w", encoding="utf-8") as dst:
    for r in records:
        dst.write(json.dumps(r, ensure_ascii=False) + "\n")

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", f"[{b.get('type')}]") if isinstance(b, dict) else str(b)
                         for b in content)
    return json.dumps(content, ensure_ascii=False)

def render(r):
    """One readable section per message block, for the .txt transcript."""
    who = "User" if r.get("type") == "user" else "Assistant"
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        yield f"> {who}:\n{content}"
        return
    for b in content or []:
        kind = b.get("type") if isinstance(b, dict) else None
        if kind == "text":
            yield f"> {who}:\n{b.get('text', '')}"
        elif kind == "thinking" and b.get("thinking"):
            yield f"> {who} (thinking):\n{b.get('thinking', '')}"
        elif kind == "tool_use":
            inputs = b.get("input") or {}
            body = "\n".join(f"  {k}: {v}" if "\n" not in str(v) else f"  {k}:\n{v}"
                             for k, v in inputs.items())
            yield f"> Tool call {b.get('name')} ({b.get('id')}):\n{body}"
        elif kind == "tool_result":
            error = " [error]" if b.get("is_error") else ""
            yield f"> Tool result ({b.get('tool_use_id')}){error}:\n{text_of(b.get('content'))}"

# A plain-text rendering of the same (redacted) conversation, easier to read than the JSONL.
with open(out / f"{sid}.txt", "w", encoding="utf-8") as dst:
    dst.write(f"Session {sid} ({mode}), scratchpad {scratchpad}\n")
    for r in records:
        if r.get("type") in ("user", "assistant") and not r.get("isMeta"):
            for section in render(r):
                dst.write("\n" + section.rstrip() + "\n")
subagents = transcript.with_suffix("") / "subagents"
if subagents.is_dir():
    shutil.rmtree(dest / "subagents", ignore_errors=True)
    shutil.copytree(subagents, dest / "subagents")

try:
    version = subprocess.run([os.environ.get("CLAUDE", "claude"), "--version"],
                             capture_output=True, text=True).stdout.strip()
except OSError:
    version = None
meta = {
    "session_id": sid,
    "mode": mode,
    "scratchpad": scratchpad,
    "snapshot": source,
    "excluded": sorted(excluded),
    "claude_version": version,
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "num_turns": result.get("num_turns"),
    "is_error": result.get("is_error"),
    "permission_denials": len(result.get("permission_denials") or []),
}
(dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
print(f"   saved {dest} ({source})")
PYEOF
done

echo "Compare with: python tests/meta/test_claude_code_sessions.py -v"
