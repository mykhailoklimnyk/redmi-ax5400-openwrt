#!/bin/bash
# Fetch the sources for one variant, apply this project's changes and write the build configuration.
#   scripts/prepare.sh <nss|next|official> [version-tag]
# The source tree goes to $SRC (default: ./src). Nothing is compiled here.
set -euo pipefail

VARIANT="${1:?usage: prepare.sh <nss|next|official> [version-tag]}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${SRC:-$ROOT/src}"
LOGS="${LOGS:-$ROOT/logs}"
VERSION="${2:-$VARIANT-$(date -u +%Y%m%d)-0}"
# shellcheck disable=SC1091
. "$ROOT/sources.env"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
mkdir -p "$SRC" "$LOGS"

fail() { echo "FAIL: $*" >&2; exit 1; }

fetch() { # <repo> <ref> <dir>
	mkdir -p "$3"
	[ -d "$3/.git" ] || { git -C "$3" init -q; git -C "$3" remote add origin "$1"; }
	git -C "$3" fetch -q --depth 1 origin "$2"
	git -C "$3" checkout -q -f FETCH_HEAD
}

case "$VARIANT" in
nss)
	fetch "$NSS_REPO" "$NSS_COMMIT" "$SRC"
	[ "$(git -C "$SRC" rev-parse HEAD)" = "$NSS_COMMIT" ] || fail "unexpected NSS source commit"
	;;
next)
	fetch "$NEXT_REPO" "$NEXT_COMMIT" "$SRC"
	[ "$(git -C "$SRC" rev-parse HEAD)" = "$NEXT_COMMIT" ] || fail "unexpected source commit for the next variant"
	;;
official)
	fetch "$OFFICIAL_REPO" "$OFFICIAL_REF" "$SRC"
	;;
*) fail "unknown variant $VARIANT" ;;
esac
SOURCE_COMMIT="$(git -C "$SRC" rev-parse HEAD)"
echo "variant $VARIANT, version $VERSION, source $SOURCE_COMMIT"
cd "$SRC"

# --- package feeds -------------------------------------------------------------------------
[ "$VARIANT" = nss ] && cp ra74/feeds.conf feeds.conf
[ "$VARIANT" = next ] && cp "$ROOT/config/feeds-next.conf" feeds.conf
./scripts/feeds update -a > "$LOGS/feeds-update.log" 2>&1 || fail "feeds update (see logs/feeds-update.log)"
./scripts/feeds install -a > "$LOGS/feeds-install.log" 2>&1 || fail "feeds install (see logs/feeds-install.log)"
# these three form a dependency loop in the pinned feeds
./scripts/feeds uninstall librespeed-cli librespeed-cli-rust luci-app-librespeed >> "$LOGS/feeds-install.log" 2>&1 || true

fetch "$ARGON_THEME_REPO" "$ARGON_THEME_COMMIT" package/extra/luci-theme-argon
fetch "$ARGON_CONFIG_REPO" "$ARGON_CONFIG_COMMIT" package/extra/luci-app-argon-config

# --- NAND ECC: let the kernel pick it from the OOB size (openwrt PR #24763) --------------------
KV="$(sed -n 's/^KERNEL_PATCHVER:=//p' target/linux/qualcommax/Makefile)"
[ -n "$KV" ] || fail "cannot read the kernel version of the qualcommax target"
P="target/linux/qualcommax/patches-$KV"
DTS=target/linux/qualcommax/dts/ipq5018-redmi-ax5400.dts
[ -d "$P" ] && [ -f "$DTS" ] || fail "source layout changed: $P or $DTS is missing"
if grep -rqs 'oobsize < 80' "$P"; then
	echo "ECC: the OOB-size rule is already in the source, no patch added"
