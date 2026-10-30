# Spinel M3 — SPI transport, sniffer on the NR+Gate: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Execution mode:** the same as plans 1 and 2. Claude implements task by task and never commits. At each "Commit" step it gives the commit command(s) with the `Co-Authored-By` trailer and waits for the user to commit.

**Goal:** Spinel over SPI between the NR+Gate's RK3566 and its nRF9151: a device SPI-slave transport in Opener, an `spi://` host transport in linux-tools, and `nrp-sniffer` running on the gateway, capturing a `dectnrp-driver` peer.

**Architecture:** The framing rules (5-byte header, when a frame counts as sent or received) live in one small pure module on each side: `subsys/spinel/spi_link.c` and `opener_tools/spinel/spi_frame.py`. Both are tested without hardware against the same byte vectors. The device transport `transport/spi.c` is thin glue: Zephyr async SPI slave, IRQ and BUSY GPIOs, one thread. The host `SpiTransport` drives the link from asyncio through a small I/O interface: real spidev and gpiod in production, a Python fake device in tests. Nothing above the transport changes: `SpinelClient`, `Radio`, `RxPipeline` and `nrp-sniffer` run unchanged with an `spi://` URL.

**Tech Stack:** Zephyr (NCS v3.4.0) `nordic,nrf-spis` with `CONFIG_SPI_ASYNC`, ztest on native_sim. Python ≥ 3.11 asyncio, `spidev` and `gpiod` ≥ 2 (the `[spi]` extra), pytest.

**Spec:** `linux-tools/docs/specs/2026-10-28-opener-spinel-design.md` §5.5, §6.1, §8.1–8.3 (milestone 3, §9). The remote capture through `nrp-hub` of §8.3.3 belongs to milestone 5. It builds on plans 1 and 2 (`2026-10-28-spinel-m1-foundation.md`, `2026-10-28-spinel-m2-sniffer.md`).

## Global Constraints

- Plan 1's Global Constraints apply:
  - repos and branch `spinel-ctrl`, path prefixes `opener:`, `samples:`, `tools:`;
  - DCO sign-off, SPDX header, `Copyright (c) 2026 Codium Electronique`;
  - `clang-format -i` (22.1.5) on touched C files, ruff on Python;
  - the explicit style: named locals; no chained calls, no ternaries, no calls inside conditions or asserts.
- SPI header (spec §5.5, OpenThread `spi_frame.hpp`), identical on both sides:
  - `HDR u8 | RECV_LEN u16 LE | DATA_LEN u16 LE`, followed by the raw Spinel frame, no HDLC;
  - `HDR` bits: `0x80` RST, `0x40` CRC, `0x20` CCF, bits 1..0 pattern `0b10`;
  - a header is valid only if `(HDR & 0x03) == 0x02` and the CRC bit is clear (CRC is never used: neither side sets it);
  - `RECV_LEN` = the payload bytes the sender can accept in this transaction; `DATA_LEN` = the payload it sends, 0 for none.
- Link rules (spec §5.5):
  - a frame counts as sent only if the transaction clocked at least `5 + DATA_LEN` bytes, otherwise it stays pending;
  - a received frame is accepted only if its `DATA_LEN` ≤ the receiver's advertised `RECV_LEN` and `5 + DATA_LEN` bytes were clocked;
  - the device sets RST in its headers until the first transaction of at least 5 bytes after boot;
  - IRQ is active high (the host waits for a rising edge) and asserted while the device has a frame pending; BUSY is active high and asserted while the device is not armed for a transaction.
- Gateway wiring (CLAUDE.md, authoritative):
  - `spi3` CS0 = `spi3.0` ↔ nRF `spi2` SPIS (CSN P0.05, SCK P0.06, MISO P0.07, MOSI P0.08), at most 8 MHz, SPI mode 0;
  - IRQ: nRF P0.09 → `gpiochip4` line 0;
  - BUSY: nRF P0.26 → `gpiochip1` line 1;
  - nRF logs: `uart0` TX P0.27 → `/dev/ttyS3` at 115200.
