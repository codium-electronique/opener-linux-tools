#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique
#
# Installs nrp-sniffer as a Wireshark extcap: writes a wrapper running the
# nrp-sniffer of the active environment into the extcap folder.
#
# Usage: install-extcap.sh [EXTCAP_DIR]
#   EXTCAP_DIR  target folder (default: ~/.local/lib/wireshark/extcap)
#
# Run it with the venv where opener-tools is installed active, or set
# NRP_SNIFFER to the nrp-sniffer to run.
set -euo pipefail

EXTCAP_DIR="${1:-$HOME/.local/lib/wireshark/extcap}"
NRP_SNIFFER="${NRP_SNIFFER:-$(command -v nrp-sniffer || true)}"

if [ -z "$NRP_SNIFFER" ]; then
    echo "error: nrp-sniffer not found; activate the venv where opener-tools is installed" >&2
    exit 1
fi
NRP_SNIFFER="$(realpath -s "$NRP_SNIFFER")"

mkdir -p "$EXTCAP_DIR"
WRAPPER="$EXTCAP_DIR/nrp-sniffer"
# Wireshark starts extcaps with its own environment: name the venv's
# nrp-sniffer, whose interpreter is that of the venv.
printf '#!/bin/sh\nexec %q "$@"\n' "$NRP_SNIFFER" >"$WRAPPER"
chmod +x "$WRAPPER"
echo "installed $WRAPPER -> $NRP_SNIFFER"

if "$WRAPPER" --extcap-interfaces >/dev/null; then
    echo "check ok: the extcap answers"
else
    echo "warning: the extcap did not answer --extcap-interfaces" >&2
    exit 1
fi
