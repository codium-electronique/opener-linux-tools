# Spinel M1 — device core, UART transport, host client: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Spinel interface in the Opener stack exposing the dectnrp driver over UART/HDLC, plus the Python host client that drives it, tested without hardware (native_sim + pty) and smoke-tested on an nRF9151 DK.

**Architecture:** Opener gets an optional `subsys/spinel` (vendored OpenThread codec, frame dispatch, op-slot broker, HDLC UART transport) that is the exclusive user of the dectnrp driver. opener-samples gets a thin `dectnrp-spinel` firmware. linux-tools gets an asyncio Python package (`opener_tools`) with a vendored pyspinel codec, transports, a Spinel client and the driver-level `Radio` API.

**Tech Stack:** Zephyr (NCS v3.4.0 / Zephyr 4.4), ztest + twister on `native_sim/native/64`; Python ≥ 3.11 (dev on 3.14), asyncio, pyserial, pytest + pytest-asyncio, ruff.

**Spec:** `linux-tools/docs/specs/2026-10-28-opener-spinel-design.md` (sections referenced as §n below). This is plan 1 of 5 (one per spec milestone §9); plan 2 (`2026-10-28-spinel-m2-sniffer.md`) covers the sniffer. Plans 3–5 (SPI, `nrp-ping`, `nrp-hub`) are written after Task 1's risk probe and M2 land, because the probe can change D2.

## Global Constraints

- Repos and branches: `opener-samples/deps/dectnrp-opener` (paths below prefixed `opener:`), `opener-samples` (`samples:`), `linux-tools` (`tools:`). All work on branch `spinel-ctrl`. Commit in the repo that owns the file; `deps/` is git-ignored in opener-samples.
- Every commit: `git commit -s`, message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- New C files: Apache-2.0 SPDX header, `Copyright (c) 2026 Codium Electronique`; Zephyr style; `clang-format -i` (22.1.5 from `opener-samples/.venv`) on every touched `*.c`/`*.h` except the vendored `subsys/spinel/lib/`.
- New Python files: `# SPDX-License-Identifier: Apache-2.0` + `# Copyright (c) 2026 Codium Electronique`; vendored files keep their original header and add a `# Vendored from …@<commit>, modified: …` line.
- Everything in Opener builds only with `CONFIG_DECTNRP_SPINEL=y`, which depends on `CONFIG_DECTNRP_DRIVER_SCHEDULED_API`.
- Vendored sources, pinned: OpenThread `src/lib/spinel/spinel.{c,h}` @ `3d645eb9106cb1d5c2ff11891e84dc14e52e293a`; pyspinel `spinel/{codec,const,hdlc}.py` @ `5e0627abd04b7d2c7ca47c6615bd14b31227eb4d`.
- IDs (single source of truth `opener:include/zephyr/net/dectnrp_spinel.h`, mirrored in `tools:src/opener_tools/spinel/dectnrp_constants.py`):
  - PROPs: `DRV_ENABLED 0x3D00`, `DRV_CAPS 0x3D01`, `DRV_TIME 0x3D02`, `DRV_NETWORK_ID 0x3D03`, `DRV_FILTER_SHORT_NWID 0x3D04`, `DRV_FILTER_SHORT_RXADDR 0x3D05`, `DRV_OP_QUEUE 0x3D10`, `DRV_EVENT 0x3D11`, `DRV_STATS 0x3D20`.
  - CAPs, mirroring the property ranges: `CAP_DECTNRP_DRV 0x3D00` (15616); reserved `CAP_DECTNRP_MAC 0x3E00`, `CAP_DECTNRP_DLC 0x3F00`, `CAP_DECTNRP_CVG 0x3F80`.
  - `STATUS_DECTNRP_ERRNO_BASE = 0x3C80` (15488). Status = base + positive errno, capped at `0x3CFF`.
  - `INTERFACE_TYPE_DECTNRP = 0x4E52`. Protocol version 4.3.
- Wire values (§4.5, §4.6):
  - Op type: TX 0, RX 1, RSSI1 2. Flags: bit0 scheduled, bit1 lbt, bit2 harq, bit3 beacon.
  - Event code: MSG_RECEIVED 1, MSG_ERROR 2, OP_RESULTS 3, OP_FINISHED 4. The driver enum has different values (`DECTNRP_EVENT_MSG_ERROR` = 1, `MSG_RECEIVED` = 2), so always map explicitly.
- Spinel formats (exact):
  - Op prefix `SCSCX`. Suffixes: TX `CSCCCCLCd`, RX `LC`, RSSI1 `L`.
  - Event prefix `SClS`. Suffixes: MSG_RECEIVED `CCXCcSdd`, MSG_ERROR `CCXCcSd`, OP_RESULTS `Sd`, OP_FINISHED none.
  - `DRV_CAPS` `LCCCCcCLCCCCLSC`, `DRV_STATS` `LLLLLLL`, filters `bC` / `bS`.
- Spinel frame header byte = `0x80 | tid`; requests use TID 1–15, unsolicited frames TID 0.
- Modem time: 69 120 ticks/ms, 2880 ticks/symbol (nRF91), frame = 10 ms = 691 200 ticks (`DECTNRP_SUBSLOTS_PER_FRAME` 48 × 5 symbols).
- Twister: `west twister -T deps/dectnrp-opener/tests -p native_sim/native/64` from `opener-samples/` with `.venv` active.
- Host: `cd linux-tools && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`; `pytest`, `ruff check`, `ruff format --check`.

## Review Focus

1. **Events arriving before `INSERTED`.** An immediate op can raise driver events before the device has queued the `INSERTED` reply. The host must register the token *before* sending the insert and must accept the events → `test_event_before_inserted`, Task 12.
2. **Slot exhaustion during an event storm.** With the event pool empty, `OP_FINISHED` must still go out, and the slot must return to the pool once its frame is written. Otherwise the host waits forever → `test_event_pool_exhaustion`, Task 6.
3. **Garbage on the UART** (line noise, a host opening the port mid-frame, frames larger than `MAX_FRAME_SIZE`). The decoder must resync on the next `0x7E` and count the error, never overflow its buffer → `test_garbage_then_frame` / `test_overflow_resync`, Tasks 3 and 9, plus `test_uart.py`, Task 10.
4. **Device reset while ops are in flight.** On an unsolicited `LAST_STATUS RESET_*`, every pending `Op` must fail with `DeviceReset` instead of hanging, and a `CMD_RESET` the host sent itself must not raise → `test_reset_fails_pending_ops`, Task 12, and `test_reset_ok`, Task 11.
5. **TID wrap and stale replies.** A late reply for a timed-out TID must not resolve a newer request reusing that TID → `test_stale_reply_ignored`, Task 11.

---

### Task 1: Risk probe — queued scheduled ops on the nRF91 modem (hardware, throwaway)

Answers §8.3.1 before anything else is built. Not committed.

**Files:**
- Create (untracked; add to `opener-samples/.git/info/exclude`): `samples:samples/probe-queued-ops/` = copy of `samples/dectnrp-driver/` with `src/main.c` replaced.

