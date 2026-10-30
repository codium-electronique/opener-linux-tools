#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique
#
# Sets up the NR+Gate Linux host for the Spinel SPI transport: installs the
# udev rule binding the nRF9151 SPI link to spidev, applies it without a
# reboot, and checks the user can open the SPI device and the IRQ and BUSY
# GPIO chips.
#
# Usage: sudo install.sh
set -euo pipefail

RULE=99-nrp-spi.rules
SPI_DEVICE=spi3.0
SPIDEV=/dev/spidev3.0
GPIOCHIPS=(/dev/gpiochip1 /dev/gpiochip4)

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run it with sudo" >&2
    exit 1
fi
USER_NAME="${SUDO_USER:-root}"
HERE="$(dirname "$(realpath "$0")")"

install -m 0644 "$HERE/$RULE" "/etc/udev/rules.d/$RULE"
echo "installed /etc/udev/rules.d/$RULE"

udevadm control --reload
udevadm trigger --action=add "/sys/bus/spi/devices/$SPI_DEVICE"
udevadm settle

if [ ! -c "$SPIDEV" ]; then
    echo "error: $SPIDEV missing after the udev trigger" >&2
    exit 1
fi
ls -l "$SPIDEV"

status=0
for path in "$SPIDEV" "${GPIOCHIPS[@]}"; do
    # Open it read-write as the user, as the tools do.
    if sudo -u "$USER_NAME" sh -c 'exec 3<>"$1"' sh "$path"; then
        echo "check ok: $USER_NAME can open $path"
    else
        echo "error: $USER_NAME cannot open $path" >&2
        status=1
    fi
done
exit $status
