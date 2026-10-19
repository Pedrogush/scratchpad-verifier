#!/usr/bin/env bash
set -euo pipefail
SP="C:/Users/Pedro/AppData/Local/Temp/scratchpad-verifier-meta/4e8633bf-68d7-4464-81a8-055f012eb52c/scratchpad"
git clone https://github.com/Pedrogush/scratchpad-verifier "$SP/clone"
git -C "$SP/clone" worktree add -b meta-test "$SP/worktree"
echo "clone and worktree ready"
