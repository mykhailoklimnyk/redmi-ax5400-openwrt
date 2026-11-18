#!/usr/bin/env python3
"""Check (and optionally patch) the NAND ECC setting in OpenWrt images for the Redmi AX5400 (RA74).

The upstream DTS hardcodes nand-ecc-strength = <8>; chips with OOB 64 need <4>
(openwrt/openwrt issue #26663). The property is one u32 cell, so the patch is done
in place: the cell itself plus the FIT hash values of the device-tree sub-image.

Supported inputs (detected automatically):
  *.ubi              UBI image whose dynamic volume holds the kernel FIT
  *sysupgrade.bin    tar with a ".../kernel" FIT member plus trailing metadata
  raw FIT            file that starts with the FDT magic

Usage:
  python patch_ecc.py <in> <out> [--strength 4]
  python patch_ecc.py --check <file>        # only print current values, verify hashes
  python patch_ecc.py --check <file> --expect auto   # fail unless the image has no fixed strength
  python patch_ecc.py --check <file> --expect 4      # fail unless the fixed strength is 4

"auto" means the device tree carries no nand-ecc-strength / nand-ecc-step-size at all, so the
kernel picks the ECC from the chip's OOB size (openwrt/openwrt PR #24763).

The output is re-read and verified; the tool exits non-zero if anything is unexpected.
"""
import argparse
import hashlib
import struct
import sys
import tarfile
import zlib

FDT_MAGIC = 0xD00DFEED
HASHERS = {
    "crc32": lambda b: struct.pack(">I", zlib.crc32(b) & 0xFFFFFFFF),
    "sha1": lambda b: hashlib.sha1(b).digest(),
    "sha256": lambda b: hashlib.sha256(b).digest(),
}


def be32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def walk(b, off):
    """Yield (node_path, prop_name, value_offset, value_len) for the FDT at `off`."""
    magic, _total, off_struct, off_strings, _rsv, _ver, _last, _boot, _sstr, size_struct = struct.unpack_from(">10I", b, off)
    if magic != FDT_MAGIC:
        raise ValueError("no FDT at 0x%x" % off)
    p = off + off_struct
    end = p + size_struct
    path = []
    while p < end:
        tok = be32(b, p)
        p += 4
        if tok == 1:  # BEGIN_NODE
            e = b.index(b"\x00", p)
            path.append(bytes(b[p:e]).decode("ascii", "replace"))
            p = (e + 1 + 3) & ~3
        elif tok == 2:  # END_NODE
            if path:
                path.pop()
        elif tok == 3:  # PROP
            ln = be32(b, p)
            nameoff = be32(b, p + 4)
            p += 8
            s = off + off_strings + nameoff
            e = b.index(b"\x00", s)
            yield "/".join(path), bytes(b[s:e]).decode("ascii", "replace"), p, ln
            p = (p + ln + 3) & ~3
        elif tok == 4:  # NOP
            continue
        elif tok == 9:  # END
            break
        else:
            raise ValueError("bad FDT token %d at 0x%x" % (tok, p - 4))


def cstr(b, off, ln):
    return bytes(b[off:off + ln]).rstrip(b"\x00").decode("ascii", "replace")


def process_fit(fit, strength, write):
    """Inspect (and optionally patch) a FIT held in bytearray `fit`. Returns a report dict."""
    props = list(walk(fit, 0))
    types = {path: cstr(fit, o, ln) for path, name, o, ln in props if name == "type"}
    algos = {path: cstr(fit, o, ln) for path, name, o, ln in props if name == "algo"}
    dts = [(path, o, ln) for path, name, o, ln in props if name == "data" and types.get(path) == "flat_dt"]
    if not dts:
        raise ValueError("FIT has no flat_dt sub-image")
    report = {"dtbs": []}
    for node, d_off, d_len in dts:
        cells = [(path, o) for path, name, o, ln in walk(fit, d_off) if name == "nand-ecc-strength" and ln == 4]
        eccprops = sorted({name for _p, name, _o, _l in walk(fit, d_off) if name.startswith("nand-ecc")})
        if not cells and write:
            raise ValueError("no nand-ecc-strength in %s (image picks ECC by itself, nothing to patch)" % node)
        model = [cstr(fit, o, ln) for path, name, o, ln in walk(fit, d_off) if path == "" and name == "model"]
        before = [be32(fit, o) for _, o in cells]
        if write:
            for _, o in cells:
                struct.pack_into(">I", fit, o, strength)
        blob = bytes(fit[d_off:d_off + d_len])
        hashes = []
        for path, name, o, ln in props:
            if name != "value" or not path.startswith(node + "/"):
                continue
            algo = algos.get(path)
            if algo not in HASHERS:
                raise ValueError("unsupported hash algo %r at %s" % (algo, path))
            digest = HASHERS[algo](blob)
            if len(digest) != ln:
                raise ValueError("hash length mismatch at %s" % path)
            if write:
                fit[o:o + ln] = digest
            hashes.append((path, algo, bytes(fit[o:o + ln]) == digest))
        if not hashes:
            raise ValueError("no hash nodes under %s" % node)
        report["dtbs"].append({
            "node": node, "model": model[0] if model else "?", "len": d_len, "eccprops": eccprops,
            "cells": [(p, b4, be32(fit, o)) for (p, o), b4 in zip(cells, before)],
            "hashes": hashes,
        })
    return report


