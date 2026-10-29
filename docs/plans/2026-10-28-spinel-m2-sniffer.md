# Spinel M2 — host-driven NR+ sniffer over UART: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Execution mode:** the same as plan 1. Claude implements task by task and never commits. At each "Commit" step it gives the commit command(s) with the `Co-Authored-By` trailer and waits for the user's approval.

**Goal:** `nrp-sniffer`, a Python tool that drives the `dectnrp-spinel` firmware to capture DECT NR+ frames into a `DLT_DECT_NR_TAP` (304) pcap, also usable as a Wireshark extcap. Validated end to end on an nRF9131EK capturing a `dectnrp-driver` peer.

**Architecture:** `RxPipeline` keeps `depth` scheduled RX windows queued on the modem and yields received and error events. `capture/` turns each event into a DECT-NR-TAP record and writes pcap. The `nrp-sniffer` CLI wires the two together, both as a plain CLI and in extcap mode. Everything is built on plan 1's `Radio` API.

**Tech Stack:** Python ≥ 3.11 asyncio, pytest, the native_sim simdev from plan 1, Wireshark/tshark ≥ 4.7.4 for decode checks.

**Spec:** `linux-tools/docs/specs/2026-10-28-opener-spinel-design.md` §6.3–6.5, §7, §8.3.2. Depends on plan 1 (`2026-10-28-spinel-m1-foundation.md`) being complete, and on the probe results from its Tasks 1 and 2.

## Global Constraints

- Plan 1's Global Constraints apply: repos, sign-off, headers, ruff.
- TAP record layout (Wireshark `epan/dissectors/packet-dect-nr-tap.c`, master as of 2026-10-28):
  - 8-byte header: `u8 version=1, u8 0, u16 LE tlvs_len_bytes, u16 LE body_len, u16 0`.
  - Then TLVs of 8 bytes each: `u16 LE type, u16 LE value_len (2 or 4), 4-byte value LE zero-padded`.
  - Then the body = PCC ‖ PDC.
  - At least 8 TLVs: pad with type 0, len 0. Otherwise the dissector rejects the record (minimum record length 72).
- TLV types: 1 data type, 2 header status, 3 PCC RSSI2, 4 PCC SNR, 5 PDC RSSI2, 6 PDC SNR, 7 transaction ID, 8 channel, 10 modem time hi, 11 modem time lo.
  - Data type values: 1/2 = 40/80-bit PCC + PDC; 3/4 = 40/80-bit PCC, PDC absent (CRC error); 5 = neither.
  - Header status values: 0 valid with PDC, 1 invalid / PDC absent, 2 valid but no PDC.
