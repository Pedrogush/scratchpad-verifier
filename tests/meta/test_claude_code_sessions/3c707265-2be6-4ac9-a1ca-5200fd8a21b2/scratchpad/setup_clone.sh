#!/usr/bin/env bash
set -euo pipefail
SP=/tmp/scratchpad-verifier-meta/3c707265-2be6-4ac9-a1ca-5200fd8a21b2/scratchpad
git clone https://github.com/Pedrogush/scratchpad-verifier "$SP/clone"
git -C "$SP/clone" worktree add -b meta-test "$SP/worktree"