class UbiVolume:
    """Maps the logical data of the kernel volume inside a UBI image file."""

    def __init__(self, img):
        self.img = img
        peb = None
        for cand in (0x20000, 0x40000, 0x10000, 0x80000):
            if img[:4] == b"UBI#" and img[cand:cand + 4] == b"UBI#":
                peb = cand
                break
        if peb is None:
            raise ValueError("cannot determine UBI PEB size")
        self.peb = peb
        vols = {}
        for p in range(0, len(img) - peb + 1, peb):
            if img[p:p + 4] != b"UBI#":
                continue
            vid_off, data_off = be32(img, p + 16), be32(img, p + 20)
            v = p + vid_off
            if img[v:v + 4] != b"UBI!":
                continue
            vol_type, copy_flag = img[v + 5], img[v + 6]
            vol_id, lnum = be32(img, v + 8), be32(img, v + 12)
            data_size, data_crc = be32(img, v + 20), be32(img, v + 32)
            vols.setdefault(vol_id, []).append((lnum, p + data_off, vol_type, copy_flag, data_size, data_crc))
            self.leb = peb - data_off
        self.vol_id = None
        for vid in sorted(v for v in vols if v < 0x7FFFEFFF):
            lebs = sorted(vols[vid])
            if img[lebs[0][1]:lebs[0][1] + 4] == struct.pack(">I", FDT_MAGIC):
                self.vol_id, self.lebs = vid, lebs
                break
        if self.vol_id is None:
            raise ValueError("no UBI volume starting with a FIT")
        for lnum, _off, vol_type, copy_flag, data_size, data_crc in self.lebs:
            if vol_type != 1 or copy_flag or data_size or data_crc:
                raise ValueError("kernel volume LEB %d is CRC-protected (static/copied); in-place patch unsafe" % lnum)
        if [l[0] for l in self.lebs] != list(range(len(self.lebs))):
            raise ValueError("kernel volume LEBs are not contiguous from 0")

    def read(self):
        return bytearray(b"".join(bytes(self.img[o:o + self.leb]) for _, o, *_ in self.lebs))

    def write(self, data):
        for i, (_, o, *_rest) in enumerate(self.lebs):
            self.img[o:o + self.leb] = data[i * self.leb:(i + 1) * self.leb]


def locate(img, path):
    """Return (kind, getter, setter) for the FIT inside the image."""
    if img[:4] == b"UBI#":
        vol = UbiVolume(img)
        return "ubi (vol %d, %d LEBs, PEB %d)" % (vol.vol_id, len(vol.lebs), vol.peb), vol.read, vol.write
    if be32(img, 0) == FDT_MAGIC:
        def setter(data):
            img[:len(data)] = data
        return "raw FIT", lambda: bytearray(img), setter
    with tarfile.open(path) as tf:
        members = [m for m in tf.getmembers() if m.name.endswith("/kernel")]
    if len(members) != 1:
        raise ValueError("expected exactly one */kernel member in tar, found %d" % len(members))
    m = members[0]
    if be32(img, m.offset_data) != FDT_MAGIC:
        raise ValueError("tar kernel member is not a FIT")

    def setter(data):
        assert len(data) == m.size
        img[m.offset_data:m.offset_data + m.size] = data
    return "sysupgrade tar (%s, %d bytes)" % (m.name, m.size), lambda: bytearray(img[m.offset_data:m.offset_data + m.size]), setter


