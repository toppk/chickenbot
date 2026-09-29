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
# Where this sits relative to the last release. `v0.2.0` exactly on one,
# `v0.2.0-3-g54f0a4d` three commits past, the bare sha if there is no release.
described=$(git describe --tags --match 'v*' --dirty --always 2>/dev/null || echo "$revision")
dirty=""
if ! git diff --quiet || ! git diff --cached --quiet; then
    dirty=" (dirty)"
    if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
        echo "working tree has uncommitted changes; commit them or set ALLOW_DIRTY=1" >&2
        exit 1
    fi
fi

# pyproject stays authoritative; a release tag that disagrees with it is a
# release nobody can install by the name it claims.
declared=$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)
case "$described" in
    v*-*-g*|*dirty*) ;;  # past a release, or not clean: nothing to check
    v*)
        if [ "${described#v}" != "$declared" ]; then
            echo "tag $described disagrees with pyproject version $declared" >&2
            exit 1
        fi
        ;;
esac

# The stamp is a PEP 440 local version: empty on a release, "3.g54f0a4d" past
# one, so `version()` reads 0.2.0 or 0.2.0+3.g54f0a4d.
case "$described" in
    v*-*-g*) stamp="$(echo "$described" | sed 's/^v[^-]*-\([0-9]*\)-g/\1.g/')" ;;
    v*)      stamp="" ;;
    *)       stamp="$described" ;;
esac
[ -n "$dirty" ] && stamp="${stamp:+$stamp.}dirty"

echo "building $described$dirty"
rm -rf "$SRC/dist"
# Stamped into the package: the running bot has no working tree to ask, and
# the whole point is that it is not running the working tree.
echo "$stamp" > "$SRC/src/chickenbot/_revision.txt"
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

# A tag per deploy, pushed, so what ran on a given evening can be checked out
# by name rather than reconstructed from a timestamp in DEPLOYED.
tag="deploy/$(date +%Y%m%d-%H%M%S)"
if [ -z "$dirty" ]; then
    git tag -a "$tag" -m "deployed $revision" >/dev/null
    if git remote get-url origin >/dev/null 2>&1; then
        git push --quiet origin "$tag" 2>/dev/null && echo "tagged $tag" || echo "tagged $tag (not pushed)"
    else
        echo "tagged $tag (no remote)"
    fi
else
    echo "not tagging a dirty build"
fi

cat > "$SERVER/DEPLOYED" <<EOF
revision: $revision$dirty
release:  $described
tag:      ${tag:-none}
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