- Host URL: `spi:///dev/spidev3.0?irq=gpiochip4:0&busy=gpiochip1:1&speed=8000000`. `irq` and `busy` are required; `speed` defaults to 8000000.
- The gateway must not need a kernel module or a reboot. `/dev/spidev3.0` comes from binding spidev to `spi3.0` through `driver_override` (no kernel driver binds `codium,nrp-spi`, modalias `spi:nrp-spi`).
- Gateway access: `ssh codium@172.16.1.134` (key login works; passwordless sudo). The nRF9151 is flashed through OpenOCD on the gateway, as in CLAUDE.md "Test hardware".

## Review Focus

1. **The host clocks while the device is not armed** (right after a transaction, or during boot). MISO then carries the SPIS default character. The overlay sets it to `0xFF`, an invalid header: the host must count it, deliver nothing and retry its frame → `test_invalid_header_retried`, Task 2; `test_complete_bad_pattern`, Task 1.
2. **A device frame longer than what the host clocked** (a 1 kB PDC). The host must clock a second transaction of `5 + DATA_LEN` and deliver the frame once; the device must keep it pending after the short transaction → `test_long_frame_second_transaction`, Task 2; `test_complete_short_clock_not_sent`, Task 1.
3. **The device has no room for the host's frame** (`RECV_LEN` smaller than the frame). The frame must be sent again in a later transaction, exactly once → `test_send_retried_until_accepted`, Task 2.
4. **No host is clocking** (sniffer not running, or killed with `kill -9`). The device TX thread must not block forever: a frame not sent within 1 s is dropped and counted in `transport_errors`. A new host must then work after its `CMD_RESET` → HW step 6, Task 4.
5. **Wrong URL, missing `[spi]` extra or missing permissions.** The transport must fail at open with a message naming the cause, not hang → `test_spi_url_errors`, `test_missing_extra`, Task 2; the udev rule, Task 4.

---

### Task 1: Device — SPI link framing (`spi_link`)

**Files:**
- Create: `opener:subsys/spinel/spi_link.{c,h}`
- Modify: `opener:subsys/spinel/CMakeLists.txt` (build `spi_link.c` with `CONFIG_DECTNRP_SPINEL_TRANSPORT_SPI` or `CONFIG_DECTNRP_SPINEL_TRANSPORT_NONE`, like `hdlc.c`, so the native_sim suite can link it)
- Test: `opener:tests/subsys/spinel/spi_link/{CMakeLists.txt,Kconfig,prj.conf,testcase.yaml,src/main.c}` (scaffolding copied from `tests/subsys/spinel/hdlc/`)

