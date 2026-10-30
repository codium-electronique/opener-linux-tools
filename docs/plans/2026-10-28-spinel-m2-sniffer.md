# Spinel M2 — host-driven NR+ sniffer over UART: Implementation Plan

> **Status: executed 2026-10-29 to 2026-10-30** (plan written 2026-10-28), including a user-decided follow-up and the final review fix pass. Commits, all on `spinel-ctrl`:
> - linux-tools: `f3f35f9..4cb87d4` — tasks `27dc40a..a03d86a` (with the fixes `59b5c22` and `e3cbb14` found in Tasks 6 and 7), follow-ups `c27c92f`, `01bdfdc`, fix pass `4cb87d4`;
> - opener-samples: `c527c00..efc3202` (UART flow control);
> - opener: none (built at `9ab85e8`).
>
> Deviations were recorded during execution in `opener-samples/.superpowers/sdd/2026-10-28-spinel-m2-sniffer/progress.md` (one `Ruling:` line each). The tasks below have been updated to what was built; "As built" notes summarise the rulings. Work after the tasks is in "After the plan" at the end.

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
  - The dissector rejects records shorter than 72 bytes. As built, padding TLVs (type 0, len 0) are added only up to that length, not up to 8 TLVs: Wireshark shows each pad as an "Unknown TLV", so records with a PDC carry none and short ones (PCC-only CRC errors) keep a few.
- TLV types: 1 data type, 2 header status, 3 PCC RSSI2, 4 PCC SNR, 5 PDC RSSI2, 6 PDC SNR, 7 transaction ID, 8 channel, 10 modem time hi, 11 modem time lo.
  - Data type values: 1/2 = 40/80-bit PCC + PDC; 3/4 = 40/80-bit PCC, PDC absent (CRC error); 5 = neither.
  - Header status values: 0 valid with PDC, 1 invalid / PDC absent, 2 valid but no PDC.
  - RSSI2 and SNR are in TAP units (as built, `e3cbb14`): RSSI2 int16 in 0.5 dBm (Q14.1), SNR int16 in 0.25 dB (Q13.2), converted from the driver's ETSI codes (TS 103 636-2 §8.3 RSSI-2 byte, §8.4 SNR in 0.5 dB).