- [ ] **Step 1: Write the probe `main()`**. Init exactly as `dectnrp-driver` does (network ID, iface). Then do two rounds on channel 1677:
  - Round A: `now = get_time()`; schedule 3 RX ops with `is_scheduled=1`, `start = now + 20 ms + k·100 ms`, `duration = 100 ms` (k = 0..2), each with its own op from the slab.
  - Round B: the same with 3 windows of 2 s.
  
  Log for every op: the return value of `schedule()`, and every event with its code and `get_time()` at callback, expressed relative to the op's start.
- [ ] **Step 2: Build and flash on DK Device1**: `west build -p -b nrf9151dk/nrf9151/ns -d build/probe samples/probe-queued-ops && west flash -d build/probe --dev-id <Device1>`. Device2 runs `dectnrp-driver` (TX every 3 s).
- [ ] **Step 3: Read the RTT log and record**:
  - (a) all 6 `schedule()` return 0;
  - (b) round A `OP_FINISHED` times are ≈ start + 100 ms, in order, with no gap between windows;
  - (c) in round B, a window that receives Device2's frame finishes early, right after `MSG_RECEIVED`. This is `RX_MODE_SINGLE_SHOT` (`nrf_modem_dect_phy.h`: "terminated automatically after the first successful PDC reception");
  - (d) the minimum lead time `start − now` that is accepted (repeat round A with leads 1, 5, 10 ms).
- [ ] **Step 4: Report to the user and gate.**
  - If (a) or (b) fails: **stop** and revisit D2 with the user. The fallback is a continuous-RX driver mode.
  - If (c) holds: plan 2 keeps short windows (default 20 ms, see plan 2).
  - Note the value found for (d). It becomes `RxPipeline`'s default lead.

### Task 2: Opener — subsystem skeleton, vendored codec, ID header

**Files:**
- Create: `opener:subsys/spinel/{Kconfig,CMakeLists.txt}`, `opener:subsys/spinel/lib/{spinel.c,spinel.h,LICENSE,README.rst,.clang-format}`, `opener:include/zephyr/net/dectnrp_spinel.h`
- Modify: `opener:subsys/Kconfig` (add `rsource "spinel/Kconfig"`), `opener:subsys/CMakeLists.txt` (add `add_subdirectory_ifdef(CONFIG_DECTNRP_SPINEL spinel)`)
- Test: `opener:tests/subsys/spinel/codec/{CMakeLists.txt,prj.conf,testcase.yaml,src/main.c}`

**Interfaces:**
- Produces Kconfig:
  - `DECTNRP_SPINEL` (bool; depends on `DECTNRP_DRIVER_SCHEDULED_API && NETWORKING`; selects `CRC`, `NET_BUF`);
  - `DECTNRP_SPINEL_MAX_FRAME_SIZE` int 1300;
  - `DECTNRP_SPINEL_OP_SLOTS` int 8;
  - `DECTNRP_SPINEL_EVENT_BUFS` int 16;
  - `DECTNRP_SPINEL_TX_BUFS` int 4;
  - `DECTNRP_SPINEL_RX_BUFS` int 4;
  - `DECTNRP_SPINEL_THREAD_STACK_SIZE` int 2048;
  - `DECTNRP_SPINEL_THREAD_PRIO` int 5;
  - `DECTNRP_SPINEL_RESET_TIMEOUT_MS` int 1000;
  - `DECTNRP_SPINEL_L2_STUB` bool default y (Task 4);
  - choice `DECTNRP_SPINEL_TRANSPORT`: `_UART` (Task 8), `_SPI` (plan 3), `_NONE` (tests only);
  - log module `DECTNRP_SPINEL`.
- Produces `dectnrp_spinel.h`:
  - `enum dectnrp_spinel_prop` (`DECTNRP_SPINEL_PROP_DRV_*` with the IDs above);
  - `DECTNRP_SPINEL_CAP_DRV`/`_MAC`/`_DLC`/`_CVG`;
  - `DECTNRP_SPINEL_STATUS_ERRNO_BASE`;
  - `DECTNRP_SPINEL_INTERFACE_TYPE`;
  - `enum dectnrp_spinel_op_type` (`_TX=0`, `_RX=1`, `_RSSI1=2`);
  - `DECTNRP_SPINEL_OP_FLAG_{SCHEDULED,LBT,HARQ,BEACON}` = `BIT(0..3)`;
  - `enum dectnrp_spinel_event_code` (`_MSG_RECEIVED=1`, `_MSG_ERROR=2`, `_OP_RESULTS=3`, `_OP_FINISHED=4`);
  - `DECTNRP_SPINEL_RX_VALID_{RSSI2,SNR,PCC}` = `BIT(0..2)`;
  - the format strings as `#define DECTNRP_SPINEL_FMT_*` (`OP_PREFIX "SCSCX"`, `OP_TX`, `OP_RX`, `OP_RSSI1`, `EVT_PREFIX "SClS"`, `EVT_MSG_RECEIVED`, `EVT_MSG_ERROR`, `EVT_OP_RESULTS`, `DRV_CAPS`, `DRV_STATS`).
  
  Every ID has a one-line doc comment citing the spec section.

- [ ] **Step 1: Vendor the codec.** Fetch `spinel.c`/`spinel.h` from `https://raw.githubusercontent.com/openthread/openthread/3d645eb9106cb1d5c2ff11891e84dc14e52e293a/src/lib/spinel/`, unmodified. Add `LICENSE`, OpenThread's BSD-3 text. Add `README.rst` stating origin, commit and "do not modify; update by re-vendoring". Add `.clang-format` containing `DisableFormat: true` and `SortIncludes: Never`, so the CI clang-format check skips these files. Build it as a `zephyr_library_named(dectnrp_spinel)` with `zephyr_library_include_directories(lib)`.
- [ ] **Step 2: Write the failing codec test** (`ZTEST_SUITE(spinel_codec)`):
  - `test_pack_unpack_op_prefix`: pack `DECTNRP_SPINEL_FMT_OP_PREFIX` with (0x1234, 1, 1677, 0x01, 0x0102030405060708). Expect exactly 14 bytes `34 12 01 8d 06 01 08 07 06 05 04 03 02 01`. Unpacking returns the same values.
  - `test_header_byte`: `SPINEL_HEADER_FLAG | 5 == 0x85`.
  - `test_prop_range`: `DECTNRP_SPINEL_PROP_DRV_ENABLED >= SPINEL_PROP_VENDOR__BEGIN` and every PROP < `SPINEL_PROP_VENDOR__END`.

  `prj.conf`: `CONFIG_ZTEST=y`, `CONFIG_NETWORKING=y`, `CONFIG_NET_TEST=y`, `CONFIG_DECTNRP_DRIVER_SCHEDULED_API=y`, `CONFIG_DECTNRP_SPINEL=y`, `CONFIG_DECTNRP_SPINEL_TRANSPORT_NONE=y`, `CONFIG_DECTNRP_SPINEL_L2_STUB=n`. `testcase.yaml`: `tests: opener.spinel.codec: platform_allow: native_sim/native/64`.
- [ ] **Step 3: Run it, expect a build or test FAIL**: `west twister -T deps/dectnrp-opener/tests/subsys/spinel/codec -p native_sim/native/64`
- [ ] **Step 4: Add the header, Kconfig and CMake until the test passes.** If `spinel.c` triggers warnings under Zephyr's flags, silence them per file with `set_source_files_properties(... COMPILE_OPTIONS ...)`, never by editing the file.
- [ ] **Step 5: Run the test, expect PASS.**
- [ ] **Step 6: Commit** in opener: `feat(spinel): add subsystem skeleton and vendored OpenThread codec`.