else
	rm -f "$P"/0401-spi-spi-qpic-snand-default-to-4-bit-ECC.patch
	cp "$ROOT"/patches/openwrt-pr24763/*.patch "$P"/
	echo "ECC: added the patches from openwrt PR #24763"
fi
sed -i -E '/nand-ecc-(step-size|strength) = <[0-9]+>;/d' "$DTS"
[ "$(grep -c 'nand-ecc' "$DTS")" = 1 ] && grep -q 'nand-ecc-engine' "$DTS" || fail "unexpected nand-ecc lines left in $DTS"

# --- WAN port link (openwrt PR #26511); the NSS variant drives the port differently ----------------
if [ "$VARIANT" = official ]; then
	if grep -rqs 'dsa_shared_port_open_conduit' target/linux/generic/*-"$KV"; then
		echo "WAN: the conduit fix is already in the source, no patch added"
	else
		cp "$ROOT"/patches/openwrt-pr26511/*.patch "target/linux/generic/pending-$KV/"
		echo "WAN: added the patch from openwrt PR #26511"
	fi
fi

# --- files added to the image ----------------------------------------------------------------
mkdir -p files
cp -a "$ROOT"/files/. files/
chmod 755 files/usr/sbin/ax5400-ota files/usr/sbin/dns-lan-check files/etc/uci-defaults/*
cat > files/etc/ax5400-release <<EOF
AX5400_VARIANT=$VARIANT
AX5400_VERSION=$VERSION
AX5400_SOURCE=$SOURCE_COMMIT
AX5400_REPO=${GITHUB_REPOSITORY:-}
EOF

# --- configuration ---------------------------------------------------------------------------
WANT="$(grep -hv '^[[:space:]]*#' "$ROOT/config/packages-common.txt" "$ROOT/config/packages-$VARIANT.txt" | tr -s '[:space:]' ' ')"
if [ "$VARIANT" = nss ]; then
	cp ra74/build.config .config
	# the full image does not fit as an initramfs; dnsmasq is replaced by dnsmasq-full
	sed -i -e 's/^CONFIG_TARGET_ROOTFS_INITRAMFS=y/# CONFIG_TARGET_ROOTFS_INITRAMFS is not set/' \
		-e 's/^CONFIG_PACKAGE_dnsmasq=y/# CONFIG_PACKAGE_dnsmasq is not set/' \
		-e 's/^CONFIG_PACKAGE_tcpdump=y/# CONFIG_PACKAGE_tcpdump is not set/' .config
elif [ "$VARIANT" = next ]; then
	# the base project's own release configuration for the 512 MB / 1 GB boards, reduced to this
	# one device (its CI builds all boards into one multi-profile image set)
	CI=.github/ci/ipq50xx
	[ -f "$CI/common.config" ] && [ -f "$CI/std.config" ] || fail "source layout changed: $CI configs are missing"
	cat "$CI/common.config" "$CI/std.config" | grep -E '^CONFIG_' 		| grep -v -E '_DEVICE_|^CONFIG_TARGET_MULTI_PROFILE=|^CONFIG_TARGET_PER_DEVICE_ROOTFS=|^CONFIG_DEVEL=|^CONFIG_CCACHE=|^CONFIG_AUTOREMOVE=|^CONFIG_KERNEL_BUILD_|^CONFIG_PACKAGE_wpad-mesh-mbedtls=' > .config
	grep -q '^CONFIG_NSS_FIRMWARE_VERSION_' .config || fail "the base configuration names no NSS firmware version"
	cat >> .config <<'CFG'
CONFIG_TARGET_qualcommax_ipq50xx_DEVICE_xiaomi_redmi-ax5400=y
# CONFIG_PACKAGE_dnsmasq is not set
CFG
else
	cat > .config <<'CFG'
CONFIG_TARGET_qualcommax=y
CONFIG_TARGET_qualcommax_ipq50xx=y
CONFIG_TARGET_qualcommax_ipq50xx_DEVICE_xiaomi_redmi-ax5400=y
# CONFIG_PACKAGE_dnsmasq is not set
CFG
fi
sed -i '/^CONFIG_LUCI_LANG_uk=/d; /^# CONFIG_LUCI_LANG_uk is not set/d' .config
echo 'CONFIG_LUCI_LANG_uk=y' >> .config
for p in $WANT; do
	sed -i "/^# CONFIG_PACKAGE_$p is not set/d; /^CONFIG_PACKAGE_$p=/d" .config
	echo "CONFIG_PACKAGE_$p=y" >> .config
done
make defconfig > "$LOGS/defconfig.log" 2>&1 || fail "make defconfig (see logs/defconfig.log)"
# The pinned NSS tree is known to be clean, so any loop there is ours. Current OpenWrt main carries
# loops between unrelated feed packages; there they are only reported, and the package check
# below remains the gate.
if grep -i -B1 'recursive dependency' "$LOGS/defconfig.log"; then
	[ "$VARIANT" = nss ] && fail "recursive dependency in the configuration"
	echo "WARNING: recursive dependencies reported by upstream packages (listed above)"
fi
MISSING=""
for p in $WANT; do grep -q "^CONFIG_PACKAGE_$p=y" .config || MISSING="$MISSING $p"; done
[ -z "$MISSING" ] || fail "packages not selected after defconfig:$MISSING"
grep -q '^CONFIG_TARGET_qualcommax_ipq50xx_DEVICE_xiaomi_redmi-ax5400=y' .config || fail "device not selected"

# --- the NSS project's own static checks -------------------------------------------------------
if [ "$VARIANT" = nss ]; then
	python3 ra74/tests/check_dma.py . || fail "check_dma"
	python3 ra74/tests/check_switch.py . --dual-link || fail "check_switch"
	python3 ra74/tests/check_defaults.py . || fail "check_defaults"
fi

{
	echo "variant=$VARIANT"
	echo "version=$VERSION"
	echo "source_commit=$SOURCE_COMMIT"
	echo "kernel_series=$KV"
} > "$ROOT/build-info.txt"
git status --short | head -40
echo PREPARE_OK
