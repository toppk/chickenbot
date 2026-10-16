#!/usr/bin/env bash
# Cut a release: deploy/release.sh [patch|minor|major], or --next to print
# the version it would choose.
#
# A tag means a release and nothing else does. Deploying a test build needs
# none of this: `deploy/deploy.sh <instance>` installs whatever is checked
# out and stamps `0.4.0+3.g1b19257` when it is past a tag. Tag only when you
# mean to publish, and never move one -- the workflow fires on the tag, and
# a moved tag is a release nobody can reproduce.
#
# Does: checks, writes the version into pyproject.toml, commits that, pushes
# master, tags, pushes the tag, and watches the Release workflow.
set -euo pipefail
cd "$(dirname "$0")/.."

next_only=0
if [ "${1:-}" = "--next" ]; then next_only=1; shift; fi
bump="${1:-${BUMP:-}}"

latest=$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --merged master --sort=-v:refname | head -n 1)
latest="${latest:-v0.0.0}"
IFS=. read -r major minor patch <<EOT
${latest#v}
EOT

if [ -z "$bump" ]; then
    [ "$next_only" = 1 ] && { echo "usage: deploy/release.sh --next patch|minor|major" >&2; exit 2; }
    printf 'latest release: %s\nbump (patch, minor, major): ' "$latest"
    read -r bump
fi
case "$bump" in
    patch) patch=$((patch + 1)) ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    major) major=$((major + 1)); minor=0; patch=0 ;;
    *) echo "bump must be patch, minor or major" >&2; exit 2 ;;
esac
version="$major.$minor.$patch"
tag="v$version"
if [ "$next_only" = 1 ]; then echo "$tag"; exit 0; fi

die() { echo "release: $*" >&2; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = master ] || die "not on master"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "the worktree has uncommitted changes"
[ -f "release-notes/$version.md" ] || die "write release-notes/$version.md first (see release-notes/TEMPLATE.md)"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "$tag already exists; never reuse a version"
git fetch -q origin master
git merge-base --is-ancestor origin/master master || die "master does not contain origin/master; pull first"

echo "checks…"
uv run ruff check
uv run ruff format --check
uv run pytest -q

printf 'release %s -> %s? [y/N] ' "$latest" "$tag"
read -r answer
[ "$answer" = y ] || die "cancelled"

# pyproject stays authoritative and deploy.sh refuses a tag that disagrees
# with it, so the bump is part of the release rather than a thing to remember.
sed -i "0,/^version = \"$major\.$minor\.\|^version = \"/s/^version = \".*\"/version = \"$version\"/" pyproject.toml
declared=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
[ "$declared" = "$version" ] || die "could not set the version in pyproject.toml (it reads $declared)"
git add pyproject.toml release-notes
git commit -q -m "chickenbot $version"

git push origin master
git tag -a "$tag" -m "chickenbot $tag"
git push origin "$tag"
echo "waiting for the Release workflow…"
sleep 8
run=$(gh run list --workflow release.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$run" --exit-status
gh release view "$tag"