### Task 3: Opener — HDLC-lite encoder / decoder

**Files:**
- Create: `opener:subsys/spinel/hdlc.{c,h}`
- Test: `opener:tests/subsys/spinel/hdlc/…` (same scaffolding as Task 2)

**Interfaces:**
- Produces (`hdlc.h`, internal):
  - `struct spinel_hdlc_decoder { uint8_t *buf; size_t size; size_t len; bool escaped; bool overflow; uint16_t fcs; }`;
  - `void spinel_hdlc_decoder_init(struct spinel_hdlc_decoder *d, uint8_t *buf, size_t size)`;
  - `int spinel_hdlc_decode_byte(struct spinel_hdlc_decoder *d, uint8_t byte)`. Returns >0 = frame length complete in `d->buf` (FCS stripped), 0 = need more, `-EBADMSG` = bad FCS, `-EMSGSIZE` = overflow (resync on next flag);
  - `size_t spinel_hdlc_encode(const uint8_t *frame, size_t len, uint8_t *out, size_t out_size)`. Returns the encoded length (leading and trailing `0x7E`) or 0 if `out` is too small.
  - `SPINEL_HDLC_MAX_ENCODED(len)` = `2*(len+2)+2`.
- FCS = CRC-16/X-25: `~crc16_ccitt(0xffff, data, len)`, appended little-endian. Escape `0x7E 0x7D 0x11 0x13 0xF8` as `0x7D, b^0x20` (§5.4).

- [ ] **Step 1: Write the failing tests:**
  - `test_fcs_check_value`: the X-25 FCS of `"123456789"` is `0x906E`.
  - `test_encode_escapes`: encoding `{0x7E, 0x11, 0x42}` gives `7E 7D 5E 7D 31 42 <fcs lo> <fcs hi> 7E`, with the FCS bytes escaped if needed.
  - `test_roundtrip_all_bytes`: a 256-byte frame 0..255 encodes, then decodes byte by byte to the same frame.
  - `test_bad_fcs`: flipping one payload byte gives `-EBADMSG`, and the next valid frame still decodes.
  - `test_garbage_then_frame`: random bytes without a flag, then a valid frame, decodes the frame.
  - `test_overflow_resync`: a frame of `size+10` bytes gives `-EMSGSIZE` once, and the following valid frame decodes.
  - `test_empty_frames_ignored`: `7E 7E 7E` yields nothing.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `hdlc.c`.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(spinel): add HDLC-lite framing`.

### Task 4: Opener — L2 stub and test mock driver

**Files:**
- Create: `opener:subsys/spinel/l2_stub.c` (built when `CONFIG_DECTNRP_SPINEL_L2_STUB`)
- Create: `opener:tests/subsys/spinel/common/{mock_driver.c,mock_driver.h,Kconfig.mock,CMakeLists.inc}`
- Test: `opener:tests/subsys/spinel/mock/…`

**Interfaces:**
- `l2_stub.c` provides what the samples currently define themselves (see `samples:samples/dectnrp-driver/src/dectnrp.c:196-223`): `NET_L2_INIT(DECTNRP_L2, …)` with `recv` → `NET_DROP`, `send` → `-ENOTSUP`, `enable` → 0, `flags` → 0; and `void dectnrp_l2_init(struct net_if *iface)`, which only sets `ctx->iface`. Kconfig help: "Spinel is the exclusive driver user; provides the DECTNRP L2 hook until the stack has a real L2. Disable if the application defines its own."
- `Kconfig.mock` (rsourced by each test's `Kconfig`, together with `source "Kconfig.zephyr"`):
  - `DECTNRP_DRIVER_MOCK` bool;
  - the `DECTNRP_DRIVER_RDCAP_*` ints with the nRF91 defaults (release 1, power class 2, NSS 1, diversity 1, rxgain −4, MCS 4, softbuffer 25344, HARQ 8, feedback delay 2, mu 1, beta 1);
  - `DECTNRP_DRIVER_MODEMTICKS_PER_SYMBOL` 2880;
  - `DECTNRP_DRIVER_MOCK_AUTO` bool (Task 8), with `_EMIT_PERIOD_MS` int 100.
- `mock_driver.h`:
  - `struct mock_driver { bool started; int start_calls, stop_calls; uint32_t network_id; bool nwid_filter; uint8_t nwid; bool rxaddr_filter; uint16_t rxaddr; dectnrp_driver_event_cb_t cb; struct dectnrp_driver_op *ops[16]; size_t n_ops; int schedule_ret; int start_ret; net_time_t time; }`;
  - `struct mock_driver *mock_driver_get(void)`;
  - `void mock_driver_reset(void)` (clears all but `cb`);
  - `struct net_if *mock_driver_iface(void)`;
  - `void mock_driver_emit(const struct dectnrp_driver_event *evt)`, which calls `cb(iface, evt)`.

  `stop()` emits `OP_FINISHED` for every recorded op still unfinished, matching nRF91 cancel-all. `get_time` returns `time` when non-zero, else `k_uptime_get() * 69120`. `get_hw_capabilities` returns `DECTNRP_HW_FILTER | DECTNRP_HW_TXTIME | DECTNRP_HW_RXTIME`. Created with `NET_DEVICE_INIT(…, DECTNRP_L2, NET_L2_GET_CTX_TYPE(DECTNRP_L2), DECTNRP_MTU)`, and `iface_api.init` calls `dectnrp_l2_init()` like the nRF91 driver.

- [ ] **Step 1: Write the failing tests** (`ZTEST_SUITE(spinel_mock)`):
  - `test_iface_found`: `net_if_get_first_by_type(&NET_L2_GET_NAME(DECTNRP)) == mock_driver_iface()`.
  - `test_schedule_records`: calling `schedule()` through the API records the op.
  - `test_stop_finishes_ops`: a registered cb sees one `OP_FINISHED` per recorded op after `stop()`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(spinel): add DECTNRP L2 stub and test mock driver`.

### Task 5: Opener — core: threads, dispatch, identity and driver properties

**Files:**
- Create: `opener:subsys/spinel/spinel_core.c`, `opener:subsys/spinel/spinel_internal.h`, `opener:subsys/spinel/transport/none.c` (for `_TRANSPORT_NONE`: a test hook transport)
- Test: `opener:tests/subsys/spinel/core/…` (uses the common mock and the `none` transport)

**Interfaces:**
- Transport contract (`spinel_internal.h`), one transport linked at a time:
  - `int spinel_transport_init(void)`;
  - `int spinel_transport_send(const uint8_t *frame, size_t len)` (blocking; called from the TX thread only);
  - core provides `void spinel_rx_submit(const uint8_t *frame, size_t len)`, ISR-safe: it copies into an RX-pool `net_buf` with `K_NO_WAIT`, puts it on the RX fifo, and on pool exhaustion counts `transport_errors`.
