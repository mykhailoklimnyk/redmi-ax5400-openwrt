#!/bin/bash
# Compare the pinned sources with what is upstream now. Prints a short report;
# exit code 10 means "something changed that a human should look at".
# Needs the GitHub CLI (gh) with a token in GH_TOKEN.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/sources.env"
changed=0

nss_slug="$(echo "$NSS_REPO" | sed -E 's#https://github.com/##; s#\.git$##')"
latest_tag="$(gh api "repos/$nss_slug/releases/latest" --jq .tag_name 2>/dev/null || true)"
if [ -n "$latest_tag" ] && [ "$latest_tag" != "$NSS_TAG" ]; then
	echo "- NSS source has a new release: \`$latest_tag\` (pinned: \`$NSS_TAG\`). Review the changes, then update \`sources.env\`."
	changed=1
else
	echo "- NSS source: no new release (pinned \`$NSS_TAG\`)."
fi

for pr in $WATCH_PRS; do
	state="$(gh api "repos/openwrt/openwrt/pulls/$pr" --jq 'if .merged then "merged" else .state end' 2>/dev/null || echo unknown)"
	case "$state" in
	open) echo "- openwrt PR #$pr: still open, the patch stays." ;;
	merged) echo "- openwrt PR #$pr was **merged**: the carried patch can be dropped once the source includes it."; changed=1 ;;
	*) echo "- openwrt PR #$pr is now **$state**: check what replaced it."; changed=1 ;;
	esac
done

off_slug="$(echo "$OFFICIAL_REPO" | sed -E 's#https://github.com/##; s#\.git$##')"
head="$(gh api "repos/$off_slug/commits/$OFFICIAL_REF" --jq '.sha[0:12] + " " + .commit.committer.date' 2>/dev/null || echo unknown)"
echo "- OpenWrt \`$OFFICIAL_REF\` is at $head (the monthly build takes whatever is current)."

[ "$changed" = 0 ] || exit 10