- **Modem time TLVs must be emitted LO (11) before HI (10).** The dissector combines the pair as `(hi << 32) | prev`, so the reverse order decodes wrongly.
- pcap: classic little-endian µs header `struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 304)`, as in the Deveritec writer. Timestamps are host time.
- Defaults:
  - channel 1677 and network ID 0x32545279 (`dectnrp-driver` sample values, not Deveritec's);
  - RX reception mode and window: **decided in plan 1 Task 2 step 4.** The planned outcome is semi-continuous with 500 ms windows if the probe shows it receives every `dectnrp-driver` frame (type 2 format 0, which continuous mode cannot receive). Otherwise single-shot with 20 ms windows: a received frame ends a single-shot window early, and short windows bound the uncovered remainder. Below, `DEFAULT_RX_MODE` / `DEFAULT_WINDOW_MS` stand for the decided values, defined once in `cli/sniffer.py`;
  - depth 4;
  - lead 20 ms. The probe found that 1 ms is accepted; the 20 ms covers host and link latency. Lead is clamped to at least `caps.sched_op_startup + caps.rx_startup`.
  - Window spacing: consecutive windows are separated by `caps.sched_op_transition` (D11; 25 920 ticks = 375 µs on nRF9131). Exactly adjacent windows are rejected by the modem (plan 1 Task 1). If the device reports 0 (unknown), use 1 ms.
- Deveritec attribution: `capture/tap.py` and `capture/extcap.py` carry `# Derived from Deveritec dectnrp-sniffer (Apache-2.0), opener-samples deveritec/dectnrp-sniffer:samples/dectnrp-sniffer/`.

## Review Focus

1. **Host stalls** (GC pause, slow SSH pipe, Wireshark FIFO not read). The pipeline falls behind, so new windows would start in the past. It must resync and report one gap, not error out or spin → `test_resync_on_late`, Task 1.
2. **Device rejects a scheduled op** (driver errno because start is in the past, or `NOMEM`). It must resync, not crash → `test_resync_on_reject`, Task 1.
3. **Device reboots mid-capture** (power glitch, watchdog). The sniffer re-initialises (network ID, enable), logs the gap and keeps writing the same pcap → `test_device_reset_reinit`, Task 4.
4. **Ctrl-C / SIGTERM / Wireshark stopping the extcap.** It must stop scheduling, drain outstanding ops, disable the radio, flush the pcap and exit 0, never leaving the radio enabled with ops queued → `test_clean_stop`, Task 4, and `test_extcap_fifo_closed`, Task 5.
5. **`MSG_ERROR` without a valid PCC.** It is written as data type 5 with an empty body, never with a stale PCC → `test_msg_error_no_pcc`, Task 2.

---

### Task 1: `RxPipeline`

**Files:**
- Create: `tools:src/opener_tools/dectnrp/pipeline.py`
- Test: `tools:tests/dectnrp/test_pipeline.py`, extend `tools:tests/fakes.py` with `FakeRadio`

**Interfaces:**
- Consumes: plan 1 `Radio.time()`, `Radio.schedule_rx(channel, duration, start=…)` → `Op`, `Radio.caps.timing`, the `MsgReceived` / `MsgError` / `OpFinished` events, `SpinelError`, `DeviceReset`.
- Produces:
  - `@dataclass(frozen=True) class Gap: start: int; length: int; reason: str` (`"resync"`, `"rejected"`, `"reset"`).
  - `@dataclass class PipelineStats: windows: int = 0; early_finishes: int = 0; gaps: int = 0; gap_ticks: int = 0`.
  - `class RxPipeline`:
    - `__init__(self, radio: Radio, channel: int, window_ms: float, depth: int = 4, lead_ms: float = 20, reception: Reception = Reception.SINGLE_SHOT)`. Raises `ValueError` if `depth > radio.caps.op_slots`. Every window is scheduled with `reception`.
    - `stats: PipelineStats`;
    - `def __aiter__(self) -> AsyncIterator[MsgReceived | MsgError | Gap]`;
    - `async def stop(self) -> None`: no new windows; awaits the in-flight ops (they finish within one window); idempotent.
- Algorithm (the part the signature does not fix):
  - Sync: `t0 = await radio.time()`, record `host0 = time.monotonic()`; `now_est() = t0 + (monotonic() − host0) · ticks_per_ms · 1000`.
  - First start = `t0 + lead`. Schedule `depth` windows back to back: `start(k+1) = start(k) + window + transition`, where `transition = caps.sched_op_transition or ms_to_ticks(1)`.
  - On each `OpFinished`, schedule one more window at `next_start`. If `next_start < now_est() + lead / 2`, resync first: `new = await radio.time() + lead`, yield `Gap(next_start, new − next_start, "resync")`, set `next_start = new`, and re-anchor `now_est`.
  - A `SpinelError` on insert → the same resync with reason `"rejected"`, then retry once. A second failure re-raises.
  - In single-shot mode, an `OpFinished` whose window received a frame (the op's events contain a `MsgReceived`) increments `early_finishes`. That is expected, not a gap.
  - Events are yielded in arrival order. `DeviceReset` propagates to the caller.

- [ ] **Step 1: Write the failing tests.** `FakeRadio` has a virtual modem clock the test advances. `schedule_rx` records `(start, duration)` and returns an Op the test finishes. `time()` returns the clock.
  - `test_back_to_back`: depth 3, window 20 ms, `sched_op_transition` 25920. The starts are `t0+lead+k·(window+25920)` for k = 0..2. Finishing op 1 schedules a 4th at k = 3.
  - `test_unknown_transition`: `sched_op_transition` 0 → spacing is `window + 69120` (1 ms).
  - `test_yields_events_in_order`: `MsgReceived` and `MsgError` injected on different ops come out in injection order.
  - `test_resync_on_late`: advance the clock 1 s past the queued windows before finishing an op. The next item is a `Gap(reason="resync")` whose `start` equals the old `next_start`, and the new window starts at `clock + lead`.
  - `test_resync_on_reject`: `schedule_rx` raises `SpinelError` once → `Gap(reason="rejected")`, then scheduling continues.
  - `test_double_reject_raises`: two consecutive rejects → `SpinelError` propagates.
  - `test_reception_passed`: `reception=Reception.SEMICONTINUOUS` → every `schedule_rx` call gets it.
  - `test_early_finish_counted`: an op with a `MsgReceived` then `OpFinished` → `stats.early_finishes == 1`, no `Gap`.
  - `test_stop_drains`: `stop()` returns once all in-flight ops are finished, and nothing new is scheduled after `stop()`.
  - `test_depth_exceeds_slots`: `depth=9` with `op_slots=8` → `ValueError`.
- [ ] **Step 2: Run, expect FAIL**: `../.venv/bin/pytest tests/dectnrp/test_pipeline.py -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add RxPipeline for gapless scheduled reception`.

### Task 2: DECT-NR-TAP record builder

**Files:**
- Create: `tools:src/opener_tools/capture/{__init__.py,tap.py}`
- Test: `tools:tests/capture/test_tap.py`

**Interfaces:**
- Produces:
  - `def build_tap_record(ev: MsgReceived | MsgError) -> bytes`;
  - `def parse_tap_record(rec: bytes) -> dict[int, int | bytes]` (TLV type → value, plus key `"body"`; used by tests and the hub later);
  - constants `TLV_*`, `DATA_TYPE_*`, `HDR_STATUS_*`.
- Mapping:
  - `MsgReceived` with a non-empty pdc → data type `1 + phy_type`, status 0.
  - `MsgReceived` with an empty pdc → data type `3 + phy_type`, status 2 (Deveritec behaviour).
  - `MsgError` with pcc → data type `3 + phy_type`, status 1, body = pcc.
  - `MsgError` without pcc → data type 5, status 1, empty body.
  - TLVs in this order:
    - data type, header status, transaction ID (`token & 0xFFFF`), channel;
    - then, only when valid: for `MsgReceived`, rssi2 → PDC RSSI2 (5) and snr → PDC SNR (6), as Deveritec does; for `MsgError`, rssi2 → PCC RSSI2 (3) and snr → PCC SNR (4), since there is no PDC. Values are the raw driver values, sign-extended for snr, as 2-byte LE;
    - then modem time LO (11) and HI (10), 4-byte values;
    - then padding to ≥ 8 TLVs.

- [ ] **Step 1: Write the failing tests:**
  - `test_record_layout`: a `MsgReceived` (phy 0, pcc 5 B, pdc `b"Hello World!"`, token 7, channel 1677, start `0x0000000102030405`, rssi2 0x50, snr −3).
    - Bytes 0..7 = `01 00 <tlvs_len LE> 11 00 00 00`, where 17 = body length.
    - The parsed TLVs = {1: 1, 2: 0, 7: 7, 8: 1677, 5: 0x50, 6: 0xFFFD, 11: 0x02030405, 10: 0x1}.
    - TLV 11 appears before TLV 10.
    - The body is the last 17 bytes.
    - `len(record) == 8 + 8·n_tlvs + 17`.
  - `test_min_tlv_count`: no metrics → still exactly 8 TLVs (padding type 0).
  - `test_pdc_crc_error`: `MsgError` with a 10-byte pcc, phy 1 → data type 4, status 1, body = pcc.
  - `test_msg_error_no_pcc`: → data type 5, body empty, length 72.
  - `test_matches_deveritec_reference`: `tests/capture/data/deveritec_record.bin` is a record produced by the Deveritec C `sniff_build_tap_record` for the same frame (generate it once by compiling `sniffer_codec.c` from `git show deveritec/dectnrp-sniffer:…` with a 10-line C driver; commit the .bin with a README line saying how). Our record must match it on every TLV Deveritec emits (types 1, 2, 7, 8, 5, 6) and on the body.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add DECT-NR-TAP record builder`.

### Task 3: pcap writer

**Files:**
- Create: `tools:src/opener_tools/capture/pcap.py`
- Test: `tools:tests/capture/test_pcap.py`

**Interfaces:**
- Produces `class PcapWriter`:
  - `__init__(self, fp: BinaryIO, linktype: int = 304, flush_each: bool = False)` (writes the global header immediately);
  - `def write(self, record: bytes, ts_ns: int | None = None) -> None` (`None` → `time.time_ns()`);
  - `def close(self) -> None`;
  - `records: int`.
  
  `BrokenPipeError` from `write` propagates; callers treat it as "reader gone".

- [ ] **Step 1: Write the failing tests:**
  - `test_global_header` (the exact 24 bytes from Global Constraints).
  - `test_record_header` (`ts_ns=1_700_000_000_123_456_789` → sec 1700000000, usec 123456, incl = orig = len).
  - `test_roundtrip_reader`: a 15-line pure-Python reader in the test parses 3 records back.
  - `test_tshark_reads`: skipped unless `shutil.which("tshark")`; `tshark -r file -T fields -e frame.len` lists 3 lengths.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add pcap writer`.

### Task 4: `nrp-sniffer` CLI

**Files:**
- Create: `tools:src/opener_tools/cli/{__init__.py,sniffer.py}`
- Modify: `tools:pyproject.toml` (`[project.scripts] nrp-sniffer = "opener_tools.cli.sniffer:main"`)
- Modify: `tools:src/opener_tools/transport/__init__.py` (add `register_scheme(scheme: str, factory: Callable[[urllib.parse.SplitResult], Transport]) -> None`, consulted by `open_transport` before the built-in schemes)
- Test: `tools:tests/cli/test_sniffer.py`

**Interfaces:**
- Consumes: `open_transport`, `SpinelClient`, `Radio`, `RxPipeline`, `build_tap_record`, `PcapWriter`.
- Produces:
  - `def main(argv: list[str] | None = None) -> int`;
  - `async def run_sniffer(transport_url: str, out: BinaryIO, channel: int, network_id: int, reception: Reception, window_ms: float, depth: int, lead_ms: float, stop: asyncio.Event, log: logging.Logger) -> SnifferStats`;
  - `@dataclass SnifferStats: records, crc_errors, dropped_events, gaps, gap_ms, resets`.
- CLI:
  - `--transport URL` (required unless extcap), `--channel` (1677), `--network-id` (int, base auto, 0x32545279), `--rx-mode single|semi|continuous` (`DEFAULT_RX_MODE`), `--window` ms (`DEFAULT_WINDOW_MS`), `--depth` (4), `--lead` ms (20), `-w FILE|-` (default `-` = stdout, `flush_each=True` for `-` and FIFOs), `-v`.
  - Logs go to stderr. Stats are printed to stderr at exit.
  - SIGINT/SIGTERM set `stop`.
- Flow:
  - Init: `client.start` → `radio.init` → `reset` → `set_network_id` → `enable(True)`.
  - Main loop: `RxPipeline` → events become records; `Gap` gets a warning log line and is added to the stats; the `dropped` field of any event goes into `dropped_events`.
  - `DeviceReset`: `resets += 1`, log a warning, redo init, and start a new pipeline. The pcap continues.
  - On stop: `pipeline.stop()` → `radio.enable(False)` → `writer.close()` → `client.close()`.
  - `BrokenPipeError` on write = a normal stop.

- [ ] **Step 1: Write the failing tests** (a `ScriptedDevice` from plan 1 `tests/fakes.py`, extended with a virtual clock and auto `OP_FINISHED`, exposed through a `FakeTransport` registered under the URL scheme `fake://`; `open_transport` gets a test-only registry hook `register_scheme(scheme, factory)`):
  - `test_writes_records`: the fake emits 3 `MSG_RECEIVED` and 1 `MSG_ERROR` → the pcap has 4 records, `stats.crc_errors == 1`.
  - `test_init_sequence`: the requests sent are, in order, RESET, SET NETWORK_ID 0x32545279, SET DRV_ENABLED true, GET DRV_TIME, then the inserts.
  - `test_device_reset_reinit`: after 2 records the fake injects `RESET_POWER_ON` and forgets its state. The sniffer re-sends NETWORK_ID and ENABLED, records continue in the same file, and `stats.resets == 1`.
  - `test_clean_stop`: set `stop` → the last requests are no further inserts and `SET DRV_ENABLED false`; the return happens after all ops finished; the file is closed.
  - `test_broken_pipe_is_clean`: the writer raises `BrokenPipeError` → `run_sniffer` returns normally and the radio is disabled.
  - `test_cli_args`: `main(["--transport","fake://","-w",tmp,"--network-id","0x1234"])` parses the hex value, and the exit code is 0 after stop.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add nrp-sniffer`.

### Task 5: Wireshark extcap mode

**Files:**
- Create: `tools:src/opener_tools/capture/extcap.py`, `tools:scripts/install-extcap.sh`
- Modify: `tools:src/opener_tools/cli/sniffer.py` (dispatch to extcap when any `--extcap-*` or `--capture` arg is present)
- Test: `tools:tests/capture/test_extcap.py`

**Interfaces:**
- Extcap protocol as in the Deveritec `dectnrp_sniffer_extcap.py`:
  - `--extcap-interfaces` lists `extcap {version=<pkg version>}` and `interface {value=nrp-sniffer}{display=DECT NR+ Sniffer (Opener Spinel)}`.
  - `--extcap-dlts` prints `dlt {number=304}{name=DECT_NR_TAP}{display=DECT NR+ TAP}`.
  - `--extcap-config` offers args `--transport` (string, default `uart:///dev/ttyACM0`), `--channel` (unsigned, 1677), `--network-id` (string, `0x32545279`), `--rx-mode` (selector single/semi/continuous, default `DEFAULT_RX_MODE`), `--window` (unsigned, `DEFAULT_WINDOW_MS`), `--depth` (unsigned, 4).
  - `--capture --fifo PATH` runs `run_sniffer` writing to the FIFO.
  - `--extcap-version`, `--extcap-interface` and `--extcap-capture-filter` are accepted (the filter is ignored).
- `install-extcap.sh`: symlinks a wrapper script running `<venv>/bin/nrp-sniffer "$@"` into `~/.local/lib/wireshark/extcap/` (or `$1`), then `chmod +x`.

- [ ] **Step 1: Write the failing tests:**
  - `test_interfaces_output`, `test_dlts_output`, `test_config_lists_args` (string checks against the exact lines above).
  - `test_capture_to_fifo`: `os.mkfifo` plus a reader thread; `--capture --fifo` with `fake://` → the reader gets a valid pcap header plus ≥ 1 record.
  - `test_extcap_fifo_closed`: the reader closes the FIFO early → the process returns 0 and the fake radio ends disabled.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add Wireshark extcap mode to nrp-sniffer`.

### Task 6: Sniffer against the simulated device

**Files:**
- Create: `tools:tests/integration/test_sniffer_simdev.py`

- [ ] **Step 1: Write the tests** (marker `simdev`, plan 1 fixture `simdev_url`):
  - `test_sniffer_captures`: `run_sniffer` for 3 s, with the default mode and window and depth 4, writing to a temp file. The simdev emits a type 2 frame every 100 ms into active windows, so expect ≥ 20 records, about 10 % of them with data type 4, and every data type 2 body ending with `b"Hello World!"`. `stats.gaps == 0`.
  - `test_tshark_decodes`: skipped unless tshark ≥ 4.7.4 (parse `tshark --version`). `tshark -r file -Y dect_nr_tap -T fields -e dect_nr_tap.tlv_element.modem_time` gives one non-zero value per record, which checks the LO/HI order end to end.
- [ ] **Step 2: Run**: `OPENER_SIMDEV_EXE=… ../.venv/bin/pytest -m simdev -v`. Expect PASS. Debug any failure with superpowers:systematic-debugging.
- [ ] **Step 3: Commit** `test: sniffer integration against simulated device`.

### Task 7: Hardware validation on EKs (§8.3.2) and docs

**Files:**
- Create: `tools:docs/nrp-sniffer.md` (usage: CLI, extcap install, Wireshark ≥ 4.7.4 note, defaults and the single-shot window note)
- Modify: `tools:README.md` (tool list, install, test commands)

- [ ] **Step 1: Set up the EKs.** Device1 runs `dectnrp-spinel` (plan 1 Task 15), Device2 runs `dectnrp-driver` with default settings.
- [ ] **Step 2: Capture.** Run `nrp-sniffer --transport uart:///dev/ttyACM<n> -w /tmp/dk.pcap` for 60 s, then Ctrl-C. Expect about 20 records (Device2 sends every 3 s; ping-pong with no peer means TX only), exit code 0, and stats with 0 dropped events.
- [ ] **Step 3: Decode in Wireshark.** Open the capture in Wireshark 4.7.4 (local build): records decode as DECT NR+ with the type 2 PCC fields and the `Hello World!` payload in the MAC PDU, and the modem time is present. Screenshot or note the result for the user.
- [ ] **Step 4: Run as extcap.** Install with `scripts/install-extcap.sh`, then start a live capture from the Wireshark GUI; stopping the capture leaves Device1 disabled (check `nrp-sniffer` has exited and the RTT log shows `stop`).
- [ ] **Step 5: Commit** `docs: add nrp-sniffer usage` in linux-tools. Report the results to the user and ask whether to write plan 3 (SPI on the NR+Gate).
