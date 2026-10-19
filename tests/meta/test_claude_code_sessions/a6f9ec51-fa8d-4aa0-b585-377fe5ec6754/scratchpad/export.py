import os
import shutil
import sys
from pathlib import Path

SESSION = "a6f9ec51-fa8d-4aa0-b585-377fe5ec6754"
SP = Path(__file__).resolve().parent
REPO = Path(sys.argv[1]).resolve()
DEST = REPO / "tests/meta/test_claude_code_sessions"


def ignore_top(directory, names):
    if Path(directory).resolve() == SP:
        return [n for n in names if n in {"clone", "worktree", ".venv"}]
    return []


shutil.copytree(SP, DEST / SESSION / "scratchpad", ignore=ignore_top, dirs_exist_ok=True)
print(f"copied scratchpad to {DEST / SESSION / 'scratchpad'}")

config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
matches = list((config / "projects").glob(f"*/{SESSION}.jsonl"))
if not matches:
    sys.exit("transcript not found")
shutil.copy2(matches[0], DEST / f"{SESSION}.jsonl")
print(f"copied transcript to {DEST / f'{SESSION}.jsonl'}")
