#!/usr/bin/env bash
# setup.sh — prepare server_apk/ for buildozer packaging.
#
# The standalone server APK bundles azt_collabd/ and azt_collab_client/
# as top-level packages alongside main.py. buildozer's source.dir for
# this APK is the server_apk/ directory itself, so the python packages
# need to live (or symlink) inside it.
#
# This script creates those symlinks idempotently from a fresh checkout.
# Mirrors the sister-app symlink pattern documented in
# azt-collab/CLAUDE.md.
#
# Usage:
#     bash server_apk/setup.sh
# Or, from inside server_apk/:
#     bash setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Each entry: <link-name>:<target relative to SCRIPT_DIR>
LINKS=(
    "azt_collabd:../azt_collabd"
    "azt_collab_client:../azt_collab_client"
)

for entry in "${LINKS[@]}"; do
    name="${entry%%:*}"
    target="${entry#*:}"

    if [ -L "$name" ]; then
        existing="$(readlink "$name")"
        if [ "$existing" = "$target" ]; then
            echo "ok: $name -> $target (already correct)"
            continue
        fi
        echo "fix: $name -> $existing  =>  $target"
        rm "$name"
    elif [ -e "$name" ]; then
        echo "error: $name exists and is not a symlink — refusing to clobber" >&2
        exit 1
    fi

    ln -s "$target" "$name"
    echo "new: $name -> $target"
done

# Sanity check: imports resolve from this dir.
if command -v python3 >/dev/null; then
    python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
import azt_collabd, azt_collab_client
print(f'verified: azt_collabd v{azt_collabd.__version__}, '
      f'azt_collab_client v{azt_collab_client.__version__}')
"
fi
