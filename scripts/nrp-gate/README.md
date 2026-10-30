# NR+Gate host setup

`install.sh` prepares the gateway's Linux host for the Spinel SPI transport
(see [docs/nrp-sniffer.md](../../docs/nrp-sniffer.md#on-the-nrgate)). Run it
once with sudo; it installs `99-nrp-spi.rules`, which binds `spi3.0` to
spidev at every boot.

## Changes for the gateway image

Changes made by hand on the gateway (Ubuntu 26.04, kernel 7.0) that the
gateway image should include:

- `99-nrp-spi.rules` in `/etc/udev/rules.d` (or a kernel driver binding
  `codium,nrp-spi` to spidev, which makes the rule's first line unneeded).
- Packages `python3-dev` (to build `spidev` with pip) and `rsync` (to deploy
  linux-tools from a PC). Alternatively ship `spidev` and `gpiod` 2.x
  prebuilt.

Seen on the gateway, not changed:

- `/etc/udev/rules.d/60-gpio.rules` gives the GPIO chips to the `gpio` group,
  but `/usr/lib/udev/rules.d/60-gpiochip.rules` sorts after it and sets
  `plugdev`: the chips end up `root:plugdev`. It works because the `codium`
  user is in both groups; a rule name sorting after `60-gpiochip.rules`
  would make `gpio` win.
- `openocd -f cod-nrp-gw.cfg -f cod-nrp-gw-rtt.cfg` exits when the running
  image has no RTT control block (`rtt start` fails), which is the case of
  `dectnrp-spinel` on the gateway (its logs go to `/dev/ttyS3`). Start
  OpenOCD without `cod-nrp-gw-rtt.cfg` then, or make `rtt start` non-fatal
  in that file.
- `sudo -u codium test -r /dev/gpiochip1` fails under sudo-rs 0.2.13 although
  `codium` can open it; `install.sh` opens the devices through `sh` instead.
