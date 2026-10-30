#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique
#
# Cargo runner for aarch64-unknown-linux-gnu: copies the binary to the
# NR+Gate and runs it there with the same arguments. NRP_GATEWAY overrides
# the ssh destination.
set -euo pipefail

host="${NRP_GATEWAY:-codium@172.16.1.134}"
bin="$1"
shift
name="$(basename "$bin")"
dir=".cache/nrp-run"

ssh "$host" mkdir -p "$dir"
rsync -a "$bin" "$host:$dir/$name"

args=""
for arg in "$@"; do
	args+=" $(printf '%q' "$arg")"
done

tty_flag=""
if [ -t 0 ]; then
	tty_flag="-t"
fi
exec ssh $tty_flag "$host" "$dir/$name$args"