- **Correction:** types 10 and 11 are not part of DECT-NR-TAP. `packet-dect-nr-tap.c` and the DECT-NR-TAP spec define types 1–9 only, and the "LO before HI" rule had no source; Wireshark shows 10/11 as unknown TLVs. As built (user decision), the modem time TLVs are optional and off by default (`--modem-time-tlv`); when on, LO (11) is still written before HI (10).
- pcap: classic little-endian µs header `struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 304)`, as in the Deveritec writer. Timestamps are host time.
- Defaults:
  - channel 1677 and network ID 0x32545279 (`dectnrp-driver` sample values, not Deveritec's);
  - RX reception mode and window (decided with the user after plan 1 Task 2): **single-shot, 20 ms windows** by default. On nRF91, continuous mode cannot receive type 2 format 0 (all current `dectnrp-driver` traffic), and semi-continuous ends on any unicast frame, so only single-shot sees every format. A received frame ends a single-shot window early, and short windows bound the uncovered remainder. The window size must stay **configurable** (`--window`, fractional ms accepted, CLI and extcap) so shorter or longer windows can be tested easily; `--rx-mode continuous` remains available for broadcast/format 1 captures. `DEFAULT_RX_MODE = Reception.SINGLE_SHOT` and `DEFAULT_WINDOW_MS = 20.0` are defined once in `cli/sniffer.py`. **Changed after the plan (user decision, `c27c92f`): `DEFAULT_WINDOW_MS = 200.0`**, and the pipeline fills the rest of a single-shot window ended early by a frame (see "After the plan");
  - depth 4 (as built, the depth must stay below `op_slots`: one slot is kept for a fill window);
  - lead 20 ms. The probe found that 1 ms is accepted; the 20 ms covers host and link latency. Lead is clamped to at least `caps.sched_op_startup + caps.rx_startup`.
  - Window spacing: consecutive windows are separated by `caps.sched_op_transition` (D11; 25 920 ticks = 375 µs on nRF9131). Exactly adjacent windows are rejected by the modem (plan 1 Task 1). If the device reports 0 (unknown), use 1 ms.
- UART: the `dectnrp-spinel` overlays enable `hw-flow-control` on uart0 (added after the plan, `efc3202`); without it the device loses about one host frame in 4000 at 1 Mbaud.
- Deveritec attribution: `capture/tap.py` and `capture/extcap.py` carry `# Derived from Deveritec dectnrp-sniffer (Apache-2.0), opener-samples deveritec/dectnrp-sniffer:samples/dectnrp-sniffer/`. As built, only `tap.py` carries it; `extcap.py` has no Deveritec line (it implements the extcap protocol from the Wireshark docs, see its docstring).

## Review Focus

1. **Host stalls** (GC pause, slow SSH pipe, Wireshark FIFO not read). The pipeline falls behind, so new windows would start in the past. It must resync and report one gap, not error out or spin → `test_resync_on_late`, Task 1.
2. **Device rejects a scheduled op** (driver errno because start is in the past, or `NOMEM`). It must resync, not crash → `test_resync_on_reject` and `test_reject_when_late`, Task 1; a missing reply (`SpinelTimeout`) counts as a reject since the fix pass → `test_insert_timeout_is_reject`.
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
  - `@dataclass(frozen=True) class Gap: start: int; length: int; reason: str` (`"resync"`, `"rejected"`, `"reset"`). As built, the pipeline yields `"resync"`, `"rejected"` and, since the fix pass, `"failed, status N"`; it never yields `"reset"` (a device reset ends the iteration).
  - `@dataclass class PipelineStats: windows: int = 0; early_finishes: int = 0; gaps: int = 0; gap_ticks: int = 0`. As built, also `dropped_events` (Task 4), `fills` and `fill_rejects` (follow-up); `windows` counts fills too.
  - `class RxPipeline`:
    - `__init__(self, radio: Radio, channel: int, window_ms: float, depth: int = 4, lead_ms: float = 20, reception: Reception = Reception.SINGLE_SHOT)`. Raises `ValueError` if `depth > radio.caps.op_slots` (since the follow-up: `depth >= op_slots`, one slot is kept for fills). Every window is scheduled with `reception`.
    - `stats: PipelineStats`;
    - `def __aiter__(self) -> AsyncIterator[MsgReceived | MsgError | Gap]`;
    - `async def stop(self) -> None`: no new windows; awaits the in-flight ops (they finish within one window); idempotent.
- Algorithm (the part the signature does not fix):
  - Sync: `t0 = await radio.time()`, record `host0 = time.monotonic()`; `now_est() = t0 + (monotonic() − host0) · ticks_per_ms · 1000`.
  - First start = `t0 + lead`. Schedule `depth` windows back to back: `start(k+1) = start(k) + window + transition`, where `transition = caps.sched_op_transition or ms_to_ticks(1)`.
  - On each `OpFinished`, schedule one more window at `next_start`. If `next_start < now_est() + lead / 2`, resync first: `new = await radio.time() + lead`, yield `Gap(next_start, new − next_start, "resync")`, set `next_start = new`, and re-anchor `now_est`.
  - A `SpinelError` on insert → the same resync with reason `"rejected"`, then retry once. A second failure re-raises. As built, the rejected window is skipped: the retry starts at `max(now + lead, next_start + spacing)`, never earlier than the next slot, because an earlier start would overlap the queued windows and be rejected again. Since the fix pass, a `SpinelTimeout` on insert is handled the same way.
  - In single-shot mode, an `OpFinished` whose window received a frame (the op's events contain a `MsgReceived`) increments `early_finishes`. That is expected, not a gap. Since the follow-up, a `MsgError` also counts, and the rest of the window gets a fill.
  - Events are yielded in arrival order. `DeviceReset` propagates to the caller.
  - As built: `events()` streams are not told of resets, so the pipeline waits on its event stream and on its in-flight Ops together (each window wrapped in a task, never cancelled) and re-raises an Op's `DeviceReset`. `pipeline.py` imports `monotonic` at module level so tests patch it to follow `FakeRadio`'s clock. `Gap`, `PipelineStats`, `RxPipeline` are exported from `opener_tools.dectnrp`.
  - Since the fix pass (`4cb87d4`): the modem time is read again when the anchor is older than `ANCHOR_MAX_AGE_S = 1.0` s (host/modem clock drift); a grid window whose `OP_FINISHED` has a non-zero status yields `Gap(reason="failed, status N")`.

- [x] **Step 1: Write the failing tests.** `FakeRadio` has a virtual modem clock the test advances. `schedule_rx` records `(start, duration)` and returns an Op the test finishes. `time()` returns the clock.
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
  - Extra tests as built: `test_reject_when_late`, `test_device_reset_propagates`, and `test_dropped_counted` (Task 4). The follow-up and fix pass added the fill and robustness tests listed in "After the plan".
- [x] **Step 2: Run, expect FAIL**: `../.venv/bin/pytest tests/dectnrp/test_pipeline.py -v`
- [x] **Step 3: Implement.**
- [x] **Step 4: Run, expect PASS.**
- [x] **Step 5: Commit** `feat: add RxPipeline for gapless scheduled reception`.

### Task 2: DECT-NR-TAP record builder

**Files:**
- Create: `tools:src/opener_tools/capture/{__init__.py,tap.py}`
- Test: `tools:tests/capture/test_tap.py`; as built also `tools:tests/capture/data/{deveritec_record.bin,make_deveritec_record.c,README.md}`

**Interfaces:**
- Produces:
  - `def build_tap_record(ev: MsgReceived | MsgError, modem_time: bool = False) -> bytes` (as built; `modem_time` added in Task 6, see Global Constraints);
  - `def parse_tap_record(rec: bytes) -> dict[int | str, int | bytes]` (TLV type → value, plus key `"body"`; used by tests and the hub later). As built, it drops padding entries and raises `ValueError` on inconsistent lengths;
  - constants `TLV_*`, `DATA_TYPE_*`, `HDR_STATUS_*`. As built: `DATA_TYPE_PCC_PDC = 1` and `DATA_TYPE_PCC = 3` are bases (+ `phy_type`), `DATA_TYPE_NONE = 5`; `HDR_STATUS_VALID`/`PDC_ERROR`/`PDC_ABSENT` = 0/1/2.
- Mapping:
  - `MsgReceived` with a non-empty pdc → data type `1 + phy_type`, status 0.
  - `MsgReceived` with an empty pdc → data type `3 + phy_type`, status 2 (Deveritec behaviour).
  - `MsgError` with pcc → data type `3 + phy_type`, status 1, body = pcc.
  - `MsgError` without pcc → data type 5, status 1, empty body.
  - TLVs in this order:
    - data type, header status, transaction ID (`token & 0xFFFF`), channel;
    - then, only when valid: for `MsgReceived`, rssi2 → PDC RSSI2 (5) and snr → PDC SNR (6), as Deveritec does; for `MsgError`, rssi2 → PCC RSSI2 (3) and snr → PCC SNR (4), since there is no PDC. Values are the raw driver values, sign-extended for snr, as 2-byte LE. **Wrong, fixed in Task 7 (`e3cbb14`):** TAP wants RSSI2 in 0.5 dBm and SNR in 0.25 dB; Wireshark showed +110.5 dBm. `tap.py` now converts: RSSI2 = (code − 0x100) × 2, SNR = snr × 2, as int16 LE;
    - then, only with `modem_time=True` (Task 6), modem time LO (11) and HI (10), 4-byte values;
    - then padding up to the 72-byte minimum record length (Task 6; originally up to 8 TLVs).

- [x] **Step 1: Write the failing tests:**
  - `test_record_layout`: a `MsgReceived` (phy 0, pcc 5 B, pdc `b"Hello World!"`, token 7, channel 1677, start `0x0000000102030405`, rssi2 0x50, snr −3).
    - Bytes 0..7 = `01 00 <tlvs_len LE> 11 00 00 00`, where 17 = body length.
    - The parsed TLVs = {1: 1, 2: 0, 7: 7, 8: 1677, 5: 0x50, 6: 0xFFFD, 11: 0x02030405, 10: 0x1}.
    - TLV 11 appears before TLV 10.
    - The body is the last 17 bytes.
    - `len(record) == 8 + 8·n_tlvs + 17`.
    
    As built (after Tasks 6 and 7): built with `modem_time=True`, rssi2 `0xDD` (−35 dBm) and snr −3 (−1.5 dB), giving TLV 5 = `0xFFBA` and TLV 6 = `0xFFFA`.
  - `test_min_tlv_count`: no metrics → still exactly 8 TLVs (padding type 0). As built, replaced by `test_min_record_length` (padded only up to 72 bytes), `test_no_padding_with_modem_time` and `test_no_modem_time_by_default`.
  - Extra tests as built: `test_empty_pdc`, `test_msg_error_metrics_are_pcc`, `test_transaction_id_wraps`, and `test_metrics_in_tap_units` (Task 7).
  - `test_pdc_crc_error`: `MsgError` with a 10-byte pcc, phy 1 → data type 4, status 1, body = pcc.
  - `test_msg_error_no_pcc`: → data type 5, body empty, length 72.
  - `test_matches_deveritec_reference`: `tests/capture/data/deveritec_record.bin` is a record produced by the Deveritec C `sniff_build_tap_record` for the same frame (generate it once by compiling `sniffer_codec.c` from `git show deveritec/dectnrp-sniffer:…` with a 10-line C driver; commit the .bin with a README line saying how). Our record must match it on every TLV Deveritec emits (types 1, 2, 7, 8, 5, 6) and on the body. As built, the C driver is committed too (`tests/capture/data/make_deveritec_record.c` + `README.md`); the Deveritec sources are fetched with `git show` at generation time and not committed. The record was regenerated with TAP-unit metrics in Task 7.
- [x] **Step 2: Run, expect FAIL.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run, expect PASS.**
- [x] **Step 5: Commit** `feat: add DECT-NR-TAP record builder`.

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

- [x] **Step 1: Write the failing tests:**
  - `test_global_header` (the exact 24 bytes from Global Constraints).
  - `test_record_header` (`ts_ns=1_700_000_000_123_456_789` → sec 1700000000, usec 123456, incl = orig = len).
  - `test_roundtrip_reader`: a 15-line pure-Python reader in the test parses 3 records back.
  - `test_tshark_reads`: skipped unless `shutil.which("tshark")`; `tshark -r file -T fields -e frame.len` lists 3 lengths. (No tshark on the machine during Task 3; it passed in Task 6 with the user's tshark 4.7.4.)
  - Extra tests as built: `test_default_timestamp_is_now`, `test_flush_each`. `PcapWriter` owns `fp` (`close()` closes it) and writes records untruncated.
- [x] **Step 2: Run, expect FAIL.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run, expect PASS.**
- [x] **Step 5: Commit** `feat: add pcap writer`.

### Task 4: `nrp-sniffer` CLI

**Files:**
- Create: `tools:src/opener_tools/cli/{__init__.py,sniffer.py}`
- Modify: `tools:pyproject.toml` (`[project.scripts] nrp-sniffer = "opener_tools.cli.sniffer:main"`)
- Modify: `tools:src/opener_tools/transport/__init__.py` (add `register_scheme(scheme: str, factory: Callable[[urllib.parse.SplitResult], Transport]) -> None`, consulted by `open_transport` before the built-in schemes)
- Test: `tools:tests/cli/test_sniffer.py`; as built also `tools:tests/fakes.py` (`SniffDevice`, `DeviceTransport`), `tools:tests/pcapfile.py` (`read_pcap`, moved from `test_pcap.py`), `tools:tests/transport/test_url.py`

**Interfaces:**
- Consumes: `open_transport`, `SpinelClient`, `Radio`, `RxPipeline`, `build_tap_record`, `PcapWriter`.
- Produces:
  - `def main(argv: list[str] | None = None) -> int`;
  - `async def run_sniffer(transport_url: str, out: BinaryIO, channel: int, network_id: int, reception: Reception, window_ms: float, depth: int, lead_ms: float, stop: asyncio.Event, log: logging.Logger, flush_each: bool = False, modem_time: bool = False) -> SnifferStats` (as built: `flush_each` added because the planned signature had no way to pass it, `modem_time` in Task 6);
  - `@dataclass SnifferStats: records, crc_errors, dropped_events, gaps, gap_ms, resets`. `crc_errors` counts every `MsgError`, with or without a PCC.
- CLI:
  - `--transport URL` (required unless extcap), `--channel` (1677), `--network-id` (int, base auto, 0x32545279), `--rx-mode single|semi|continuous` (`DEFAULT_RX_MODE`), `--window` ms (float > 0, `DEFAULT_WINDOW_MS`; argparse error otherwise), `--depth` (4), `--lead` ms (20), `-w FILE|-` (default `-` = stdout, `flush_each=True` for `-` and FIFOs), `-v`. As built, also `--modem-time-tlv` (Task 6).
  - Logs go to stderr (default level INFO, `-v` DEBUG). Stats are printed to stderr at exit. Errors are logged and the exit code is 1.
  - SIGINT/SIGTERM set `stop`.
- Flow:
  - Init: `client.start` → `radio.init` → `reset` → `set_network_id` → `enable(True)`. As built, the order is `reset` → `init` → `set_network_id` → `enable(True)`: the reset first drops the ops a previous host left queued, before the caps are read.
  - Main loop: `RxPipeline` → events become records; `Gap` gets a warning log line and is added to the stats; the `dropped` field of any event goes into `dropped_events`. As built, `RxPipeline` sums the drops itself (`PipelineStats.dropped_events`, every event of its stream, since the device reports drops on the next event sent whatever its op) and the sniffer adds them up per pipeline.
  - `DeviceReset`: `resets += 1`, log a warning, redo init, and start a new pipeline. The pcap continues.
  - On stop: `pipeline.stop()` → `radio.enable(False)` → `writer.close()` → `client.close()`. As built, the writer is closed last, after `radio.close()` and `client.close()`, and `run_sniffer` also closes the transport.
  - `BrokenPipeError` on write = a normal stop.
- As built, `register_scheme(scheme, factory)` is a public function of `opener_tools.transport`, and `read_pcap` moved to `tests/pcapfile.py`, shared by the capture and CLI tests.

- [x] **Step 1: Write the failing tests** (a `ScriptedDevice` from plan 1 `tests/fakes.py`, extended with a virtual clock and auto `OP_FINISHED`, exposed through a `FakeTransport` registered under the URL scheme `fake://`; `open_transport` gets a test-only registry hook `register_scheme(scheme, factory)`):
  - `test_writes_records`: the fake emits 3 `MSG_RECEIVED` and 1 `MSG_ERROR` → the pcap has 4 records, `stats.crc_errors == 1`.
  - As built, the fake device is `SniffDevice` (a `ScriptedDevice` subclass in `tests/fakes.py`) behind a `DeviceTransport`. It runs its modem clock on the event-loop clock and finishes ops by timer, since the sniffer runs in real time; tests use 5 ms windows and stop through device hooks (`after_insert`, `when_written`). The `fake://` fixture (`device`) moved to `tests/conftest.py` in Task 5.
  - `test_init_sequence`: the requests sent are, in order, RESET, SET NETWORK_ID 0x32545279, SET DRV_ENABLED true, GET DRV_TIME, then the inserts. As built, checked as an ordered subsequence (the GETs of `radio.init()` come between).
  - `test_device_reset_reinit`: after 2 records the fake injects `RESET_POWER_ON` and forgets its state. The sniffer re-sends NETWORK_ID and ENABLED, records continue in the same file, and `stats.resets == 1`.
  - `test_clean_stop`: set `stop` → the last requests are no further inserts and `SET DRV_ENABLED false`; the return happens after all ops finished; the file is closed.
  - `test_broken_pipe_is_clean`: the writer raises `BrokenPipeError` → `run_sniffer` returns normally and the radio is disabled.
  - `test_cli_args`: `main(["--transport","fake://","-w",tmp,"--network-id","0x1234"])` parses the hex value, and the exit code is 0 after stop.
  - `test_window_option`: `--window 5` and `--window 0.5` give inserted RX durations of 5 ms and 0.5 ms (in ticks); `--window 0` and `--window -1` exit with an argparse error (code 2). Without `--window`, the duration is 20 ms and the flags carry single-shot. As built, the error cases are split into `test_window_must_be_positive`, and the default is 200 ms since the follow-up.
  - Extra tests as built: `test_registered_scheme` (in `tests/transport/test_url.py`), `test_rx_mode_option`, `test_modem_time_option` (Task 6), `test_transport_lost` (fix pass).
- [x] **Step 2: Run, expect FAIL.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run, expect PASS.** Also smoke-run against the simdev: 3 s → 29 records (26 data type 2, 3 type 4), exit 0 on SIGINT and when `head` closes stdout.
- [x] **Step 5: Commit** `feat: add nrp-sniffer`.

### Task 5: Wireshark extcap mode

**Files:**
- Create: `tools:src/opener_tools/capture/extcap.py`, `tools:scripts/install-extcap.sh`
- Modify: `tools:src/opener_tools/cli/sniffer.py` (dispatch to extcap when any `--extcap-*` or `--capture` arg is present)
- Test: `tools:tests/capture/test_extcap.py`; as built, the `fake://` fixture `device` moved from `tests/cli/conftest.py` to `tools:tests/conftest.py`

**Interfaces:**
- Extcap protocol as in the Deveritec `dectnrp_sniffer_extcap.py`:
  - `--extcap-interfaces` lists `extcap {version=<pkg version>}` and `interface {value=nrp-sniffer}{display=DECT NR+ Sniffer (Opener Spinel)}`.
  - `--extcap-dlts` prints `dlt {number=304}{name=DECT_NR_TAP}{display=DECT NR+ TAP}`.
  - `--extcap-config` offers args `--transport` (string, default `uart:///dev/ttyACM0`), `--channel` (unsigned, 1677), `--network-id` (string, `0x32545279`), `--rx-mode` (selector single/semi/continuous, default `DEFAULT_RX_MODE`), `--window` (double, `DEFAULT_WINDOW_MS`), `--depth` (unsigned, 4). As built: the selector shows single-shot/semicontinuous/continuous, the network ID has a validation regex, and arg 6 `--modem-time-tlv` (boolflag) was added in Task 6.
  - `--capture --fifo PATH` runs `run_sniffer` writing to the FIFO.
  - `--extcap-version`, `--extcap-interface` and `--extcap-capture-filter` are accepted (the filter is ignored). As built, `--extcap-version` alone answers like `--extcap-interfaces`, and extcap mode uses `parse_known_args` (Wireshark adds arguments across versions); `--transport` defaults to `uart:///dev/ttyACM0` there and stays required on the command line.
  - As built: `capture/extcap.py` holds only the protocol text and argument helpers (`wants_extcap`, `add_arguments`, `interfaces`, `dlts`, `config`); `cli/sniffer.main` dispatches and captures through its own path, to avoid an import cycle. Since the Task 6 fix (`59b5c22`), extcap mode logs at WARNING (`-v` raises it) and logs the stats instead of printing them, because Wireshark reports any extcap stderr as "Error from extcap pipe".
- `install-extcap.sh`: symlinks a wrapper script running `<venv>/bin/nrp-sniffer "$@"` into `~/.local/lib/wireshark/extcap/` (or `$1`), then `chmod +x`. As built, it writes the wrapper file itself into the extcap dir (no symlink: the wrapper holds the venv path, so there is nothing to link to), takes `nrp-sniffer` from `PATH` or `$NRP_SNIFFER`, and checks that it answers `--extcap-interfaces`.

- [x] **Step 1: Write the failing tests:**
  - `test_interfaces_output`, `test_dlts_output`, `test_config_lists_args` (string checks against the exact lines above).
  - `test_capture_to_fifo`: `os.mkfifo` plus a reader thread; `--capture --fifo` with `fake://` → the reader gets a valid pcap header plus ≥ 1 record.
  - `test_extcap_fifo_closed`: the reader closes the FIFO early → the process returns 0 and the fake radio ends disabled.
  - As built, `test_capture_to_fifo` also asserts an empty stderr (Task 6 fix). Smoke run: wrapper installed in a scratch dir, `--capture --fifo` against the simdev, SIGTERM after 3 s → exit 0, 28 records, link type 304.
- [x] **Step 2: Run, expect FAIL.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run, expect PASS.**
- [x] **Step 5: Commit** `feat: add Wireshark extcap mode to nrp-sniffer`.

### Task 6: Sniffer against the simulated device

**Files:**
- Create: `tools:tests/integration/test_sniffer_simdev.py`

- [x] **Step 1: Write the tests** (marker `simdev`, plan 1 fixture `simdev_url`):
  - `test_sniffer_captures`: `run_sniffer` for 3 s, with the default mode and window and depth 4, writing to a temp file. The simdev emits a type 2 frame every 100 ms into active windows, so expect ≥ 20 records, about 10 % of them with data type 4, and every data type 2 body ending with `b"Hello World!"`. `stats.gaps == 0`. As built: "about 10 %" is asserted as `1 <= errors <= records // 5`, every record is data type 2 or 4, and the stats match the file. Since the follow-up (`c27c92f`) it captures with modem time TLVs, expects ≥ 25 records and checks that consecutive frames are exactly one emit period (100 ms) apart, i.e. no frame is missed; it fails with fills disabled.
  - `test_tshark_decodes`: skipped unless tshark ≥ 4.7.4 (parse `tshark --version`). `tshark -r file -Y dect_nr_tap -T fields -e dect_nr_tap.tlv_element.modem_time` gives one non-zero value per record, which checks the LO/HI order end to end. **Replaced:** no `modem_time` field exists in the dissector (see Global Constraints). As built, it is parametrized on `modem_time` and checks per record that tshark's data type equals ours, the PHY header decodes, and the "Unknown TLV" count equals our pads (+2 with modem time).
- [x] **Step 2: Run**: `OPENER_SIMDEV_EXE=… ../.venv/bin/pytest -m simdev -v`. Expect PASS. Debug any failure with superpowers:systematic-debugging. Result: with the user's tshark 4.7.4, the planned `test_tshark_decodes` failed (no modem time field), which led to the modem time and padding changes of `59b5c22` (Tasks 2 and 5 revisited). Then `pytest -m simdev` → 9 passed (3/3 runs); `tshark -D` lists `nrp-sniffer` and `tshark -i nrp-sniffer` captures from the simdev without a stderr error. The DECT NR+ "Undecoded" MAC warnings come from the simdev's raw `"Hello World!"` payload (no MAC header), as expected.
- [x] **Step 3: Commit** `test: sniffer integration against simulated device` (`2b9c0e8`, after `59b5c22` `fix: make TAP modem time optional, keep extcap stderr quiet`).

### Task 7: Hardware validation on EKs (§8.3.2) and docs

**Files:**
- Create: `tools:docs/nrp-sniffer.md` (usage: CLI, extcap install, Wireshark ≥ 4.7.4 note, defaults and the single-shot window note)
- Modify: `tools:README.md` (tool list, install, test commands)

- [x] **Step 1: Set up the EKs.** Device1 runs `dectnrp-spinel` (plan 1 Task 15), Device2 runs `dectnrp-driver` with default settings.
- [x] **Step 2: Capture.** Run `nrp-sniffer --transport uart:///dev/ttyACM<n> -w /tmp/dk.pcap` for 60 s, then Ctrl-C. Expect about 20 records (Device2 sends every 3 s; ping-pong with no peer means TX only), exit code 0, and stats with 0 dropped events. Result (EKs rebuilt at opener `9ab85e8`): 20 records, 0 dropped, 0 gaps, exit 0.
- [x] **Step 3: Decode in Wireshark.** Open the capture in Wireshark 4.7.4 (local build): records decode as DECT NR+ with the type 2 PCC fields and the `Hello World!` payload in the MAC PDU, and the modem time is present. Screenshot or note the result for the user. Done with tshark 4.7.4: PCC type 2 and MAC PDU `"Hello World!\n"` decode. Modem time checked with `--modem-time-tlv` (69.12 MHz ticks, shown as 2 unknown TLVs). This step found that RSSI2/SNR were written as raw driver codes (Wireshark showed +110.5 dBm); fixed in `e3cbb14` (TAP units, reference record regenerated), after which the EK read −35 dBm and SNR 22.5–24.5 dB.
- [x] **Step 4: Run as extcap.** Install with `scripts/install-extcap.sh`, then start a live capture from the Wireshark GUI; stopping the capture leaves Device1 disabled (check `nrp-sniffer` has exited and the RTT log shows `stop`). Done with `tshark -i nrp-sniffer` instead of the GUI (same extcap protocol); cleanup checked by reading back `DRV_ENABLED = 00` and no `nrp-sniffer` process left, not with the RTT log. A click-through in the Wireshark GUI is left to the user.
- [x] **Step 5: Commit** `docs: add nrp-sniffer usage` in linux-tools (`a03d86a`, also updates `README.md`). Report the results to the user and ask whether to write plan 3 (SPI on the NR+Gate).
- Findings reported at the end of the task (see "After the plan" for what became of them): a ~0.7 ms blind zone per window boundary, and `--window 2` or `3` leading to resyncs then a `SpinelTimeout` on an op insert (5 ms windows were fine).

## After the plan

User-decided follow-ups and fixes found during the tasks:

- `59b5c22` (Task 6): modem time TLVs 10/11 optional, off by default (`build_tap_record(ev, modem_time=False)`, `run_sniffer(modem_time=False)`, CLI `--modem-time-tlv`, extcap arg 6); padding only up to the 72-byte minimum; extcap mode keeps stderr quiet.
- `e3cbb14` (Task 7): RSSI2 and SNR written in DECT-NR-TAP units (Q14.1 dBm, Q13.2 dB) instead of raw driver codes; Deveritec reference record regenerated.
- `c27c92f` (user decision, 2026-10-30): long windows with fill windows, default window 200 ms. In single-shot mode a frame (`MsgReceived` or `MsgError`) ends a window early; the pipeline then schedules a fill from `now + lead` to the next queued window (or the next grid start) minus the transition, skipped below `FILL_MIN_MS = 1.0`. Fills do not extend the grid; a rejected fill counts in `stats.fill_rejects` without a resync; fills are allowed while stopping, bounded by queued windows only. `depth` must be below `op_slots` (one slot kept for a fill). A probe on the nRF9131 showed the modem accepts an RX op starting before already-queued ops. HW: 10 × 60 s at depth 4 and 200 ms → about 20 fills per run, 0 rejects, 0 missed frames.
- `efc3202` (opener-samples) and `01bdfdc` (docs): `hw-flow-control` on uart0 in the `dectnrp-spinel` EK and DK overlays. Root cause of the occasional insert `SpinelTimeout`, and of the Task 7 `--window 2`/`3` timeouts (the investigation reproduced them with 3 ms windows): the device UART lost received bytes at 1 Mbaud without flow control, the request failed its FCS, the device dropped it (`frames_rejected` +1) and the host waited 1 s; about 1 in 4000 requests. After the fix, a 3 ms-window reproducer saw 0 timeouts and 0 rejected frames over about 79 000 inserts. Host `rtscts` is not needed: the J-Link interface MCU follows the device's RTS.

Final review (Opus, fresh context, on linux-tools `f3f35f9..01bdfdc` and opener-samples `c527c00..efc3202`): verdict "with fixes", 0 critical, 5 important, 7 minor. All 5 important findings were fixed in `4cb87d4`:

- A fill no longer overlaps a running grid window: it is bounded by the first queued window, whatever its start (`test_no_fill_over_running_window`).
- `OP_FINISHED` status is checked: a failed grid window yields `Gap("failed, status N")`, a failed fill counts in `fill_rejects` (`test_failed_window_is_gap`, `test_failed_fill_counted`).
- Host/modem clock drift: `DRV_TIME` is read again when the anchor is older than `ANCHOR_MAX_AGE_S = 1.0` s (`test_reanchored_when_host_fast`, `test_fill_start_when_host_slow`).
- Transport loss no longer hangs: `SpinelClient.on_close` callbacks; `Radio` fails its Ops and ends its streams with `TransportClosed`; `nrp-sniffer` exits 1 (`test_transport_closed_fails_ops`, `test_transport_lost`).
- A `SpinelTimeout` on an insert is handled as a reject (grid: skip and resync; fill: `fill_rejects`) instead of ending the capture (`test_insert_timeout_is_reject`, `test_fill_insert_timeout`).
- Not done (ruling): no drain timeout or second-signal escape for a device that stays connected but stops sending `OP_FINISHED`; that breaks the spec's delivery guarantee.

After the fix pass: `pytest -q` → 154 passed, 14 skipped (tshark on PATH); `-m simdev` → 9 passed; `-m hw --peer` → 5 passed; HW 3 × 60 s CLI → 20/20/19 records, 0 gaps.

Open:

- About 0.7 ms blind zone per window boundary (375 µs transition plus the last ~0.4 ms of a window): on the EK, frames at −0.2 ms and +19.7 ms of a 20 ms window were missed, frames at 0.0, +0.1, +0.3, +5 and +19.5 ms received. It is not counted as a gap. Longer windows mean fewer boundaries, but each window and fill boundary still has it; no mitigation decided.
- One unexplained insert timeout (early in a run with 3 ms windows) where the insert was accepted and `frames_rejected` did not move; not reproduced in 29 later runs.
- The nRF9151 DK overlay's `hw-flow-control` was built, not run on a DK.
- Deferred minor findings: a duplicated `isinstance(event, MsgReceived | MsgError)` check in `RxPipeline._run`; `--depth 0` or negative is accepted, captures nothing and exits 0; a device reset is counted in `resets` but not reported as a gap; `stats.windows` counts fills too; in extcap mode, gap/reset warnings go to stderr, which Wireshark shows as errors; a few explicit-style deviations (calls in conditions).
- Declined to judge (kept as is): `Radio._schedule` leaks the token when an insert is cancelled (plan 1 code; the sniffer never cancels one); two rejects in a row still end the capture (`test_double_reject_raises`); no fills in semi-continuous mode; pcap timestamps are host write time; whether a PCC-only CRC error ends a single-shot RX on nRF91; host `rtscts` off.
