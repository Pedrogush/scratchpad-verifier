"""Copy the scratchpad (minus clone/worktree/.venv) and the session transcript into the repo."""
import os
import shutil
import sys
from pathlib import Path

SESSION = "3fa551b1-ac92-4314-b257-a6ca7b32abbe"
SP = Path(sys.argv[1])
OUT = Path("tests/meta/test_claude_code_sessions")
SKIP = {"clone", "worktree", ".venv"}

dest = OUT / SESSION / "scratchpad"
shutil.copytree(
    SP, dest, dirs_exist_ok=True,
    ignore=lambda d, names: [n for n in names if Path(d) == SP and n in SKIP],
)
print(f"copied scratchpad -> {dest}")

config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
matches = list((config / "projects").glob(f"*/{SESSION}.jsonl"))
if len(matches) != 1:
    sys.exit(f"expected one transcript, found {len(matches)}")
shutil.copy2(matches[0], OUT / f"{SESSION}.jsonl")
print(f"copied transcript -> {OUT / (SESSION + '.jsonl')}")
