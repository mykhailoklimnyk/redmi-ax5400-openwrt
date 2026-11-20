# OpenWrt builds for the Redmi AX5400 (RA74)

Automated, checked and signed OpenWrt images for the Xiaomi **Redmi AX5400, model RA74**
(IPQ5018, 512 MB RAM, 128 MB NAND), with over-the-air updates from this repository's releases.

Not for the Redmi AX5400 *Gaming* (RB04) or any other router.

> **This router has no second firmware slot once OpenWrt is installed.** An image that does not
> boot is repaired only through the vendor recovery mode (Reset held while powering on, see
> [docs/RECOVERY.md](docs/RECOVERY.md)), which puts the stock firmware back. Read this page fully
> before flashing anything. You flash at your own risk.

## Why this exists

Two problems make the plain OpenWrt snapshot unsafe or slow on this model:

1. **Two different NAND chips are sold under the same model.** Units with a 128-byte-OOB chip
   need 8-bit ECC, units with a 64-byte-OOB chip (for example ESMT F50D1G41LB) need 4-bit.
   OpenWrt hardcodes 8-bit, so a 64-byte unit boot-loops after an apparently successful
   `sysupgrade` ([openwrt#26663](https://github.com/openwrt/openwrt/issues/26663)).
   Images here carry the fix from [openwrt PR #24763](https://github.com/openwrt/openwrt/pull/24763):
   the kernel picks the ECC from the chip's OOB size, so one image fits both chips.
2. **The WAN port and gigabit speed.** In plain OpenWrt the WAN port does not pass traffic
   ([openwrt#26157](https://github.com/openwrt/openwrt/issues/26157)) and routing tops out around
   500 Mbit/s. The NSS image uses Qualcomm's network accelerator and reaches gigabit.

## The two images

| | `nss` | `official` |
| --- | --- | --- |
| Base | [Yongzhaooo/redmi-ax5400-nss](https://github.com/Yongzhaooo/redmi-ax5400-nss) at a pinned, reviewed commit | current [openwrt/openwrt](https://github.com/openwrt/openwrt) `main` |
| Changes on top | ECC by OOB size (PR #24763) | ECC by OOB size (PR #24763), WAN port fix ([PR #26511](https://github.com/openwrt/openwrt/pull/26511)) |
| Speed | gigabit wired, Wi-Fi acceleration available | about half a gigabit |
| Closed-source parts | Qualcomm NSS firmware | none beyond what OpenWrt ships |
| Ports | WAN `eth0.2`, LAN `eth1.1` | standard OpenWrt DSA ports |

There is also an experimental third image, `next`: the current NSS base for IPQ5018
([kuncy7/openwrt-nss-edma](https://github.com/kuncy7/openwrt-nss-edma), which the `nss` source was
forked from) at one of its release tags, with a newer NSS firmware and many Wi-Fi offload fixes.
That project's own image for this router keeps a fixed 8-bit ECC in the device tree; the `next`
image here removes it, like the other two. It runs on the maintainer's 64-byte-OOB unit since
2026-11-18 (updated from `nss` with settings kept, which carries the `nss` port layout over); with
its newer 2.4 GHz Wi-Fi firmware, clients re-join normally with Wi-Fi offload on, which the `nss`
image does not manage. Its default port layout differs from `nss`, and a clean install and
128-byte-OOB units have not been tried by this project.

All images add the same package set ([config/](config/)): LuCI with the Argon theme, WireGuard,
policy-based routing, DNS-over-HTTPS, ad blocking, banIP, watchcat and Prometheus exporters.
The interface languages are English and Ukrainian. No home-specific settings are baked in.

The two images use different port layouts. **Do not switch between them with settings kept**;
the updater never does. Switching means a clean install.

Measured on one 64-byte-OOB unit with the `nss` base and a fixed 4-bit ECC (the predecessor of
these builds): 940/920 Mbit/s wired, 700+ Mbit/s on a Wi-Fi 6 client at 80 MHz, stable memory
over the first night. Wi-Fi acceleration is off by default in the NSS base; it is switched on
with `uci set nss.general.wifi_offload=1; uci commit nss; reboot`.

## Releases: candidate and stable

- Every build that passes the checks is published as a **pre-release** ("candidate"). Nobody has
  run it on hardware yet.
- A candidate is marked as a normal release ("stable") by hand, after it has run on a real router
  for several days.
- Release tags are `<variant>-<date>-<run>`, for example `nss-20261117-12`.

Each release holds the image, `SHA256SUMS`, its signature `SHA256SUMS.sig`, the package list,
the build configuration and `checks.txt` with the result of every check.

### What is checked before a release is published

- the device tree carries no fixed ECC strength, and the FIT hashes and the sysupgrade metadata
  trailer are valid ([tools/patch_ecc.py](tools/patch_ecc.py));
- the ECC rule (and for `official` the WAN fix) is present in the kernel source that was compiled;
- the image is a sysupgrade archive for `xiaomi,redmi-ax5400` and the kernel fits its partition;
- every requested package is in the image;
- for `nss`, the base project's own static checks pass.

These checks prove the image is built correctly. They cannot prove that it works on hardware.

## Updating over the air

Images also carry a small watchdog, `dns-lan-check`, run from cron once a minute: it restarts
dnsmasq if it stops answering on the LAN address (seen once on the `nss` image, cause not yet
found) and saves diagnostics to `/root/dns-lan-check/` first.

Images contain `ax5400-ota`:

```sh
ax5400-ota check      # is there a newer release of my variant? changes nothing
ax5400-ota download   # fetch to RAM and verify; changes nothing on flash
ax5400-ota install    # verify, ask, flash with settings kept
```

Before flashing it verifies the signature of `SHA256SUMS` against the public key shipped in the
image, the image checksum, that the image has no fixed ECC strength, and `sysupgrade -T`.
Any failure stops the update.

By default only stable releases are offered. To follow candidates as well:

```sh
echo 'OTA_CHANNEL=candidate' >> /etc/ax5400-ota.conf
```

Nothing updates by itself. For a daily check that only writes to the system log, add to
`/etc/crontabs/root`: `17 4 * * * /usr/sbin/ax5400-ota check >/dev/null 2>&1`.

Do not upgrade single packages on the router and do not install `kmod-*` packages from the
official feeds: the images have their own kernel. Updates are whole images.

## First installation from the stock firmware

Short form; the details and the traps are in the linked projects.

1. Get SSH on the stock firmware with [xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher)
   and **back up every partition** first.
2. Find out which NAND you have (`dmesg | grep -i nand` on stock, or the notes in
   [843901020/RedMi-AX5400-openwrt-esmt](https://github.com/843901020/RedMi-AX5400-openwrt-esmt)).
   With a 64-byte-OOB chip the official OpenWrt initramfs must have its ECC patched before it is
   written: `python3 tools/patch_ecc.py <in.ubi> <out.ubi> --strength 4`.
3. Write the OpenWrt initramfs image to the inactive slot, boot it, then `sysupgrade` to an image
   from this repository. A `sysupgrade` from the initramfs does not keep settings.
4. Prepare the recovery path ([docs/RECOVERY.md](docs/RECOVERY.md)) **before** step 3, and keep the
   provider cable unplugged until the WAN port is configured.

A ready-made first-install image is not published yet.

## Building it yourself

On Debian/Ubuntu with the usual OpenWrt build dependencies:

```sh
scripts/prepare.sh nss        # or: official
scripts/build.sh              # 1.5-4 hours, about 15 GB of disk
scripts/verify.sh             # image and check results in ./out
```

In GitHub Actions: **Actions → Build firmware → Run workflow**. A monthly schedule builds all
three images, and a weekly job opens an issue when a pinned source has a new release or one of the
carried pull requests is merged or closed. To sign releases, generate a key pair with
`usign -G -s secret.key -p files/etc/ax5400-ota/pubkey`, commit the public key and store the
content of `secret.key` as the repository secret `USIGN_SECRET_KEY`.

## Credits and licences

- OpenWrt and its contributors; the RA74 port ([openwrt PR #23374](https://github.com/openwrt/openwrt/pull/23374)).
- [Yongzhaooo/redmi-ax5400-nss](https://github.com/Yongzhaooo/redmi-ax5400-nss) and its base
  [kuncy7/openwrt-nss-edma](https://github.com/kuncy7/openwrt-nss-edma), NSS packages from
  [qosmio](https://github.com/qosmio/nss-packages).
- The authors of openwrt PR #24763 and PR #26511, whose patches are carried in [patches/](patches/).
- [843901020/RedMi-AX5400-openwrt-esmt](https://github.com/843901020/RedMi-AX5400-openwrt-esmt)
  for the write-up of the two NAND variants.
- [jerrykuku/luci-theme-argon](https://github.com/jerrykuku/luci-theme-argon) (Apache-2.0).

The scripts and tools in this repository are licensed under GPL-2.0 ([LICENSE](LICENSE)).
The `nss` image contains Qualcomm NSS firmware, which is closed-source and stays under Qualcomm's
terms; it is downloaded at build time from the source named by the base project and is not stored
in this repository.