def trailer_crc(img):
    """fwtool metadata trailer at the end of a sysupgrade image: (stored, computed) or None.

    The trailer is 16 bytes: "FWx0", crc32, type, pad, size. Its CRC covers every byte of the
    file before the trailer, so patching the kernel without updating it makes sysupgrade report
    "Image metadata not present".
    """
    if len(img) < 16 or bytes(img[-16:-12]) != b"FWx0":
        return None
    stored = be32(img, len(img) - 12)
    computed = ~zlib.crc32(bytes(img[:-16])) & 0xFFFFFFFF
    return stored, computed


def show_trailer(img):
    t = trailer_crc(img)
    if t is None:
        print("  metadata trailer: none")
        return True
    print("  metadata trailer crc: %s" % ("OK" if t[0] == t[1] else "MISMATCH (sysupgrade will not see the metadata)"))
    return t[0] == t[1]


def show(report):
    ok = True
    for d in report["dtbs"]:
        print("  dtb %s  model=%r  len=%d" % (d["node"], d["model"], d["len"]))
        print("    nand-ecc properties: %s" % (", ".join(d["eccprops"]) or "none"))
        if not d["cells"]:
            print("    nand-ecc-strength: not fixed (kernel picks it from the OOB size)")
        for path, before, after in d["cells"]:
            print("    %s nand-ecc-strength: %d%s" % (path, before, "" if before == after else " -> %d" % after))
        for path, algo, good in d["hashes"]:
            print("    %s %s: %s" % (path, algo, "OK" if good else "MISMATCH"))
            ok = ok and good
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--strength", type=int, default=4)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--expect", choices=["auto", "4", "8"], help="with --check: required ECC setting")
    a = ap.parse_args()
    if not a.check and not a.dst:
        ap.error("dst required unless --check")

    orig = open(a.src, "rb").read()
    img = bytearray(orig)
    kind, getter, setter = locate(img, a.src)
    print("%s: %s, %d bytes" % (a.src, kind, len(img)))
    fit = getter()

    if a.check:
        rep = process_fit(fit, a.strength, write=False)
        ok = show(rep)
        ok = show_trailer(img) and ok
        if kind.startswith("sysupgrade") and trailer_crc(img) is None:
            print("  ERROR: sysupgrade image without a metadata trailer")
            ok = False
        print("  sha256: %s" % hashlib.sha256(img).hexdigest())
        if a.expect:
            values = {after for d in rep["dtbs"] for _, _, after in d["cells"]}
            if a.expect == "auto":
                good = not values and all(d["eccprops"] == ["nand-ecc-engine"] for d in rep["dtbs"])
            else:
                good = values == {int(a.expect)}
            print("  expected ECC %s: %s" % (a.expect, "OK" if good else "FAILED"))
            ok = ok and good
        return 0 if ok else 1

    if not show(process_fit(fit, a.strength, write=False)):
        print("ERROR: input image already has inconsistent FIT hashes; refusing to patch")
        return 1
    print("patching ->", a.strength)
    process_fit(fit, a.strength, write=True)
    had_trailer = trailer_crc(img)
    if had_trailer and had_trailer[0] != had_trailer[1]:
        print("ERROR: input image has a bad metadata trailer crc; refusing to patch")
        return 1
    setter(fit)
    if had_trailer:
        struct.pack_into(">I", img, len(img) - 12, trailer_crc(img)[1])
    if len(img) != len(orig):
        print("ERROR: size changed")
        return 1
    open(a.dst, "wb").write(img)

    # Independent verification of what was written.
    out = bytearray(open(a.dst, "rb").read())
    kind2, getter2, _ = locate(out, a.dst)
    rep = process_fit(getter2(), a.strength, write=False)
    ok = show(rep)
    values = {after for d in rep["dtbs"] for _, _, after in d["cells"]}
    diff = sum(1 for x, y in zip(orig, out) if x != y)
    print("verify: %s, strengths=%s, changed bytes=%d, sha256=%s" % (kind2, sorted(values), diff, hashlib.sha256(out).hexdigest()))
    ok = show_trailer(out) and ok
    if not ok or values != {a.strength} or diff == 0 or diff > 64 * len(rep["dtbs"]) + 4:
        print("ERROR: verification failed")
        return 1
    print("OK:", a.dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
