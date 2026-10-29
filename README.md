# opener-tools

Host tools for the [Opener](https://github.com/Opener-Initiative/opener) DECT NR+
stack. They drive a radio co-processor running the Opener `dectnrp` driver and
its Spinel interface (Opener `doc/spinel.rst`, firmware sample
`opener-samples/samples/dectnrp-spinel`).

## Install

Python 3.11 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Extras: `spi` (SPI transport: `spidev`, `gpiod` 2.x), `hub`, `dev` (tests and
linters).

## Layout

- `src/opener_tools/spinel/`: Spinel codec and constants vendored from
  [pyspinel](https://github.com/openthread/pyspinel) (see `NOTICE`), HDLC-lite
  framing, and the DECT NR+ identifiers (`dectnrp_constants.py`, mirror of
  Opener's `include/zephyr/net/dectnrp_spinel.h`).

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check
.venv/bin/ruff format --check
```

`tests/spinel/test_constants.py` checks the DECT NR+ identifiers against the C
header of an Opener checkout: `OPENER_DIR` (default
`../opener-samples/deps/dectnrp-opener`). It is skipped if the header is not
found.

Tests marked `hw` need a device (`--transport uart:///dev/ttyACM0?baud=1000000`),
tests marked `simdev` need the native_sim device (`OPENER_SIMDEV_EXE`).

## License

Apache-2.0, see `LICENSE`.
