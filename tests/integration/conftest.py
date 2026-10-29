# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Host tests against the native_sim simulated device of the opener repo.

OPENER_SIMDEV_EXE names its zephyr.exe, e.g.
../opener-samples/build/simdev/simdev/zephyr/zephyr.exe.
"""

import os
import re
import subprocess
import time

import pytest

# Printed by the native_sim pty UART driver for the Spinel UART (uart1).
PTY_LINE = re.compile(rb"uart_1 connected to pseudotty: (/dev/pts/\d+)")
START_TIMEOUT = 5.0


def wait_for_pty(log_path, proc):
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        returncode = proc.poll()
        if returncode is not None:
            raise RuntimeError(f"simdev exited with {returncode}")
        with open(log_path, "rb") as log:
            output = log.read()
        match = PTY_LINE.search(output)
        if match is not None:
            path = match.group(1)
            return path.decode()
        time.sleep(0.05)
    raise RuntimeError("simdev printed no uart_1 pty")


@pytest.fixture(scope="session")
def simdev_url(tmp_path_factory):
    exe = os.environ.get("OPENER_SIMDEV_EXE")
    if exe is None:
        pytest.skip("needs OPENER_SIMDEV_EXE")

    log_dir = tmp_path_factory.mktemp("simdev")
    log_path = log_dir / "stdout.log"
    # The output goes to a file: a pipe nobody drains would block the device.
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([exe], stdout=log, stderr=subprocess.STDOUT)
    try:
        pty = wait_for_pty(log_path, proc)
        yield f"uart://{pty}"
    finally:
        proc.terminate()
        proc.wait(timeout=START_TIMEOUT)
