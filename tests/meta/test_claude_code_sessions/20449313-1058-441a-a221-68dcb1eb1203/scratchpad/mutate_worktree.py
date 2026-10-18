import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st, HealthCheck

WORKTREE = Path(__file__).resolve().parent / "worktree"

tracked = sorted(
    subprocess.run(
        ["git", "-C", str(WORKTREE), "ls-files"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
)
if len(tracked) < 5:
    sys.exit(f"need at least 5 tracked files in {WORKTREE}, found {len(tracked)}")

text_st = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=40
)
action_st = st.tuples(st.sampled_from(["delete", "append", "replace"]), text_st, st.integers(0, 10**6))


@settings(database=None, max_examples=1, derandomize=False,
          suppress_health_check=list(HealthCheck))
@given(
    files=st.lists(st.sampled_from(tracked), min_size=5, max_size=15, unique=True),
    data=st.data(),
)
def mutate(files, data):
    print(f"mutating {len(files)} of {len(tracked)} tracked files in {WORKTREE}")
    for rel in files:
        path = WORKTREE / rel
        action, text, idx = data.draw(action_st)
        if action == "delete":
            path.unlink()
            print(f"deleted  {rel}")
        elif action == "append":
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + text + "\n")
            print(f"appended {rel}: {text!r}")
        else:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if not lines:
                path.write_text(text + "\n", encoding="utf-8")
                print(f"replaced {rel} (empty file) with {text!r}")
                continue
            i = idx % len(lines)
            lines[i] = text + "\n"
            path.write_text("".join(lines), encoding="utf-8")
            print(f"replaced {rel} line {i + 1} with {text!r}")


if __name__ == "__main__":
    mutate()