**Interfaces:**
- Produces (`spi_link.h`):
  - `#define SPINEL_SPI_HEADER_LEN 5`; `SPINEL_SPI_FLAG_RST BIT(7)`, `SPINEL_SPI_FLAG_CRC BIT(6)`, `SPINEL_SPI_FLAG_CCF BIT(5)`, `SPINEL_SPI_PATTERN 0x02`, `SPINEL_SPI_PATTERN_MASK 0x03`.
  - `struct spinel_spi_link { bool reset_pending; uint16_t recv_len; size_t armed_len; };` — `armed_len` is the payload length of the frame in the armed buffer, 0 for none.
  - `void spinel_spi_link_init(struct spinel_spi_link *link, uint16_t recv_len)`: sets `reset_pending`.
  - `size_t spinel_spi_link_arm(struct spinel_spi_link *link, const uint8_t *frame, size_t len, uint8_t *tx_buf, size_t tx_size)`: writes the header (RST if pending, `RECV_LEN` = `link->recv_len`, `DATA_LEN` = `len`) and copies the frame. Returns the number of bytes to arm (`5 + len`), or 0 if `tx_buf` is too small. `frame` may be NULL with `len` 0.
  - `struct spinel_spi_result { bool sent; const uint8_t *rx_frame; size_t rx_len; int err; };`
  - `void spinel_spi_link_complete(struct spinel_spi_link *link, const uint8_t *rx_buf, size_t clocked, struct spinel_spi_result *res)`:
    - `res->sent`: `armed_len > 0` and `clocked >= 5 + armed_len`;
    - `reset_pending` is cleared once `clocked >= 5`;
    - `res->rx_frame` points into `rx_buf` (payload of the host's frame), `rx_len > 0` only for a valid frame;
    - `res->err`: 0, or `-EBADMSG` for fewer than 5 bytes clocked, a bad pattern, the CRC bit set, or `DATA_LEN` larger than what was clocked; `-EMSGSIZE` for `DATA_LEN` > `link->recv_len`.
- Consumed by Task 3.

- [ ] **Step 1: Write the failing tests** (`ZTEST_SUITE(spinel_spi_link)`). The byte vectors are shared with the host's `test_spi_frame.py` (Task 2); keep them identical.
  - `test_arm_header`: `recv_len` 1300, a 7-byte frame, reset already cleared → the header is `02 14 05 07 00`, followed by the frame; returns 12.
  - `test_arm_reset_flag`: right after init → the first byte is `0x82`; after a `complete()` with 5 bytes clocked → `0x02`.
  - `test_arm_empty`: `frame` NULL, `len` 0 → `02 14 05 00 00`, returns 5.
  - `test_arm_too_small`: `tx_size` 8 for a 7-byte frame → returns 0.
  - `test_complete_rx_frame`: host header `02 00 01 03 00` + 3 bytes, clocked 8 → `rx_len` 3, `rx_frame == rx_buf + 5`, `err` 0.
  - `test_complete_sent`: armed 7 bytes, clocked 12 → `sent`; clocked 128 → `sent`.
  - `test_complete_short_clock_not_sent`: armed 600 bytes, clocked 133 → not `sent`, `err` 0 (a valid host header with `DATA_LEN` 0).
  - `test_complete_bad_pattern`: header `ff ff ff ff ff` and `00 00 00 00 00` → `-EBADMSG`, `rx_len` 0.
  - `test_complete_crc_flag`: `42 00 01 00 00` → `-EBADMSG`.
  - `test_complete_too_long`: `recv_len` 16, host `DATA_LEN` 17 → `-EMSGSIZE`.
  - `test_complete_truncated`: host `DATA_LEN` 10, clocked 12 → `-EBADMSG`.
  - `test_complete_no_header`: clocked 3 → `-EBADMSG`, not `sent`, `reset_pending` still set.
- [ ] **Step 2: Run, expect FAIL** (build error): `west twister -T deps/dectnrp-opener/tests/subsys/spinel/spi_link -p native_sim/native/64`
- [ ] **Step 3: Implement** `spi_link.c`. Little-endian fields with `sys_put_le16` / `sys_get_le16`.
- [ ] **Step 4: Run, expect PASS**, then the whole Spinel set: `west twister -T deps/dectnrp-opener/tests/subsys/spinel -p native_sim/native/64` → all suites pass.
- [ ] **Step 5: Commit** in opener: `feat(spinel): add SPI link framing`.

### Task 2: Host — SPI framing and `spi://` transport

**Files:**
- Create: `tools:src/opener_tools/spinel/spi_frame.py`, `tools:src/opener_tools/transport/spi.py`, `tools:src/opener_tools/transport/spi_io.py`
- Modify: `tools:src/opener_tools/transport/__init__.py` (the `spi` scheme instead of `NotImplementedError`; export `SpiTransport`)
- Test: `tools:tests/spinel/test_spi_frame.py`, `tools:tests/transport/test_spi.py`, `tools:tests/transport/test_url.py`; `FakeSpiDevice` in `tools:tests/fakes.py`

**Interfaces:**
- Produces, `spinel/spi_frame.py`:
  - `HEADER_LEN = 5`, `FLAG_RST = 0x80`, `FLAG_CRC = 0x40`, `FLAG_CCF = 0x20`, `PATTERN = 0x02`, `PATTERN_MASK = 0x03`.
  - `@dataclass(frozen=True) class SpiHeader: reset: bool; recv_len: int; data_len: int`.
  - `def build_header(recv_len: int, data_len: int, reset: bool = False) -> bytes`.
  - `def parse_header(data: bytes) -> SpiHeader | None`: None if shorter than 5 bytes, a bad pattern or the CRC bit set.
- Produces, `transport/spi_io.py`, the hardware boundary:
  - `class SpiIo(Protocol)`:
    - `def open(self) -> None`; `def close(self) -> None`;
    - `def transfer(self, data: bytes) -> bytes`: one full-duplex transaction, CS held for all of it;
    - `def irq_fd(self) -> int`: readable when IRQ edges are pending;
    - `def read_irq_edges(self) -> None`: drains them;
    - `def irq_active(self) -> bool`; `def busy_active(self) -> bool`.
  - `class LinuxSpiIo(SpiIo)`, `__init__(self, path: str, irq: tuple[str, int], busy: tuple[str, int], speed: int)`:
    - spidev `SpiDev()` opened on the bus and device numbers parsed from `/dev/spidevB.D`, mode 0, `max_speed_hz = speed`, `xfer3` for transfers;
    - gpiod 2.x `request_lines` on `/dev/<chip>`: IRQ as an input with `Edge.RISING`, BUSY as a plain input, consumer `"nrp-spi"`;
    - `spidev` and `gpiod` are imported in `open()`. A failed import raises `RuntimeError("the SPI transport needs the spi extra: pip install 'opener-tools[spi]'")`.
- Produces, `transport/spi.py`:
  - `DEFAULT_SPEED = 8_000_000`; `DEFAULT_CLOCK = 128` (payload bytes clocked when no longer frame is known); `BUSY_POLL_S = 0.0002`.
  - `class SpiTransport(Transport)`, `__init__(self, path: str, irq: tuple[str, int], busy: tuple[str, int], speed: int = DEFAULT_SPEED, max_frame: int = DEFAULT_MAX_FRAME, io: SpiIo | None = None)`. `io` defaults to `LinuxSpiIo(path, irq, busy, speed)`. `errors` counts invalid headers and refused frames.
  - `send()` returns once the device has accepted the frame (backpressure); `recv()` as for UART. Losing the link (an `OSError` from `io`), or `close()`, makes `recv()` and the pending `send()`s raise `TransportClosed`.
- Link loop (a task started by `open()`; transfers are short, so they run inline in the event loop):
  1. Wait until IRQ is active or a frame is waiting to be sent: `loop.add_reader(io.irq_fd(), …)`, plus an `asyncio.Event` for sends. After every transaction, wait until BUSY is inactive, then check `irq_active()`: an edge can come while a transaction runs, and the device updates IRQ before it releases BUSY, so the level is current only then.
  2. Wait until BUSY is inactive, polling every `BUSY_POLL_S`.
  3. Clock `5 + max(len(frame), DEFAULT_CLOCK)` bytes: host header (`RECV_LEN` = clocked − 5, `DATA_LEN` = `len(frame)` or 0), then the frame, then zero padding.
  4. Parse the device header. If it is invalid, count it and go back to 1; the frame stays queued.
  5. If a frame was sent and the device's `RECV_LEN` ≥ its length, it is accepted: resolve its `send()`. Otherwise it stays queued.
  6. If the device's `DATA_LEN` > 0: deliver `rx[5:5+DATA_LEN]` if it was all clocked. Otherwise, after BUSY is inactive again, clock a second transaction of `5 + DATA_LEN` with no host frame, and deliver from it. A `DATA_LEN` > `max_frame` counts as an error and delivers nothing.
- `open_transport("spi:///dev/spidev3.0?irq=gpiochip4:0&busy=gpiochip1:1&speed=4000000")` returns `SpiTransport(path="/dev/spidev3.0", irq=("gpiochip4", 0), busy=("gpiochip1", 1), speed=4000000)`.
- `FakeSpiDevice` (tests) implements `SpiIo` with the device rules of the Global Constraints:
  - an `os.pipe()` stands for the IRQ edges;
  - `queue(frame)` makes a frame pending and raises IRQ, `received` lists the frames it accepted;
  - `recv_len` is settable;
  - `busy_for(n)` keeps BUSY active for the next n polls;
  - `garbage_next()` answers the next transaction with `ff ff ff ff ff`;
  - `fail_next()` makes the next `transfer()` raise `OSError`;
  - `transfers` logs the clocked lengths;
  - `transfer()` raises `AssertionError` if called while BUSY is active.

- [ ] **Step 1: Write the failing tests.**
  - `test_spi_frame.py`: the byte vectors of Task 1 Step 1 (`build_header(1300, 7) == bytes.fromhex("0214050700")`, reset → `0x82`; `parse_header` of `ff…`, `00…`, `42 00 01 00 00`, 4 bytes → None; a round trip).
  - `test_spi.py`:
    - `test_round_trip`: host `send(b"\x81\x02\x03")` → the device received it; device `queue(b"\x80\x06\x00")` → `recv()` returns it.
    - `test_long_frame_second_transaction`: the device queues 600 bytes → `recv()` returns them, and `transfers == [133, 605]`.
    - `test_send_retried_until_accepted`: device `recv_len` 0 for the first transaction, then 1300 → the frame is in `received` exactly once, and `send()` returned.
    - `test_invalid_header_retried`: `garbage_next()`, then a send → delivered once, `errors == 1`.
    - `test_waits_for_busy`: `busy_for(5)` → no transfer while BUSY is active (the fake asserts it), the frame is still delivered.
    - `test_irq_level_rechecked`: two frames queued back to back with a single edge → both received.
    - `test_close_ends_recv`: `close()` → `recv()` and a pending `send()` raise `TransportClosed`.
    - `test_io_error_closes`: `fail_next()` → `recv()` raises `TransportClosed`.
    - `test_missing_extra`: with `sys.modules["spidev"] = None` (monkeypatch), `LinuxSpiIo(...).open()` raises the `RuntimeError` above.
  - `test_url.py`: `test_spi_url` (the URL above → fields); `test_spi_url_errors` → `ValueError` for no `irq`, no `busy`, `irq=gpiochip4` without a line, an unknown parameter.
- [ ] **Step 2: Run, expect FAIL**: `../.venv/bin/pytest tests/spinel/test_spi_frame.py tests/transport -q`
- [ ] **Step 3: Implement** `spi_frame.py`, `spi_io.py`, `spi.py` and the URL scheme.
- [ ] **Step 4: Run, expect PASS** 10 times in a row (timing-dependent fake), then `../.venv/bin/pytest -q`, `ruff check .` and `ruff format --check .`: all clean.
- [ ] **Step 5: Commit** in linux-tools: `feat: add SPI transport`.

### Task 3: Device — SPI transport and NR+Gate firmware

**Files:**
- Create: `opener:subsys/spinel/transport/spi.c`, `opener:dts/bindings/spinel/opener,spinel-spi.yaml`
- Modify:
  - `opener:zephyr/module.yml` (add `dts_root: .` under `settings`);
  - `opener:subsys/spinel/Kconfig`, `opener:subsys/spinel/CMakeLists.txt`;
  - `opener:doc/spinel.rst` (new "SPI" section next to "UART with HDLC-lite framing");
  - `samples:samples/dectnrp-spinel/{prj.conf,sample.yaml,README.rst}`, `samples:.github/workflows/build-samples.yml`.
- Create: `samples:samples/dectnrp-spinel/boards/cod_nrp_gw_nrf9151_ns.{overlay,conf}`

**Interfaces:**
- Consumes: Task 1 `spinel_spi_link_*`; the transport contract of `spinel_internal.h` (`spinel_transport_init()`, `spinel_transport_send()` from the TX thread only, `spinel_rx_submit()`, `spinel_stat_inc()`).
- Produces:
  - Binding `opener,spinel-spi`: `include: spi-device.yaml`, `irq-gpios` (required), `busy-gpios` (required). It is a child of the SPIS controller node.
  - Kconfig:
    - `DECTNRP_SPINEL_TRANSPORT_SPI` depends on `DT_HAS_OPENER_SPINEL_SPI_ENABLED` and selects `SPI`, `SPI_SLAVE`, `SPI_ASYNC`, `GPIO`;
    - the transport choice gets `default DECTNRP_SPINEL_TRANSPORT_SPI if DT_HAS_OPENER_SPINEL_SPI_ENABLED` before the UART default;
    - new `DECTNRP_SPINEL_SPI_STACK_SIZE` int, default 1024;
    - new `DECTNRP_SPINEL_SPI_TX_TIMEOUT_MS` int, default 1000: a frame no host clocked in that time is dropped and counted in `SPINEL_STAT_TRANSPORT_ERRORS`.
- `transport/spi.c` behaviour:
  - Uses the first enabled `opener,spinel-spi` node, `SPI_DT_SPEC_GET(node, SPI_OP_MODE_SLAVE | SPI_WORD_SET(8) | SPI_TRANSFER_MSB, 0)`, mode 0.
  - Static TX and RX buffers of `SPINEL_SPI_HEADER_LEN + CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE`; `recv_len` = `CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE`.
  - One thread `spinel_spi` loops:
    1. Arm with the pending frame or none (`spinel_spi_link_arm`) and call `spi_transceive_cb()`. Set IRQ (active if a frame is pending, inactive otherwise), and only then set BUSY inactive.
    2. Wait for the transaction (a semaphore given by the callback) or for a new frame from `spinel_transport_send()`. A new frame while armed empty only sets IRQ: it goes in the buffer at the next arming.
    3. On completion, call `spinel_spi_link_complete()` with the callback's result, the number of bytes received. On `sent`: wake the sender. On `rx_len`: `spinel_rx_submit()`. On `err`: `spinel_stat_inc(SPINEL_STAT_FRAMES_REJECTED)`.
  - The SPI callback (ISR) sets BUSY active before giving the semaphore. The host waits for BUSY inactive before clocking, so it cannot clock an unarmed SPIS right after a transaction.
  - At init: BUSY active, IRQ inactive, then start the thread.
  - `spinel_transport_send()` copies nothing: the frame stays valid until it returns. It waits up to `CONFIG_DECTNRP_SPINEL_SPI_TX_TIMEOUT_MS`, then returns `-ETIMEDOUT` after counting a transport error.
- Gateway board files:
  - The overlay includes `nrf91x1_partition_without_bootloader.dtsi` and `nrf91x1_dect_modem.dtsi`.
  - `&spi2`:
    - `def-char = <0xff>`: an unarmed SPIS answers with an invalid header;
    - `/delete-property/ cs-gpios;`: the SPIS uses its CSN pin, and a CS GPIO would drive P0.05;
    - child `spinel_spi: spinel@0 { compatible = "opener,spinel-spi"; reg = <0>; spi-max-frequency = <8000000>; irq-gpios = <&gpio0 9 GPIO_ACTIVE_HIGH>; busy-gpios = <&gpio0 26 GPIO_ACTIVE_HIGH>; };`.
  - The `.conf` has the DK conf's modem lines, plus logs on `uart0` instead of RTT: `CONFIG_USE_SEGGER_RTT=n`, `CONFIG_LOG_BACKEND_RTT=n`, `CONFIG_RTT_CONSOLE=n`, `CONFIG_UART_CONSOLE=y`, `CONFIG_LOG_BACKEND_UART=y`.
  - `prj.conf` drops `CONFIG_DECTNRP_SPINEL_TRANSPORT_UART=y`: the devicetree now picks the transport.
- The SPI section of `spinel.rst` documents:
  - the header and the link rules of the Global Constraints;
  - IRQ and BUSY, both active high (IRQ's polarity is the opposite of OpenThread's, and BUSY is an extension);
  - the devicetree example;
  - that `def-char` should be `0xff`.

- [ ] **Step 1: Write the transport, binding, Kconfig and board files.** Hardware is the only test of the glue; the framing it relies on is tested in Task 1.
- [ ] **Step 2: Build for the gateway**: `west build -p always -b cod_nrp_gw/nrf9151/ns -d build/cod_nrp_gw/nrf9151/ns/dectnrp-spinel samples/dectnrp-spinel`. Expected: success, and `build/.../zephyr/.config` has `CONFIG_DECTNRP_SPINEL_TRANSPORT_SPI=y` and `CONFIG_SPI_ASYNC=y`.
- [ ] **Step 3: Check the UART boards are unchanged**: build for `nrf9151dk/nrf9151/ns` and `nrf9131ek/nrf9131/ns` → `CONFIG_DECTNRP_SPINEL_TRANSPORT_UART=y`. Then reflash Device1 (EK) and run `../.venv/bin/pytest -m hw --transport 'uart:///dev/ttyACM2?baud=1000000' --peer` from linux-tools → 5 passed.
- [ ] **Step 4: Run the device suites**: `west twister -T deps/dectnrp-opener/tests -p native_sim/native/64` → all pass. Run `clang-format --dry-run --Werror` on the touched C files.
- [ ] **Step 5: Add the CI build**: in `build-samples.yml`, after "Build dectnrp-spinel", a step building `cod_nrp_gw/nrf9151/ns` into `${BUILD_BASE}/dectnrp-spinel-gw` (build only, no artifact).
- [ ] **Step 6: Commit**:
  - in opener: `feat(spinel): add SPI slave transport`;
  - in opener-samples: `samples: dectnrp-spinel: add the NR+Gate over SPI`.

### Task 4: NR+Gate bring-up and hardware validation (§8.3.3)

**Files:**
- Create: `tools:scripts/nrp-gate/99-nrp-spi.rules`, `tools:scripts/nrp-gate/install.sh`
- Modify:
  - `tools:docs/nrp-sniffer.md` (transport table, an "On the NR+Gate" section);
  - `tools:README.md` (the `[spi]` extra);
  - `tools:docs/specs/2026-10-28-opener-spinel-design.md` (only where the result differs from §5.5 and §6.1).

**Interfaces:**
- Consumes: Task 2 `spi://` URL, Task 3 firmware.
- The udev rule:
  - on `ACTION=="add", SUBSYSTEM=="spi", ENV{MODALIAS}=="spi:nrp-spi"`, it writes `spidev` to the device's `driver_override` and binds it to the `spidev` driver;
  - `SUBSYSTEM=="spidev", KERNEL=="spidev3.0"` gets `GROUP="gpio", MODE="0660"`.
- `install.sh`, run on the gateway with sudo:
  - installs the rule;
  - reloads udev and triggers the `spi3.0` device, so no reboot is needed;
  - checks `/dev/spidev3.0` exists and the user can open `/dev/gpiochip1` and `/dev/gpiochip4`.

- [ ] **Step 1: Deploy linux-tools.**
  - From `linux-tools/`, run `rsync -a --delete --exclude .venv --exclude __pycache__ ./ codium@172.16.1.134:linux-tools/`.
  - On the gateway: `python3 -m venv ~/nrp-venv && ~/nrp-venv/bin/pip install -e "$HOME/linux-tools[spi,dev]"`. If building `gpiod` or `spidev` fails for lack of Python headers, `sudo apt install python3-dev` and retry.
  - Run `sudo ~/linux-tools/scripts/nrp-gate/install.sh`. Expected: `/dev/spidev3.0` exists, group `gpio`.
- [ ] **Step 2: Flash the gateway nRF9151.**
  - On the gateway, start OpenOCD: `openocd -f cod-nrp-gw.cfg -c "bindto 172.16.1.134" -f cod-nrp-gw-rtt.cfg`.
  - From the PC, load `tfm_merged.hex` with the GDB command of CLAUDE.md.
  - Expected: `cat /dev/ttyS3` (115200) on the gateway shows the sample banner. If the gateway's modem firmware is not the NR+ PHY one, stop and ask the user (it needs the J-Link).
- [ ] **Step 3: Hardware tests on the gateway**:
  - `~/nrp-venv/bin/pytest -m hw --transport 'spi:///dev/spidev3.0?irq=gpiochip4:0&busy=gpiochip1:1' --peer`, with Device2 running `dectnrp-driver` on channel 1677;
  - expected: 5 passed;
  - if only the `--peer` test fails, the gateway may be out of radio range of Device2: report it and ask the user to move the boards.
- [ ] **Step 4: Sniffer on the gateway.**
  - Run `timeout -s INT 60 ~/nrp-venv/bin/nrp-sniffer --transport '<spi URL>' -w /tmp/gw.pcap`. Expected: 19–20 records, 0 gaps, exit 0.
  - Copy the pcap to the PC; tshark 4.7.4 decodes PCC type 2 and the MAC PDU `Hello World!`.
  - Read `DRV_STATS` afterwards: `frames_rejected` and `transport_errors` unchanged from before the run.
- [ ] **Step 5: Link stress.**
  - Run a 60 s capture with `--window 5`: no `SpinelTimeout`, gaps only from resyncs, `transport.errors` at most a few.
  - Note the invalid-header count: a high count means the host clocks before the device is armed (Review Focus 1).
- [ ] **Step 6: Failure modes** (Review Focus 4 and spec §7):
  - `kill -9` the sniffer mid-capture, wait 5 s, start it again → it captures, and `transport_errors` went up (frames no host clocked);
  - reset the nRF from the OpenOCD telnet (`reset run`) during a capture → the sniffer logs the reset, initialises again and keeps writing (`1 device resets`).
- [ ] **Step 7: Docs and commit.**
  - In `nrp-sniffer.md`, document the `spi://` URL, the one-time `install.sh`, and that the gateway needs the `[spi]` extra.
  - Commit in linux-tools: `feat: NR+Gate SPI setup and docs`.