- Core internals:
  - `enum spinel_stat { SPINEL_STAT_OPS_ACCEPTED, …_OPS_REJECTED, …_EVENTS_SENT, …_EVENTS_DROPPED, …_FRAMES_RX, …_FRAMES_REJECTED, …_TRANSPORT_ERRORS, SPINEL_STAT_COUNT }` (order = `DRV_STATS` field order);
  - `void spinel_stat_inc(enum spinel_stat s)`;
  - `int spinel_send_frame(struct net_buf *buf)` (puts on the TX fifo);
  - `int spinel_reply(uint8_t tid, uint32_t cmd, spinel_prop_key_t prop, const char *fmt, ...)` (packs into a TX-pool buffer with `K_FOREVER` from the RX thread);
  - `int spinel_reply_status(uint8_t tid, uint32_t status)`;
  - `uint32_t spinel_status_from_errno(int err)` (`-EALREADY` → `SPINEL_STATUS_ALREADY`, `-ENOMEM` → `NOMEM`, `-EBUSY` → `BUSY`, `-EINVAL` → `INVALID_ARGUMENT`, others → base + |err| capped);
  - `struct net_if *spinel_iface(void)`.
- `none.c` test API (declared in `spinel_internal.h` under `CONFIG_DECTNRP_SPINEL_TRANSPORT_NONE`): `int spinel_test_recv(uint8_t *buf, size_t size, k_timeout_t t)` pops the next frame the core sent (from a `k_msgq` of `CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE`-sized items). Inbound frames are injected with `spinel_rx_submit()`.
- Behaviour:
  - Startup (`SYS_INIT(…, APPLICATION, …)`, §5.1): find the iface with `net_if_get_first_by_type(&NET_L2_GET_NAME(DECTNRP))`; register the event handler with `configure(EVENT_HANDLER)`; init the transport; start the RX and TX threads; send `CMD_PROP_VALUE_IS(LAST_STATUS, RESET_POWER_ON)` with TID 0. The spec's "overridable by Kconfig" iface is dropped (YAGNI, single driver).
  - Dispatch: header must have `FLG=0b10` and `IID=0`, otherwise drop and count `FRAMES_REJECTED`. `CMD_NOOP` → `LAST_STATUS OK`. `CMD_RESET` → Task 6 hook (here: stop the driver, send `LAST_STATUS RESET_SOFTWARE` with TID 0). GET/SET of the props below. `INSERT` → Task 6. Any other command → `INVALID_COMMAND`. Unknown prop → `PROP_NOT_FOUND`. Unpack failure → `PARSE_ERROR`.
  - Identity (§4.2):
    - `PROTOCOL_VERSION` `ii` 4,3;
    - `NCP_VERSION` `U` `"OPENER/<APP_VERSION_STRING or KERNEL_VERSION_STRING>; <dev->name>; " __DATE__`;
    - `INTERFACE_TYPE` `i` 0x4E52;
    - `VENDOR_ID` `i` 0;
    - `CAPS` = packed `i` list `[CAP_DECTNRP_DRV]`;
    - `LAST_STATUS` get → OK.
  - Driver props (§4.4). `DRV_ENABLED` set calls `start`/`stop`, treating `-EALREADY` as success, and replies `IS` with the new state. The filter/network-ID getters return the cached last-set values (initial: 0, inactive). `DRV_CAPS` packs `get_hw_capabilities()`, the `CONFIG_DECTNRP_DRIVER_RDCAP_*` values, `CONFIG_DECTNRP_DRIVER_MODEMTICKS_PER_SYMBOL`, `CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE` and `CONFIG_DECTNRP_SPINEL_OP_SLOTS`.

- [ ] **Step 1: Write the failing tests** (`ZTEST_SUITE(spinel_core)`; the `before` hook calls `mock_driver_reset()` and drains `spinel_test_recv`). Helpers `send_req(tid, cmd, prop, fmt, …)` and `recv_reply(&tid, &cmd, &prop, &payload, &len)` use `spinel_datatype_pack/unpack`.
  - `test_boot_reset_notification`: the first frame ever received is TID 0 `PROP_VALUE_IS LAST_STATUS RESET_POWER_ON`. This suite must run first; name it `test_00_…`.
  - `test_noop`: TID 3 → reply TID 3, `LAST_STATUS OK`.
  - `test_protocol_version`: values 4, 3.
  - `test_interface_type`: 0x4E52.
  - `test_caps_contains_drv`: the CAPS list contains 0x3D00.
  - `test_enable_disable`: set `DRV_ENABLED` true → `mock->started`, reply `b` true; set again → still OK (`-EALREADY` path via `start_ret = -EALREADY`); set false → stopped.
  - `test_network_id_roundtrip`: set 0x32545279 → `mock->network_id == 0x32545279`, get returns it.
  - `test_filters`: set `DRV_FILTER_SHORT_NWID` (true, 0x79) and `DRV_FILTER_SHORT_RXADDR` (true, 0xBEEF) → mock fields set, getters match.
  - `test_time`: `mock->time = 123456789` → get `X` returns it.
  - `test_caps_layout`: the unpacked `DRV_CAPS` give hw_caps = 0x904, ticks/symbol 2880, max frame 1300, op slots 8, rxgain −4.
  - `test_unknown_prop`: get 0x3DFF → `LAST_STATUS PROP_NOT_FOUND`, same TID.
  - `test_unknown_cmd`: cmd 0x7F → `INVALID_COMMAND`.
  - `test_truncated_set`: set `DRV_NETWORK_ID` with a 2-byte value → `PARSE_ERROR`.
  - `test_bad_header_ignored`: a frame with header 0x45 → no reply within 50 ms, and `FRAMES_REJECTED` incremented (read through `DRV_STATS`).
  - `test_driver_errno_status`: `start_ret = -EIO` → status `0x3C80 + EIO`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** RX thread: `k_fifo_get` → dispatch → `net_buf_unref`. TX thread: `k_fifo_get` → `spinel_transport_send` → `net_buf_unref`. Use `NET_BUF_POOL_DEFINE` pools for RX, TX and events, each with data size `CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(spinel): add core dispatch and driver-level properties`.

### Task 6: Opener — op broker: slots, `DRV_OP_QUEUE`, event forwarding, reset

**Files:**
- Create: `opener:subsys/spinel/spinel_ops.c`
- Modify: `opener:subsys/spinel/spinel_core.c` (INSERT and RESET hooks), `spinel_internal.h`
- Test: `opener:tests/subsys/spinel/ops/…`

**Interfaces:**
- Internal API:
  - `int spinel_ops_insert(uint8_t tid, const uint8_t *value, size_t len)`. It replies itself: `INSERTED(DRV_OP_QUEUE, "S" token)` on success, `LAST_STATUS` otherwise;
  - `void spinel_ops_event_cb(struct net_if *iface, const struct dectnrp_driver_event *evt)` (the registered driver handler);
  - `int spinel_ops_reset(k_timeout_t timeout)`: `stop()`, then wait until all slots are free, then force-free stragglers and log a warning;
  - `size_t spinel_ops_in_flight(void)`.
