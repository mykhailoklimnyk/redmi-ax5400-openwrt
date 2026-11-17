#!/usr/bin/env python3
"""DHCP + TFTP feeder for the Xiaomi bootloader recovery mode (Redmi AX5400 / RA74).

Recovery mode (hold Reset while powering on, ~20 s): the bootloader pings its server,
asks for an address over DHCP, downloads a stock firmware over TFTP and flashes it.
This script is the PC side: it answers DHCP, names itself as the TFTP server
(siaddr + option 66/67) and sends the firmware whatever file name is requested.

Usage (PC wired to a router LAN port, PC address 192.168.31.100/24, no gateway):
  python recovery_server.py                    # serves firmware/stock/miwifi_ra74_..._1.0.63.bin
  python recovery_server.py <file> [--server IP] [--client IP] [--mac 44:df:65]
  python recovery_server.py --selftest         # loopback test, sends nothing to the network

Only the standard library is used. Everything is logged to recovery.log next to the script.
"""
import argparse
import hashlib
import os
import select
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FW = os.path.normpath(os.path.join(HERE, "..", "..", "firmware", "stock", "miwifi_ra74_firmware_7937d_1.0.63.bin"))
KNOWN_MD5 = {"9e219468394bcaf3ab5677b99697937d": "RA74 1.0.63 (official)"}
MAGIC_COOKIE = b"\x63\x82\x53\x63"
RRQ, DATA, ACK, ERROR, OACK = 1, 3, 4, 5, 6
_lock = threading.Lock()
_logfile = os.path.join(HERE, "recovery.log")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with _lock:
        print(line, flush=True)
        try:
            with open(_logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def parse_options(raw):
    opts, i = {}, 0
    while i < len(raw):
        code = raw[i]
        if code == 0:
            i += 1
            continue
        if code == 255 or i + 1 >= len(raw):
            break
        ln = raw[i + 1]
        opts[code] = raw[i + 2:i + 2 + ln]
        i += 2 + ln
    return opts


def build_reply(req, mtype, cfg):
    bootfile = cfg["bootfile"].encode()[:127]
    p = bytearray()
    p += bytes([2, 1, 6, 0]) + req[4:8] + b"\x00\x00" + req[10:12]   # op htype hlen hops xid secs flags
    p += b"\x00" * 4                                                 # ciaddr
    p += socket.inet_aton(cfg["client"])                             # yiaddr
    p += socket.inet_aton(cfg["server"])                             # siaddr = TFTP server
    p += req[24:28]                                                  # giaddr
    p += req[28:44]                                                  # chaddr
    p += b"\x00" * 64                                                # sname
    p += bootfile + b"\x00" * (128 - len(bootfile))                  # file
    p += MAGIC_COOKIE
    for code, val in (
        (53, bytes([mtype])),
        (54, socket.inet_aton(cfg["server"])),
        (51, struct.pack("!I", 3600)),
        (1, socket.inet_aton(cfg["mask"])),
        (66, cfg["server"].encode()),
        (67, bootfile),
    ):
        p += bytes([code, len(val)]) + val
    p += b"\xff"
    p += b"\x00" * max(0, 300 - len(p))
    return bytes(p)


def dhcp_loop(cfg, stop):
    socks = []
    for addr in dict.fromkeys(["0.0.0.0", cfg["server"]]):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind((addr, cfg["dhcp_port"]))
        except OSError as e:
            log("DHCP: cannot bind %s:%d (%s)" % (addr, cfg["dhcp_port"], e))
            if addr == cfg["server"]:
                cfg["fatal"] = "no interface has address %s, or the DHCP port is taken" % cfg["server"]
                stop.set()
                return
            continue
        socks.append(s)
    tx = socks[-1]                      # bound to the server address: replies leave via that interface
    log("DHCP ready on %s:%d, will hand out %s / %s" % (cfg["server"], cfg["dhcp_port"], cfg["client"], cfg["mask"]))
    seen, ignored = {}, set()
    while not stop.is_set():
        ready, _, _ = select.select(socks, [], [], 1.0)
        for s in ready:
            try:
                pkt, addr = s.recvfrom(2048)
            except OSError:
                continue
            if len(pkt) < 240 or pkt[0] != 1 or pkt[236:240] != MAGIC_COOKIE:
                continue
            opts = parse_options(pkt[240:])
            mtype = opts.get(53, b"\x00")[0]
            mac = ":".join("%02x" % b for b in pkt[28:34])
            key = (pkt[4:8], mtype)
            now = time.time()
            if now - seen.get(key, 0) < 0.3:      # same datagram seen on the second socket
                continue
            seen[key] = now
            who = "%s vendor=%r host=%r" % (mac, opts.get(60, b"").decode("ascii", "replace"), opts.get(12, b"").decode("ascii", "replace"))
            reason = None
            if cfg["macs"] and not any(mac.startswith(m) for m in cfg["macs"]):
                reason = "MAC filter"
            elif any(mac.startswith(m) for m in cfg["ignore"]):
                reason = "known home device"
            elif opts.get(60, b"").lower().startswith((b"udhcp", b"msft", b"android", b"dhcpcd")) or 12 in opts:
                reason = "a running OS, not a bootloader"     # e.g. the stock firmware asking on its WAN port
            if reason:
                if mac not in ignored:          # one line per device, not per packet
                    ignored.add(mac)
                    log("DHCP: ignored (%s) %s" % (reason, who))
                continue
            if mtype == 1:
                reply, name = build_reply(pkt, 2, cfg), "DISCOVER -> OFFER"
            elif mtype == 3:
                reply, name = build_reply(pkt, 5, cfg), "REQUEST -> ACK"
            else:
                continue
            log("DHCP: %s  %s" % (name, who))
            for dst in cfg["reply_to"]:
                try:
                    tx.sendto(reply, (dst, cfg["dhcp_client_port"]))
                except OSError as e:
                    log("DHCP: send to %s failed (%s)" % (dst, e))
    for s in socks:
        s.close()


def tftp_transfer(client, request, cfg):
    parts = request.split(b"\x00")
    name = parts[0].decode("ascii", "replace")
    opts = {}
    for i in range(2, len(parts) - 1, 2):
        if parts[i]:
            opts[parts[i].decode("ascii", "replace").lower()] = parts[i + 1].decode("ascii", "replace")
    data = cfg["data"]
    total = len(data)
    blk = 512
    try:
        blk = max(8, min(65464, int(opts.get("blksize", 512))))
    except ValueError:
        pass
    log("TFTP: %s:%d asks for %r, options %s -> sending %s (%d bytes, block %d)" % (client[0], client[1], name, opts, cfg["bootfile"], total, blk))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((cfg["server"], 0))
    s.settimeout(1.0)

    def wait_ack(block, packet):
        """Send `packet` and wait for the matching ACK; duplicate ACKs do not trigger a resend."""
        for _ in range(15):
            s.sendto(packet, client)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    resp, peer = s.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    return False
                if peer != client or len(resp) < 4:
                    continue
                op, num = struct.unpack("!HH", resp[:4])
                if op == ERROR:
                    log("TFTP: client error %d %r" % (num, resp[4:].rstrip(b"\x00")))
                    return False
                if op == ACK and num == block:
                    return True
        return False

    try:
        negotiated = [k for k in ("blksize", "tsize", "timeout") if k in opts]
        if negotiated:
            body = b""
            for k in negotiated:
                v = str(blk) if k == "blksize" else str(total) if k == "tsize" else opts[k]
                body += k.encode() + b"\x00" + v.encode() + b"\x00"
            if not wait_ack(0, struct.pack("!H", OACK) + body):
                log("TFTP: option acknowledgement was not confirmed, transfer aborted")
                return
        blocks = total // blk + 1          # a final short (possibly empty) block ends the transfer
        t0 = time.time()
        for n in range(1, blocks + 1):
            chunk = data[(n - 1) * blk:n * blk]
            if not wait_ack(n & 0xFFFF, struct.pack("!HH", DATA, n & 0xFFFF) + chunk):
                log("TFTP: FAILED at block %d of %d" % (n, blocks))
                return
            if n % 5000 == 0:
                log("TFTP: %d%%" % (n * 100 // blocks))
        log("TFTP: DONE, %d bytes in %.1f s" % (total, time.time() - t0))
        if not cfg["selftest"]:
            log(">>> The router now erases and writes flash. DO NOT cut power for at least 5 minutes.")
            log(">>> If nothing answers on 192.168.31.1 after 10 minutes, power-cycle the router once.")
        cfg["transfers"] += 1
    finally:
        s.close()


def tftp_loop(cfg, stop):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((cfg["tftp_bind"], cfg["tftp_port"]))
    except OSError as e:
        log("TFTP: cannot bind port %d (%s)" % (cfg["tftp_port"], e))
        cfg["fatal"] = "TFTP port is taken"
        stop.set()
        return
    s.settimeout(1.0)
    log("TFTP ready on port %d" % cfg["tftp_port"])
    while not stop.is_set():
        try:
            pkt, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            continue
        if len(pkt) >= 4 and struct.unpack("!H", pkt[:2])[0] == RRQ:
            threading.Thread(target=tftp_transfer, args=(addr, pkt[2:], cfg), daemon=True).start()
    s.close()


def selftest(cfg):
    """Loopback-only: a fake client does DISCOVER/REQUEST and two TFTP downloads, results are compared."""
    cfg.update(server="127.0.0.1", client="127.0.0.9", reply_to=["127.0.0.1"], tftp_bind="127.0.0.1",
               dhcp_port=16767, dhcp_client_port=16768, tftp_port=16969, macs=["02:00:00"], selftest=True)
    stop = threading.Event()
    for fn in (dhcp_loop, tftp_loop):
        threading.Thread(target=fn, args=(cfg, stop), daemon=True).start()
    time.sleep(0.5)
    ok = True
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.bind(("127.0.0.1", 16768))
    c.settimeout(3.0)
    mac = bytes.fromhex("020000aabbcc")
    for mtype, expect in ((1, 2), (3, 5)):
        req = bytes([1, 1, 6, 0]) + b"TEST" + b"\x00" * 20 + mac + b"\x00" * 10 + b"\x00" * 192 + MAGIC_COOKIE + bytes([53, 1, mtype, 255])
        c.sendto(req, ("127.0.0.1", 16767))
        rep, _ = c.recvfrom(2048)
        o = parse_options(rep[240:])
        good = (rep[0] == 2 and rep[4:8] == b"TEST" and rep[16:20] == socket.inet_aton("127.0.0.9")
                and rep[20:24] == socket.inet_aton("127.0.0.1") and rep[28:34] == mac and o[53][0] == expect
                and o[66] == b"127.0.0.1" and o[67].decode() == cfg["bootfile"]
                and rep[108:108 + len(o[67])] == o[67])
        print("selftest DHCP type %d -> %d: %s" % (mtype, expect, "OK" if good else "FAIL"))
        ok = ok and good
    # a foreign MAC must be ignored
    req = bytes([1, 1, 6, 0]) + b"OTHR" + b"\x00" * 20 + bytes.fromhex("aabbccddeeff") + b"\x00" * 10 + b"\x00" * 192 + MAGIC_COOKIE + bytes([53, 1, 1, 255])
    c.sendto(req, ("127.0.0.1", 16767))
    c.settimeout(1.0)
    try:
        c.recvfrom(2048)
        print("selftest MAC filter: FAIL (answered a foreign MAC)")
        ok = False
    except socket.timeout:
        print("selftest MAC filter: OK")
    c.close()

    want = hashlib.sha256(cfg["data"]).hexdigest()
    for label, optbytes in (("plain 512", b""), ("blksize 1468 + tsize", b"blksize\x001468\x00tsize\x000\x00")):
        t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t.bind(("127.0.0.1", 0))
        t.settimeout(5.0)
        t.sendto(struct.pack("!H", RRQ) + b"anything.bin\x00octet\x00" + optbytes, ("127.0.0.1", 16969))
        got, blk, expect_block, peer = bytearray(), 512, 1, None
        while True:
            pkt, peer = t.recvfrom(70000)
            op, num = struct.unpack("!HH", pkt[:4])
            if op == OACK:
                fields = pkt[2:].split(b"\x00")
                kv = dict(zip(fields[0::2], fields[1::2]))
                blk = int(kv[b"blksize"])
                assert int(kv[b"tsize"]) == len(cfg["data"])
                t.sendto(struct.pack("!HH", ACK, 0), peer)
                continue
            if op != DATA:
                break
            if num == expect_block & 0xFFFF:
                got += pkt[4:]
                expect_block += 1
            t.sendto(struct.pack("!HH", ACK, num), peer)
            if len(pkt) - 4 < blk:
                break
        t.close()
        good = hashlib.sha256(got).hexdigest() == want
        print("selftest TFTP %s: %d bytes, sha256 %s" % (label, len(got), "OK" if good else "MISMATCH"))
        ok = ok and good
    stop.set()
    time.sleep(1.2)
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", nargs="?", default=DEFAULT_FW)
    ap.add_argument("--server", default="192.168.31.100", help="address of this PC on the wired interface")
    ap.add_argument("--client", default="192.168.31.1", help="address handed to the router")
    ap.add_argument("--mask", default="255.255.255.0")
    ap.add_argument("--mac", action="append", default=[], help="answer only MACs with this prefix (repeatable)")
    ap.add_argument("--ignore-macs", metavar="FILE", help="file with MACs or MAC prefixes (one per line) that must never get an answer")
    ap.add_argument("--any-file", action="store_true", help="serve a file whose MD5 is not the known stock one")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.firmware):
        print("firmware not found:", a.firmware)
        return 1
    data = open(a.firmware, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    print("firmware: %s\n  %d bytes, md5 %s (%s)" % (a.firmware, len(data), md5, KNOWN_MD5.get(md5, "UNKNOWN")))
    if md5 not in KNOWN_MD5 and not a.any_file:
        print("refusing: not the known official stock image (use --any-file to override)")
        return 1
    if data[:4] != b"HDR1" and not a.any_file:
        print("refusing: no Xiaomi HDR1 header; recovery mode accepts only stock images")
        return 1

    try:
        last = socket.inet_ntoa(struct.pack("!I", struct.unpack("!I", socket.inet_aton(a.server))[0] | (~struct.unpack("!I", socket.inet_aton(a.mask))[0] & 0xFFFFFFFF)))
    except OSError:
        print("bad --server or --mask")
        return 1
    ignore = []
    if a.ignore_macs:
        for ln in open(a.ignore_macs, encoding="utf-8"):
            ln = ln.split("#")[0].strip().lower().replace("-", ":")
            if ln:
                ignore.append(ln)
        print("  ignoring %d known MACs/prefixes" % len(ignore))
    cfg = {
        "ignore": ignore,
        "data": data, "bootfile": os.path.basename(a.firmware), "server": a.server, "client": a.client, "mask": a.mask,
        "macs": [m.lower() for m in a.mac], "reply_to": ["255.255.255.255", last], "tftp_bind": "0.0.0.0",
        "dhcp_port": 67, "dhcp_client_port": 68, "tftp_port": 69, "selftest": False, "transfers": 0, "fatal": None,
    }
    if a.selftest:
        return selftest(cfg)

    stop = threading.Event()
    for fn in (dhcp_loop, tftp_loop):
        threading.Thread(target=fn, args=(cfg, stop), daemon=True).start()
    time.sleep(0.7)
    if cfg["fatal"]:
        print("\nCANNOT START: %s\nSet the wired adapter to %s/%s (no gateway) and stop other DHCP/TFTP servers "
              "(Internet Connection Sharing uses port 67)." % (cfg["fatal"], a.server, a.mask))
        return 1
    print("\nReady. Router: power off -> hold Reset -> power on -> keep holding 20 s -> release.")
    print("Expected below: DHCP DISCOVER/REQUEST, then TFTP ... DONE. Ctrl-C to stop.\n")
    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    print("\nstopped; completed transfers: %d" % cfg["transfers"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
