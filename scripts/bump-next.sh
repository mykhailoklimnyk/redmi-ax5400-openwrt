#!/bin/bash
# If the base of the "next" image has a newer release, pin it: update NEXT_TAG and
# NEXT_COMMIT in sources.env and take the feeds that release was built with.
# Prints the new tag and exits 0 when something was changed, exits 10 when there is
# nothing new. Changes files only; committing and building is the caller's job.
# Needs the GitHub CLI (gh) with a token in GH_TOKEN.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/sources.env"
slug="$(echo "$NEXT_REPO" | sed -E 's#https://github.com/##; s#\.git$##')"
tag="$(gh api "repos/$slug/releases/latest" --jq .tag_name)"
[ -n "$tag" ] || { echo "cannot read the latest release of $slug" >&2; exit 1; }
[ "$tag" != "$NEXT_TAG" ] || exit 10
commit="$(gh api "repos/$slug/commits/$tag" --jq .sha)"
echo "$commit" | grep -Eq '^[0-9a-f]{40}$' || { echo "bad commit for $tag: $commit" >&2; exit 1; }
tmp="$(mktemp)"
gh release download "$tag" -R "$slug" -p feeds-std.buildinfo -O - > "$tmp"
# every feed must be pinned to a commit, or the build would not be reproducible
[ -s "$tmp" ] && ! grep -Ev '^src-git [a-z0-9_]+ https://[^ ^]+\^[0-9a-f]{40}$' "$tmp" | grep -q . \
	|| { echo "unexpected feeds file in release $tag" >&2; cat "$tmp" >&2; exit 1; }
cp "$tmp" "$ROOT/config/feeds-next.conf"; rm -f "$tmp"
sed -i -E "s#^NEXT_TAG=.*#NEXT_TAG=$tag#; s#^NEXT_COMMIT=.*#NEXT_COMMIT=$commit#" "$ROOT/sources.env"
echo "$tag"