- `struct spinel_op_slot`: `bool used`, `uint16_t token`, `struct dectnrp_driver_op op`, `struct dectnrp_rssi1_result rssi1`. Find a slot from an event's op with `CONTAINER_OF(op, struct spinel_op_slot, op)`.
- TX ops: allocate a `net_pkt` holding `data` with `net_pkt_alloc_with_buffer(iface, len, AF_UNSPEC, 0, K_NO_WAIT)` + `net_pkt_write`. Unref it when the slot is released, as `samples:…/dectnrp_operation.c:dectnrp_free_operation` does. Validate `len ≥ PCC size for phy_header_type` (5 or 10) and `len ≤ PCC + DECTNRP_MTU`. Otherwise `INVALID_ARGUMENT`.
- Events (§4.6, §5.3): packed directly in the callback into an event-pool buffer (`K_NO_WAIT`), header TID 0, `CMD_PROP_VALUE_IS`, `DRV_EVENT`, prefix `SClS` (token, wire code, status, dropped).
  - `dropped` = `atomic_set(&pending_drops, 0)`, saturated to 0xFFFF.
  - On pool exhaustion: `atomic_inc(&pending_drops)` and `EVENTS_DROPPED++`.
  - `OP_FINISHED` uses a separate `finished_pool` of `CONFIG_DECTNRP_SPINEL_OP_SLOTS` buffers of 16 bytes, whose **destroy callback releases the slot** (index stored in the buffer's user data). Hence the slot frees only after the TX thread has written the frame, and the pool can never run dry.
  - `MSG_RECEIVED` `valid` = rssi2_valid | snr_valid<<1. `MSG_ERROR` adds pcc_valid<<2, and its `d pcc` holds `phy_type ? 10 : 5` bytes when pcc_valid, else empty.
  - `OP_RESULTS` packs the slot's `rssi1.channel` and `DECTNRP_SUBSLOTS_PER_FRAME` bytes.
  - `channel` in every event = `slot->op.channel`.

- [ ] **Step 1: Write the failing tests** (`ZTEST_SUITE(spinel_ops)`):
  - `test_insert_rx_scheduled`: insert token 7, RX, ch 1677, flags 1, start 1000, duration 691200, rssi 0. Expect `INSERTED` with token 7, and the mock op has `type RX`, `rx.mode.is_scheduled 1`, `start_time 1000`, `rx.duration 691200`, `channel 1677`.
  - `test_insert_tx`: data `[5-byte PCC]"Hello World!"`. The mock op's `tx.pkt` holds 17 bytes and the transport params match.
  - `test_insert_tx_too_short`: 3 bytes of data → `INVALID_ARGUMENT`, and no op scheduled.
  - `test_duplicate_token`: second insert of token 7 while in flight → `ALREADY`.
  - `test_slots_exhausted`: insert 8 ops, then the 9th → `NOMEM`.
  - `test_schedule_error`: `schedule_ret = -EBUSY` → `BUSY`, and the slot is not leaked (the next insert succeeds).
  - `test_msg_received_event`: emit `MSG_RECEIVED` (phy_type 0, rssi2_valid, rssi2 0x50, snr 12, start 42, pcc 5 B, pdc "Hello World!") for token 7. The decoded event is token 7, code 1, status 0, dropped 0, valid 0b01, start 42, channel 1677, the exact pcc and pdc.
  - `test_msg_error_event_no_pcc`: pcc_valid 0 → empty `d pcc`, valid bit2 clear.
  - `test_rssi1_results`: an RSSI1 op, then `OP_RESULTS` → 48 bytes plus the channel.
  - `test_finished_frees_slot`: after `OP_FINISHED` is received by the test transport, `spinel_ops_in_flight() == 0` and token 7 can be reused.
  - `test_event_pool_exhaustion`: block the TX thread (the test transport `send` waits on a semaphore), then emit `CONFIG_DECTNRP_SPINEL_EVENT_BUFS + 5` `MSG_RECEIVED` and one `OP_FINISHED`; then unblock. Expect: exactly `EVENT_BUFS` `MSG_RECEIVED` delivered, the `OP_FINISHED` is delivered with `dropped == 5`, `DRV_STATS` events_dropped == 5, and the slot is freed.
  - `test_reset_with_ops_in_flight`: 3 ops in flight, then `CMD_RESET`. Expect `mock->stop_calls == 1`, three `OP_FINISHED`, then `LAST_STATUS RESET_SOFTWARE` TID 0, and in-flight 0.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Make the test transport's blocking `send` hook a `none.c` test API: `void spinel_test_block_tx(bool block)`.
- [ ] **Step 4: Run, expect PASS.** Also rerun the `core` suite.
- [ ] **Step 5: Commit** `feat(spinel): add op broker and driver event forwarding`.

### Task 7: Opener — UART HDLC transport and native_sim simulated device

**Files:**
- Create: `opener:subsys/spinel/transport/uart_hdlc.c`
- Create: `opener:tests/subsys/spinel/simdev/{CMakeLists.txt,Kconfig,prj.conf,testcase.yaml,app.overlay,src/main.c}`
- Modify: `opener:tests/subsys/spinel/common/mock_driver.c` (auto mode)

**Interfaces:**
- `DECTNRP_SPINEL_TRANSPORT_UART` depends on `$(dt_chosen_enabled,opener,spinel-uart)` and selects `SERIAL` and `UART_INTERRUPT_DRIVEN`. Behaviour:
  - Interrupt-driven RX feeds `spinel_hdlc_decode_byte` in ISR context, with the decoder buffer sized `CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE`. A complete frame goes to `spinel_rx_submit`. `-EBADMSG`/`-EMSGSIZE` → `spinel_stat_inc(FRAMES_REJECTED)`.
  - `spinel_transport_send` HDLC-encodes into a static buffer of `SPINEL_HDLC_MAX_ENCODED(MAX_FRAME_SIZE)` and transmits it via an IRQ-driven TX with a `k_sem` for completion.
- Mock auto mode (`CONFIG_DECTNRP_DRIVER_MOCK_AUTO`), simulating the nRF91 for host integration tests:
  - Time = `k_uptime_get() * 69120`.
  - An RX op becomes active at `start_time` (immediately if not scheduled) and emits `OP_FINISHED` at `start + duration`.
  - Every `EMIT_PERIOD_MS`, if an RX window is active, emit one canned frame and finish that op early (single-shot semantics, as found in Task 1). Every 10th canned frame is a `MSG_ERROR` with `DECTNRP_ERROR_PDC_CRC` (pcc valid). The others are `MSG_RECEIVED` with phy_type 0, PCC = the 5-byte header `dectnrp-driver` sends, PDC = `"Hello World!"`, rssi2 0x50, snr 20.
  - TX → `OP_FINISHED` after 1 ms. RSSI1 → `OP_RESULTS` with all subslots 0x60, then `OP_FINISHED`.
  - Implemented with one `k_work_delayable` per recorded op.
- simdev:
  - `app.overlay`: `chosen { opener,spinel-uart = &uart1; }`.
  - `prj.conf`: `CONFIG_DECTNRP_SPINEL_TRANSPORT_UART=y`, `CONFIG_DECTNRP_DRIVER_MOCK=y`, `CONFIG_DECTNRP_DRIVER_MOCK_AUTO=y`, `CONFIG_NATIVE_SIM_SLOWDOWN_TO_REAL_TIME=y`, `CONFIG_UART_NATIVE_PTY_1=y` if that symbol exists in this Zephyr (check `drivers/serial/Kconfig.native_pty`).
  - `main.c` is empty: the subsystem self-starts.
  - `testcase.yaml`: `build_only: true` (it is a fixture for host tests).

- [ ] **Step 1: Build the simdev, expect FAIL** (no transport yet): `west build -p -b native_sim/native/64 -d build/simdev deps/dectnrp-opener/tests/subsys/spinel/simdev`
- [ ] **Step 2: Implement the UART transport and the mock auto mode.**
- [ ] **Step 3: Smoke-test against the pty.** Run `build/simdev/zephyr/zephyr.exe`. It prints the `/dev/pts/N` of uart1; note the exact line format, which Task 12 parses. Then send an HDLC-encoded `CMD_PROP_VALUE_GET(PROTOCOL_VERSION)` and decode the reply:
  ```bash
  python3 - /dev/pts/N <<'EOF'
  import sys, serial, binascii
  def fcs(d):
      c = 0xffff
      for b in d:
          c ^= b
          for _ in range(8): c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
      return (~c) & 0xffff
  def enc(f):
      f = f + fcs(f).to_bytes(2, "little"); out = bytearray([0x7e])
      for b in f: out += bytes([0x7d, b ^ 0x20]) if b in (0x7e, 0x7d, 0x11, 0x13, 0xf8) else bytes([b])
      return bytes(out + b"\x7e")
  s = serial.Serial(sys.argv[1], timeout=1); s.write(enc(bytes([0x81, 0x02, 0x01]))); print(binascii.hexlify(s.read(64)))
  EOF
  ```
  Expected: the boot frame `…80 06 00 70…`, then `7e 81 06 01 04 03 <fcs> 7e`.
- [ ] **Step 4: Run all Spinel suites, expect PASS**: `west twister -T deps/dectnrp-opener/tests/subsys/spinel -p native_sim/native/64`
- [ ] **Step 5: Commit** `feat(spinel): add UART HDLC transport and native_sim simulated device`.

### Task 8: Firmware sample and protocol documentation

**Files:**
- Create: `samples:samples/dectnrp-spinel/{CMakeLists.txt,prj.conf,sample.yaml,README.rst,src/main.c,boards/nrf9151dk_nrf9151_ns.{conf,overlay}}`. The overlay includes the same `nrf91x1_partition_without_bootloader.dtsi` / `nrf91x1_dect_modem.dtsi` as `dectnrp-driver`; copy them into `boards/`.
- Create: `opener:doc/spinel.rst`; Modify: the opener doc index (toctree that lists the driver docs).

**Interfaces:**
- Sample:
  - `prj.conf` = `dectnrp-driver`'s minus the sample-specific symbols, plus `CONFIG_DECTNRP_SPINEL=y`, `CONFIG_DECTNRP_SPINEL_TRANSPORT_UART=y`.
  - DK overlay: `chosen { opener,spinel-uart = &uart0; };` with `&uart0 { current-speed = <1000000>; }`. Logs go on RTT, and the console moves off uart0: `CONFIG_UART_CONSOLE=n`, `CONFIG_LOG_BACKEND_UART=n`.
  - `main.c` logs a banner and returns.
  - `sample.yaml` scenario `sample.dectnrp_spinel` on `nrf9151dk/nrf9151/ns`.
- `doc/spinel.rst`: §4 of the spec turned into reference documentation: framing, identity, ranges, property table, op and event layouts, the reset/OP_FINISHED guarantee. Also the DK quick start, and the deviation notes (IRQ polarity and BUSY are documented in plan 3).

- [ ] **Step 1: Build, expect PASS**: `west build -p -b nrf9151dk/nrf9151/ns -d build/nrf9151dk/nrf9151/ns/dectnrp-spinel samples/dectnrp-spinel`. Check that `build/.../zephyr/.config` has `CONFIG_DECTNRP_SPINEL_TRANSPORT_UART=y` and `CONFIG_DECTNRP_SPINEL_L2_STUB=y`.
- [ ] **Step 2: Build the docs, expect no new warnings** (command in CLAUDE.md "Building the stack's docs").
- [ ] **Step 3: Commit** in opener-samples `samples: add dectnrp-spinel firmware`, and in opener `doc: add Spinel interface specification`.

### Task 9: linux-tools — package skeleton, vendored codec, HDLC, constants

**Files:**
- Create: `tools:pyproject.toml`, `tools:.gitignore`, `tools:README.md`, `tools:LICENSE` (Apache-2.0)
- Create: `tools:src/opener_tools/__init__.py`, `tools:src/opener_tools/spinel/{__init__.py,codec.py,const.py,hdlc.py,dectnrp_constants.py,NOTICE}`
- Test: `tools:tests/spinel/{test_codec.py,test_hdlc.py,test_constants.py}`, `tools:tests/conftest.py`

**Interfaces:**
- `pyproject.toml`:
  - project `opener-tools`, `requires-python >=3.11`, deps `pyserial>=3.5`;
  - extras `spi = [spidev, gpiod>=2]`, `hub = [aiohttp>=3.9]`, `dev = [pytest, pytest-asyncio, ruff]`;
  - scripts are added by later tasks;
  - `[tool.pytest.ini_options] asyncio_mode="auto"`, `markers = ["hw: needs hardware (--transport)", "simdev: needs OPENER_SIMDEV_EXE"]`;
  - ruff line-length 100, `extend-exclude` the vendored `codec.py`/`const.py` from formatting only.
- `codec.py`: pyspinel's `SpinelCodec` class only (pyspinel `codec.py` lines 74–417: `parse_*`, `parse_field(s)`, `encode_*`, `encode_fields`, `encode_packet`), imports reduced to `struct`. `const.py`: the `SPINEL` class only. Record the modifications in the vendored header.
- `hdlc.py` (ours, FCS table logic from pyspinel `hdlc.py`): `def hdlc_encode(frame: bytes) -> bytes`; `class HdlcDecoder: def __init__(self, max_frame: int = 1300); def feed(self, data: bytes) -> list[bytes]; errors: int`. Same rules as Task 3.
- `dectnrp_constants.py`: the same names as the C header, minus the `DECTNRP_SPINEL_` prefix (`PROP_DRV_ENABLED = 0x3D00`, …, `FMT_OP_PREFIX = "SCSCX"`, …, `class OpType(IntEnum)`, `class EventCode(IntEnum)`).

- [ ] **Step 1: Write the failing tests:**
  - `test_codec.py`: `encode_fields("SCSCX", 0x1234, 1, 1677, 1, 0x0102030405060708).hex() == "341201 8d0601 0807060504030201".replace(" ","")`. Parsing returns the tuple. `encode_fields("i", 0x3D00)` round-trips as a packed uint (`b"\x80\x7a"`).
  - `test_hdlc.py`: the same cases as Task 3 (FCS of `b"123456789"` = 0x906E, escapes, roundtrip of 0..255, bad FCS skipped with `errors == 1`, garbage then frame, overflow resync, data split across `feed` calls one byte at a time).
  - `test_constants.py`: parse `../opener-samples/deps/dectnrp-opener/include/zephyr/net/dectnrp_spinel.h` (path from env `OPENER_DIR`, default `../opener-samples/deps/dectnrp-opener`; `pytest.skip` if absent) with a regex over `DECTNRP_SPINEL_(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)` and `#define DECTNRP_SPINEL_FMT_(\w+)\s+"(\w*)"`. Assert every C name exists in Python with the same value.
- [ ] **Step 2: Run, expect FAIL**: `.venv/bin/pytest tests/spinel -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS**, plus `ruff check && ruff format --check`.
- [ ] **Step 5: Commit** `feat: add package skeleton and Spinel codec`.

### Task 10: linux-tools — transports (uart, tcp) and URL factory

**Files:**
- Create: `tools:src/opener_tools/transport/{__init__.py,base.py,uart.py,tcp.py}`
- Test: `tools:tests/transport/{test_uart.py,test_tcp.py,test_url.py}`

**Interfaces:**
- `base.py`: `class TransportClosed(Exception)`; `class Transport(abc.ABC)` with `async def open(self) -> None`, `async def close(self) -> None`, `async def send(self, frame: bytes) -> None`, `async def recv(self) -> bytes` (whole Spinel frame; raises `TransportClosed`), `errors: int`, and async context manager support.
- `uart.py`: `UartTransport(path: str, baud: int = 1_000_000)`. pyserial opened non-blocking and wrapped with `loop.add_reader(fd)`, feeding `HdlcDecoder`, so there is no thread.
- `tcp.py`: `TcpTransport(host: str, port: int)`, frames `[len u16 LE][frame]` (§6.1).
- `__init__.py`: `def open_transport(url: str) -> Transport`:
  - `uart:///dev/ttyACM0?baud=1000000` → `UartTransport`;
  - a bare `/dev/…` path → uart;
  - `tcp://host:port` → `TcpTransport`;
  - `spi://` → `NotImplementedError("plan 3")`;
  - anything else → `ValueError`.

- [ ] **Step 1: Write the failing tests:**
  - `test_url.py`: the parse cases above, including the default baud and `?baud=115200`.
  - `test_uart.py`: use `os.openpty()`, give the transport the slave name, and write HDLC frames raw on the master fd. `recv()` returns 2 frames split across 3 writes; `send()` bytes read on the master are `hdlc_encode(frame)`; garbage before a frame increments `errors`.
  - `test_tcp.py`: an `asyncio.start_server` echo peer; round-trip of a 1300-byte frame; a peer close makes `recv()` raise `TransportClosed`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add UART and TCP transports`.

### Task 11: linux-tools — async Spinel client

**Files:**
- Create: `tools:src/opener_tools/spinel/client.py`
- Test: `tools:tests/spinel/test_client.py`, `tools:tests/fakes.py` (a `FakeTransport` with `inject(frame)` and a `sent: asyncio.Queue`)

**Interfaces:**
- `@dataclass(frozen=True) class SpinelFrame: tid: int; cmd: int; prop: int | None; value: bytes`
- Exceptions: `SpinelError(status: int)` (str shows the `SPINEL.STATUS_*` name, or `errno <n>` for the Opener base range), `SpinelTimeout`, `DeviceReset(status: int)`.
- `class SpinelClient`:
  - `__init__(self, transport: Transport, timeout: float = 1.0)`;
  - `async def start(self) -> None` (spawns the reader task);
  - `async def close(self) -> None`;
  - `async def request(self, cmd: int, prop: int | None = None, value: bytes = b"") -> SpinelFrame`;
  - `async def get(self, prop: int) -> bytes`;
  - `async def set(self, prop: int, value: bytes) -> bytes`;
  - `async def insert(self, prop: int, value: bytes) -> bytes`;
  - `async def reset(self) -> None` (sends `CMD_RESET` and waits for TID-0 `LAST_STATUS RESET_*`);
  - `def subscribe(self) -> asyncio.Queue[SpinelFrame]` (every TID-0 frame, including resets);
  - `def unsubscribe(self, q) -> None`;
  - `on_reset: list[Callable[[int], None]]` (called on an unsolicited `RESET_*` not caused by `reset()`).
- Rules:
  - TIDs cycle 1..15. A TID is not reused while a request on it is pending.
  - After a timeout the TID stays quarantined for `timeout` seconds, so a stale reply cannot satisfy a later request (Review Focus 5).
  - When all 15 TIDs are busy, `request` waits.
  - A reply whose `cmd` is `PROP_VALUE_IS` with prop `LAST_STATUS` and status ≠ OK raises `SpinelError`. A status OK reply to a `CMD_NOOP` resolves.
  - `get`/`set`/`insert` return the value. They check that the reply prop matches, else `SpinelError(PARSE_ERROR)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_get_matches_tid`: a fake answers TID 1 → `get` returns the value.
  - `test_status_error`: a reply with `LAST_STATUS PROP_NOT_FOUND` raises `SpinelError`, with `.status == SPINEL.STATUS_PROP_NOT_FOUND`.
  - `test_errno_status_str`: status 0x3C80+5 → `str` contains `errno 5`.
  - `test_timeout`: no reply → `SpinelTimeout` after ≈ `timeout`.
  - `test_stale_reply_ignored`: request A on TID 1 times out. Then 15 more requests go out, and none of them reuses TID 1 within the quarantine window. A late TID-1 reply is dropped.
  - `test_concurrent_requests`: 20 concurrent `get`s all resolve, in any order.
  - `test_unsolicited_fanout`: two subscribers both receive a TID-0 frame.
  - `test_reset_ok`: `reset()` resolves when the fake sends `LAST_STATUS RESET_SOFTWARE` TID 0 and does not trigger `on_reset`.
  - `test_unsolicited_reset_callback`: an injected `RESET_POWER_ON` calls `on_reset` with that status.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add asyncio Spinel client`.

### Task 12: linux-tools — driver-level `Radio` API, events, timing

**Files:**
- Create: `tools:src/opener_tools/dectnrp/{__init__.py,radio.py,events.py,timing.py}`
- Test: `tools:tests/dectnrp/{test_events.py,test_timing.py,test_radio.py}`

**Interfaces:**
- `events.py`. All are frozen dataclasses with the common fields `token: int`, `status: int`, `dropped: int`:
  - `MsgReceived(…, phy_type: int, start_time: int, rssi2: int | None, snr: int | None, channel: int, pcc: bytes, pdc: bytes)`;
  - `MsgError(…, phy_type, start_time, rssi2, snr, channel, pcc: bytes | None)`;
  - `OpResults(…, channel: int, rssi1: bytes)`;
  - `OpFinished(…)`.
  
  Also `def decode_event(value: bytes) -> Event`, with an unknown code → `ValueError`.
- `timing.py`: `@dataclass(frozen=True) class Timing: ticks_per_symbol: int`, with properties `ticks_per_subslot` (×5), `ticks_per_slot` (×10), `ticks_per_frame` (×240) and `ticks_per_ms` (`ticks_per_frame // 10`), plus methods `ms_to_ticks(ms: float) -> int` and `ticks_to_ms(t: int) -> float`.
- `radio.py`:
  - `@dataclass DrvCaps` (fields in §4.4 order: `hw_caps, release, power_class, nss_rx, diversity, rx_gain_db, mcs_max, soft_buffer, harq_max, harq_delay, mu_max, beta_max, ticks_per_symbol, max_frame, op_slots`; property `timing -> Timing`).
  - `@dataclass DrvStats` (`ops_accepted, ops_rejected, events_sent, events_dropped, frames_rx, frames_rejected, transport_errors`).
  - `@dataclass TxParams(phy_header_type: int, tbs: int, mu: int, beta: int, mcs: int, slots: int, lbt_period: int = 0, lbt_rssi_threshold: int = 0)`.
  - `class Op` (awaitable; `token: int`; `events: list[Event]`; `done: bool`; `def __await__` resolves to the `OpFinished`; raises `DeviceReset` if a reset intervenes).
  - `class Radio`:
    - `__init__(self, client: SpinelClient)`;
    - `async def init(self) -> DrvCaps` (checks `INTERFACE_TYPE == 0x4E52` and `CAP_DECTNRP_DRV` in `CAPS`, else raises `RuntimeError`; caches caps);
    - `caps: DrvCaps`;
    - `async def reset(self) -> None`;
    - `async def enable(self, on: bool = True) -> None`;
    - `async def time(self) -> int`;
    - `async def set_network_id(self, nwid: int) -> None`;
    - `async def set_filter_short_nwid(self, active: bool, value: int = 0) -> None`;
    - `async def set_filter_short_rxaddr(self, active: bool, value: int = 0) -> None`;
    - `async def stats(self) -> DrvStats`;
    - `async def schedule_rx(self, channel: int, duration: int, start: int | None = None, expected_rssi: int = 0, beacon: bool = False) -> Op` (`start=None` means immediate, flag bit0 clear);
    - `async def schedule_tx(self, channel: int, data: bytes, params: TxParams, start: int | None = None, lbt: bool = False, harq: bool = False, beacon: bool = False) -> Op`;
    - `async def schedule_rssi1(self, channel: int, subslots: int, start: int | None = None) -> Op`;
    - `def events(self) -> AsyncIterator[Event]` (every event, including the ones that belong to Ops).
  - Tokens cycle 1..65535, skipping in-flight ones. **The Op is registered under its token before the insert is sent** (Review Focus 1). If the insert fails, it is unregistered and the `SpinelError` is re-raised. On a client reset (`on_reset`, or `reset()`), every pending Op fails with `DeviceReset`.

- [ ] **Step 1: Write the failing tests** (`FakeTransport`, plus a small `ScriptedDevice` helper in `tests/fakes.py` that answers GET/SET/INSERT from a dict and lets the test inject events):
  - `test_events.py`: decode a `MSG_RECEIVED` built with `encode_fields` (valid=0b11 → rssi2/snr set; valid=0 → `None`); decode a `MSG_ERROR` with empty pcc → `pcc is None`; decode `OP_RESULTS` with 48 bytes.
  - `test_timing.py`: `Timing(2880)`: frame 691200, ms 69120, subslot 14400, `ms_to_ticks(20) == 1382400`.
  - `test_radio.py`:
    - `test_init_checks_identity`: a wrong interface type raises.
    - `test_schedule_rx_payload`: the bytes sent equal `encode_fields("SCSCXLC", tok, 1, 1677, 1, 1000, 691200, 0)`.
    - `test_event_before_inserted`: the device emits `MSG_RECEIVED` for the token *before* replying `INSERTED`. The op's `events` contains it, and awaiting the op returns after `OP_FINISHED`.
    - `test_insert_error_unregisters`: `NOMEM` raises `SpinelError`, and the token is free again.
    - `test_reset_fails_pending_ops`: two ops pending, then an injected `RESET_POWER_ON`: both raise `DeviceReset`.
    - `test_tx_payload`: the TX encoding matches `FMT_OP_PREFIX + FMT_OP_TX`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add driver-level Radio API`.

### Task 13: Host ↔ simulated device integration

**Files:**
- Create: `tools:tests/integration/{conftest.py,test_simdev.py}`

**Interfaces:**
- Fixture `simdev_url`. It starts `$OPENER_SIMDEV_EXE` (skip if unset). The path comes from Task 7, e.g. `../opener-samples/build/simdev/zephyr/zephyr.exe`. The fixture parses uart1's pty from stdout, using the line format recorded in Task 7 step 3, yields `uart:///dev/pts/N`, and kills the process on teardown. Tests are marked `simdev`.

- [ ] **Step 1: Write the tests:**
  - `test_identity`: `Radio.init()` → `caps.ticks_per_symbol == 2880`, `op_slots == 8`.
  - `test_reset_enable_time`: `reset`, `enable(True)`, then two `time()` calls 100 ms apart differ by 6 912 000 ± 10 %.
  - `test_rx_window_receives`: an immediate RX window of 500 ms → at least one `MsgReceived` with `pdc == b"Hello World!"`, then `OpFinished`.
  - `test_pipelined_windows`: 3 scheduled 50 ms windows back to back from `time() + 20 ms` → 3 `OpFinished` in token order.
  - `test_stats_count`: after the above, `stats().ops_accepted >= 3`.
- [ ] **Step 2: Build the simdev (Task 7 step 1) and run**: `OPENER_SIMDEV_EXE=… .venv/bin/pytest -m simdev -v`. Expect PASS. A failure here is debugged with superpowers:systematic-debugging before any fix.
- [ ] **Step 3: Commit** `test: add simulated device integration tests`.

### Task 14: DK hardware smoke test (§8.3.2, first half)

**Files:**
- Create: `tools:tests/hw/{conftest.py,test_smoke.py}` (marker `hw`; the `--transport URL` option goes in `tests/conftest.py`; skip when the option is absent)

- [ ] **Step 1: Write the tests**, reusing Task 13's first four tests' bodies through a shared helper module `tests/integration/scenarios.py` (move them there), parametrised on the URL. The RX test asserts only `OpFinished`, not reception, since a peer is optional. Add `test_rx_from_peer`, skipped unless `--peer` is given: a 5 s immediate RX window receives a frame whose `pdc` starts with `b"Hello World!"` from a `dectnrp-driver` DK.
- [ ] **Step 2: Flash the DK**: `west flash -d build/nrf9151dk/nrf9151/ns/dectnrp-spinel --dev-id <Device1>`.
- [ ] **Step 3: Run**: `.venv/bin/pytest -m hw --transport uart:///dev/ttyACM<n> -v`, then again with `--peer` while Device2 runs `dectnrp-driver`. Expect PASS. Record the VCOM port that carries uart0 (the nRF9151 DK exposes several).
- [ ] **Step 4: Commit** `test: add hardware smoke tests`, then update `CLAUDE.md` (root, not a repo) with the twister, simdev and pytest commands.

### Task 15: CI (§8.4)

The opener repo is not a west workspace and has no workflow able to run twister. The Spinel ztests therefore run in opener-samples' CI, which already checks out opener through west. Raise this with the user as an upstream point; the alternative is a `west.yml` in opener.

**Files:**
- Modify: `samples:.github/workflows/build-samples.yml`. Add a build step for `samples/dectnrp-spinel/` to `${BUILD_BASE}/dectnrp-spinel`, staging and upload like `dectnrp-driver`, plus a new job `spinel-tests` with the same setup steps that runs `west twister -T deps/dectnrp-opener/tests -p native_sim/native/64 --inline-logs`.
- Create: `tools:.github/workflows/ci.yml`: Python 3.11 and 3.14 matrix, `pip install -e '.[dev]'`, `ruff check`, `ruff format --check`, `pytest` (the simdev and hw markers skip themselves).

- [ ] **Step 1: Validate the workflows locally.** Run `actionlint` on both files if installed. Otherwise run each `run:` command by hand: the twister command from `opener-samples/`, and the pytest/ruff commands from `linux-tools/` in a fresh venv. Expect PASS.
- [ ] **Step 2: Commit** in opener-samples `ci: build dectnrp-spinel and run Spinel ztests`, and in linux-tools `ci: add pytest and ruff workflow`.
