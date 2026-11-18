#!/bin/bash
# Download the package sources and build the image prepared by scripts/prepare.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${SRC:-$ROOT/src}"
LOGS="${LOGS:-$ROOT/logs}"
JOBS="${JOBS:-$(nproc)}"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
mkdir -p "$LOGS"
cd "$SRC"

echo "downloading sources ($(date -u +%H:%M))"
make -j"$JOBS" download > "$LOGS/download.log" 2>&1 || make -j2 download >> "$LOGS/download.log" 2>&1 || {
	tail -n 40 "$LOGS/download.log"; echo "FAIL: download" >&2; exit 1; }

echo "building with $JOBS jobs ($(date -u +%H:%M))"
if ! make -j"$JOBS" > "$LOGS/make.log" 2>&1; then
	echo "parallel build failed, running again single-threaded with a full log"
	if ! make -j1 V=s > "$LOGS/make-v.log" 2>&1; then
		tail -n 150 "$LOGS/make-v.log"
		echo "FAIL: build" >&2
		exit 1
	fi
fi
echo "BUILD_OK ($(date -u +%H:%M))"
