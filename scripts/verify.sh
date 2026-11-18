#!/bin/bash
# Collect the built image into ./out under a fixed name and run every check on it.
# A release is published only if this script succeeds.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${SRC:-$ROOT/src}"
OUT="${OUT:-$ROOT/out}"
# shellcheck disable=SC1091
. "$ROOT/build-info.txt"
MAX_KERNEL=$((34 * 1024 * 1024)) # the ubi_kernel partition is 36 MiB
BIN="$SRC/bin/targets/qualcommax/ipq50xx"
rm -rf "$OUT"; mkdir -p "$OUT"
CHECKS="$OUT/checks.txt"
: > "$CHECKS"
ok() { echo "OK    $*" | tee -a "$CHECKS"; }
fail() { echo "FAIL  $*" | tee -a "$CHECKS"; exit 1; }

built=( "$BIN"/*redmi-ax5400*squashfs-sysupgrade.bin )
[ -f "${built[0]}" ] || fail "no sysupgrade image was produced"
IMG="$OUT/redmi-ax5400-$version-sysupgrade.bin"
cp "${built[0]}" "$IMG"
cp "$BIN"/*.manifest "$OUT/packages.manifest"
cp "$SRC/.config" "$OUT/build.config"
cp "$ROOT/build-info.txt" "$OUT/build-info.txt"

# 1. ECC: no fixed strength in the device tree, FIT hashes and metadata trailer intact
python3 "$ROOT/tools/patch_ecc.py" --check "$IMG" --expect auto > "$OUT/image-check.txt" 2>&1 \
	|| { cat "$OUT/image-check.txt"; fail "image check (ECC / hashes / trailer)"; }
grep -q MISMATCH "$OUT/image-check.txt" && fail "hash mismatch in the image"
ok "device tree has no fixed ECC strength; FIT hashes and metadata trailer are valid"

# 2. the fixes are really in the compiled kernel source
K="$(ls -d "$SRC"/build_dir/target-*/linux-qualcommax_ipq50xx/linux-*/ | head -n1)"
grep -q 'mtd->oobsize < 80' "$K/drivers/spi/spi-qpic-snand.c" || fail "ECC-by-OOB rule missing from the kernel source"
ok "kernel picks ECC from the OOB size (64 bytes -> 4-bit, 128 bytes -> 8-bit)"
if [ "$variant" = official ]; then
	grep -q 'dsa_shared_port_open_conduit' "$K/net/dsa/port.c" || fail "WAN conduit fix missing from the kernel source"
	ok "WAN port conduit fix is in the kernel"
fi

# 3. image layout and size
member="$(tar -tf "$IMG" 2>/dev/null | sed -n 's#/CONTROL$##p' | head -n1)"
[ -n "$member" ] || fail "sysupgrade image is not a tar with a CONTROL member"
ksize="$(tar -xOf "$IMG" "$member/kernel" 2>/dev/null | wc -c)"
rsize="$(tar -xOf "$IMG" "$member/root" 2>/dev/null | wc -c)"
[ "$ksize" -gt 1000000 ] && [ "$ksize" -le "$MAX_KERNEL" ] || fail "kernel size $ksize is outside 1 MB..$MAX_KERNEL"
[ "$rsize" -gt 1000000 ] || fail "root filesystem size $rsize is too small"
ok "layout $member: kernel $ksize bytes, root $rsize bytes"
tail -c 4096 "$IMG" | grep -aq 'xiaomi,redmi-ax5400' || fail "image metadata does not name xiaomi,redmi-ax5400"
ok "image metadata names the device xiaomi,redmi-ax5400"

# 4. every requested package is in the image
MISSING=""
for p in $(grep -hv '^[[:space:]]*#' "$ROOT/config/packages-common.txt" "$ROOT/config/packages-$variant.txt"); do
	grep -q "^$p " "$OUT/packages.manifest" || MISSING="$MISSING $p"
done
[ -z "$MISSING" ] || fail "packages missing from the image:$MISSING"
ok "all requested packages are in the image ($(wc -l < "$OUT/packages.manifest") packages in total)"
if [ "$variant" = nss ] || [ "$variant" = next ]; then
	grep -q '^kmod-qca-nss-drv ' "$OUT/packages.manifest" || fail "NSS driver missing from the NSS image"
	ok "NSS driver is in the image"
fi

(cd "$OUT" && sha256sum "$(basename "$IMG")" packages.manifest build.config build-info.txt image-check.txt checks.txt > SHA256SUMS)
echo "image=$(basename "$IMG")" >> "$ROOT/build-info.txt"
cat "$OUT/SHA256SUMS"
echo VERIFY_OK
