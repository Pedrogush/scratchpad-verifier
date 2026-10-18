import subprocess
from pathlib import Path

from hypothesis import given, settings, strategies as st

WT = Path("/tmp/scratchpad-verifier-meta/3c707265-2be6-4ac9-a1ca-5200fd8a21b2/scratchpad/worktree")

tracked = sorted(
    subprocess.run(["git", "-C", str(WT), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
)
assert len(tracked) >= 5, f"need at least 5 tracked files, found {len(tracked)}"

text =st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=5, max_size=40)
op = st.tuples(st.sampled_from(["delete", "append", "replace"]), text, st.integers(min_value=0))


@settings(database=None, max_examples=1)
@given(
    files=st.lists(st.sampled_from(tracked), min_size=5, max_size=15, unique=True),
    data=st.data(),
)
def mutate(files, data):
    print(f"mutating {len(files)} of {len(tracked)} tracked files")
    for f in files:
        path = WT / f
        action, txt, n = data.draw(op)
        if action == "delete":
            path.unlink()
            print(f"deleted  {f}")
        elif action == "append":
            with path.open("a") as fh:
                fh.write("\n" + txt + "\n")
            print(f"appended {f}: {txt!r}")
        else:
            lines = path.read_text(errors="replace").splitlines()
            if not lines:
                lines = [""]
            i = n % len(lines)
            lines[i] = txt
            path.write_text("\n".join(lines) + "\n")
            print(f"replaced {f} line {i + 1}: {txt!r}")


mutate()
