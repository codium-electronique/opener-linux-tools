# opener-tools

Host tools for the [Opener](https://github.com/Opener-Initiative/opener) DECT NR+
stack. They drive a radio co-processor running the Opener `dectnrp` driver and
its Spinel interface (Opener `doc/spinel.rst`, firmware sample
`opener-samples/samples/dectnrp-spinel`).

## Tools

- `nrp-sniffer`: captures DECT NR+ frames into a pcap (link type
  `DECT_NR_TAP`, 304, decoded by Wireshark 4.7.4 or later), from the command
  line or as a Wireshark extcap. See [docs/nrp-sniffer.md](docs/nrp-sniffer.md).

## Install

Python 3.11 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Extras: `spi` (SPI transport: `spidev`, `gpiod` 2.x; building `spidev` needs
the Python headers, `python3-dev`), `hub`, `dev` (tests and linters). The
NR+Gate also needs a one-time udev setup, see
[docs/nrp-sniffer.md](docs/nrp-sniffer.md#on-the-nrgate).

To use `nrp-sniffer` from Wireshark, install it as an extcap with the venv
active: `scripts/install-extcap.sh`.

## Layout

- `src/opener_tools/spinel/`: Spinel codec and constants vendored from
  [pyspinel](https://github.com/openthread/pyspinel) (see `NOTICE`), HDLC-lite
  framing, and the DECT NR+ identifiers (`dectnrp_constants.py`, mirror of
  Opener's `include/zephyr/net/dectnrp_spinel.h`), and the Spinel client.
- `src/opener_tools/transport/`: device links selected by URL (`uart://`,
  `tcp://`).
- `src/opener_tools/dectnrp/`: the driver API over Spinel (`Radio`), its
  events, and `RxPipeline`, which keeps RX windows queued on the device.
- `src/opener_tools/capture/`: DECT-NR-TAP records, pcap writer, Wireshark
  extcap interface.
- `src/opener_tools/cli/`: command-line tools.
- `crates/`: Rust tools, in one Cargo workspace. `nrp-spi-probe` checks the
  NR+Gate SPI link and times Spinel round trips.

## Rust tools

Cross-built on the PC for the NR+Gate (aarch64, glibc), with the
`aarch64-unknown-linux-gnu` rustup target and the `gcc-aarch64-linux-gnu`
linker. No kernel headers or target libraries are needed. With that target,
`cargo run` and `cargo test` copy the binary to the gateway and run it there
(`scripts/gateway-run.sh`; `NRP_GATEWAY` overrides `codium@172.16.1.134`):

```bash
rustup target add aarch64-unknown-linux-gnu
sudo apt install gcc-aarch64-linux-gnu
cargo test                          # unit tests, on the PC
cargo gw -p nrp-spi-probe           # release build, run on the gateway
cargo gw-test                       # unit tests, on the gateway
```

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check
.venv/bin/ruff format --check
cargo fmt --check
cargo clippy --all-targets
```

`tests/spinel/test_constants.py` checks the DECT NR+ identifiers against the C
header of an Opener checkout: `OPENER_DIR` (default
`../opener-samples/deps/dectnrp-opener`). It is skipped if the header is not
found.

Tests marked `simdev` run against the simulated device, an Opener native_sim
build that serves Spinel over a pty on a mock radio. Build it from
`opener-samples`, then point `OPENER_SIMDEV_EXE` at it:

```bash
west build -b native_sim/native/64 -d build/simdev deps/dectnrp-opener/tests/subsys/spinel/simdev
OPENER_SIMDEV_EXE=<opener-samples>/build/simdev/simdev/zephyr/zephyr.exe .venv/bin/pytest -m simdev
```

With `tshark` 4.7.4 or later on `PATH`, they also check that tshark decodes
the captures.

Tests marked `hw` need a device running `dectnrp-spinel`; `--peer` adds the
tests that expect a `dectnrp-driver` device transmitting on channel 1677:

```bash
.venv/bin/pytest -m hw --transport 'uart:///dev/ttyACM0?baud=1000000' [--peer]
```

Without `OPENER_SIMDEV_EXE` or `--transport`, these tests are skipped.

## License

Apache-2.0, see `LICENSE`.
