#!/usr/bin/env bash
# Build the checkout and install it into the server venv.
#
# The services run what this script installed, not whatever the working tree
# happens to say. `git status` is checked because a wheel built from a dirty
# tree cannot be traced back to anything.
set -euo pipefail

SRC="${SRC:-$HOME/workspace/chickenbot}"
SERVER="${SERVER:-$HOME/server/chickenbot}"
VENV="$SERVER/venv"

cd "$SRC"
revision=$(git rev-parse --short HEAD)
dirty=""
if ! git diff --quiet || ! git diff --cached --quiet; then
    dirty=" (dirty)"
    if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
        echo "working tree has uncommitted changes; commit them or set ALLOW_DIRTY=1" >&2
        exit 1
    fi
fi

echo "building $revision$dirty"
rm -rf "$SRC/dist"
uv build --wheel -o "$SRC/dist" >/dev/null
(cd "$SRC/external" && uv build --wheel -o "$SRC/dist" >/dev/null)

if [ ! -d "$VENV" ]; then
    echo "creating $VENV"
    uv venv "$VENV" >/dev/null
fi
# --reinstall: the version rarely changes between deploys, so uv would
# otherwise decide the already-installed wheel is good enough.
uv pip install --python "$VENV/bin/python" --reinstall --quiet \
    "$SRC"/dist/chickenbot-*.whl "$SRC"/dist/chickenbot_github_tool-*.whl

cat > "$SERVER/DEPLOYED" <<EOF
revision: $revision$dirty
built:    $(date --iso-8601=seconds)
from:     $SRC
versions: $("$VENV/bin/chickenbot" --version)
EOF
cat "$SERVER/DEPLOYED"

instances=$(systemctl --user list-units 'chickenbot@*' --no-legend --plain 2>/dev/null | awk '{print $1}')
if [ -n "$instances" ]; then
    echo
    echo "running instances not restarted; when ready:"
    for unit in $instances; do
        name="${unit#chickenbot@}"; name="${name%.service}"
        echo "  systemctl --user restart chickenbot@$name chickenbot-github@$name"
    done
fi
