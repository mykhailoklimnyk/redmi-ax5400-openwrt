# Recovery: back to the stock firmware

The Xiaomi bootloader has a recovery mode that needs no UART and no opened case. It is the only
way back when an OpenWrt image does not boot, so rehearse it before you need it.

## What happens

1. The bootloader takes the address `192.168.31.1`, looks for a PC at `192.168.31.100` on a LAN
   port and asks it for an address over DHCP.
2. It downloads the stock firmware over TFTP (it asks for
   `miwifi_ra74_firmware_7937d_1.0.63.bin`) and writes it.
3. It does **not** reboot by itself when it is done.

The result is a factory-reset stock firmware. From there the whole installation is repeated.

## Steps

1. Download the stock firmware 1.0.63 for the RA74 from Xiaomi. MD5 of the file:
   `9e219468394bcaf3ab5677b99697937d`.
2. Connect the PC by cable to a LAN port of the router. Unplug everything else, the provider
   cable included.
3. Give the wired interface of the PC the static address `192.168.31.100/24`, no gateway.
   Switch off Wi-Fi on the PC or make sure it does not use the same subnet.
4. Start the server (Python 3, standard library only; needs administrator/root rights for
   ports 67 and 69, and the firewall must let them through):

   ```sh
   python3 tools/recovery/recovery_server.py /path/to/miwifi_ra74_firmware_7937d_1.0.63.bin
   ```

5. Unplug the router's power. **Press and hold Reset first, then plug the power in**, and keep
   holding for about 20 seconds. Holding Reset on a running router does something else.
6. The server log shows the DHCP request and then the TFTP transfer. If nothing arrives, the
   router simply booted normally: repeat step 5.
7. After the transfer wait about 13 minutes, then unplug and replug the power. The stock setup
   wizard appears on `192.168.31.1`.

`recovery_server.py --help` lists options for other addresses and for limiting answers to the
router's MAC prefix, which matters if other devices share the cable segment.
