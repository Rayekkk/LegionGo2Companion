#!/usr/bin/env bash
# Run the backend suite on Linux (WSL2) like CI. /mnt/c (drvfs) lacks POSIX modes, FIFOs and owner
# semantics the tests rely on, so the working tree is copied to the native WSL filesystem first.
# Usage (from Windows): wsl -d Ubuntu-24.04 -- bash "/mnt/c/.../scripts/wsl-test.sh" [python3.14] [unittest args...]
# Default: full suite on python3.14 (STANDARD §4: the only supported version). Log: /tmp/lg2c-test.log (summary + failures printed).
# LG2C_DST / LG2C_LOG override the copy and log paths (parallel runs from separate worktrees).
set -euo pipefail
src="$(cd "$(dirname "$0")/.." && pwd)"
dst="${LG2C_DST:-$HOME/lg2c}"; log="${LG2C_LOG:-/tmp/lg2c-test.log}"
py="${1:-python3.14}"; shift || true
rm -rf "$dst"; mkdir -p "$dst"
# Tracked + untracked (non-ignored) working-tree files, so uncommitted edits are tested too.
(cd "$src" && git ls-files -z --cached --others --exclude-standard | tar --null --ignore-failed-read -T - -cf -) | tar -xf - -C "$dst"
cd "$dst"
args=("$@"); [ ${#args[@]} -gt 0 ] || args=(discover -s tests -p "test_*.py")
set +e
PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 "$py" -W error -m unittest "${args[@]}" > "$log" 2>&1
rc=$?
set -e
tail -3 "$log"
grep -E "^(FAIL|ERROR):" "$log" | cut -c1-160 | sort -u || true
exit $rc
