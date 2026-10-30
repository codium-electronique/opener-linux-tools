# Opener Spinel interface and Linux host tooling — design

- Date: 2026-10-28
- Status: approved design, pending implementation plan
- Repos: `opener` (Codium fork, branch `spinel-ctrl`), `opener-samples` (Codium fork, branch `spinel-ctrl`), `linux-tools`
- Spec references: ETSI TS 103 636-1..5 V2.2.1; Spinel: draft-rquattle-spinel-unified (expired 2017 draft, de-facto spec is OpenThread's `src/lib/spinel/spinel.h`)

## 1. Goal and scope

Add an **optional** Spinel interface to the Opener DECT NR+ stack, giving a Linux host the kind of radio co-processor control Spinel provides for OpenThread, plus the matching Linux tooling. Control is layered: this project delivers the **driver level** (the `dectnrp_driver_api`); MAC, DLC, CVG and app levels are reserved for later work and must fit without redesign.

Deliverables:

1. Spinel interface in the Opener stack, enabled by `CONFIG_DECTNRP_SPINEL`, upstreamable.
2. `samples/dectnrp-spinel` firmware in opener-samples (nRF9151 DK over UART, NR+Gate over SPI).
3. `linux-tools` Python package with:
   - `nrp-sniffer`: host-driven equivalent of the Deveritec `dectnrp-sniffer` sample (branch `deveritec/dectnrp-sniffer`), all driving logic on the host, pcap output `DLT_DECT_NR_TAP` (304);
   - `nrp-ping`: host-driven equivalent of the `dectnrp-driver` sample (periodic TX, sync to peers), interoperable with DKs running `dectnrp-driver`;
   - `nrp-hub`: service owning one link, with an exclusive control endpoint and read-only monitor endpoints, including a web UI served from the gateway.

Success criteria:

- `nrp-sniffer` captures frames emitted by `dectnrp-driver` DKs, decoded by Wireshark ≥ 4.7.4, both over UART (DK) and SPI (NR+Gate, locally and remotely through `nrp-hub`).
- `nrp-ping` running against the NR+Gate is seen by `dectnrp-driver` DKs as a peer: they log `SYNCHRONIZED` and print the received `Hello World!` payload; the host receives their frames.
- Device and host automated tests run without hardware (native_sim + pty).

The D9 and D11 driver changes are made on a separate opener branch `driver-rx-mode` as independent commits, so they can go upstream without the Spinel subsystem; `spinel-ctrl` is rebased onto them.

Out of scope: MAC/DLC/CVG Spinel levels; on-device monitoring of layers not owned by Spinel; changes to the driver beyond (a) bug fixes found on the way and (b) one minimal addition, an **RX reception mode** in the driver API (single-shot, semi-continuous, continuous) mapped to the nRF91 modem modes (D9).

## 2. Key decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | When enabled, the Spinel layer is the **exclusive** user of the dectnrp driver (RCP style). Later levels each expose the layer the firmware is built with. A future monitoring mode, if any, is read-only and never injects into layers Spinel does not own. | Deterministic, covers both use cases, no arbitration in the driver path. |
| D2 | Device side is a **thin op broker**; the host keeps the radio busy by **pipelining** ops scheduled at modem time (2–3 ops queued). | Keeps all logic on the host with the existing driver API; absorbs link and host latency. Requires the modem to accept several queued future ops (verified first, §8.3). |
| D3 | Device codec: **vendored** OpenThread `src/lib/spinel/spinel.c/.h` (BSD-3-Clause, Apache-2.0 compatible). No dependency on the OpenThread module. OpenThread C++ helpers are not used. | Zephyr's openthread module builds nothing without `CONFIG_OPENTHREAD`; pulling the repo for two files is not acceptable. |
| D4 | Two device transports, selected by devicetree: **UART + HDLC-lite** and **SPI slave** with OpenThread's Spinel SPI framing. | DKs only have a USB VCOM; the NR+Gate links the nRF9151 to the RK3566 over SPI. |
| D5 | Host: **Python** (≥ 3.11, asyncio), vendored subset of **pyspinel** (Apache-2.0, archived 2023-09). | Reuse of pyspinel and Deveritec TAP/extcap code. Measured on the NR+Gate (Cortex-A55, Python 3.14): ~12 000 pkt/s per core for parse + TAP + pcap, ≥ 5× the single-channel worst case (~2 400 pkt/s). |
| D6 | Host transports: `uart`, `spi`, `tcp`. Tools run either on the gateway (SPI) or on a PC (TCP through `nrp-hub`, or UART to a DK). | One code path for all setups. |
| D7 | `nrp-hub` owns the link: **one exclusive controller** (Unix socket or TCP), plus **read-only monitors** (TCP tap and web UI websocket) seeing all frames in both directions. Monitors can never inject. | Remote debugging over the gateway's Wi-Fi AP without control conflicts. |
| D8 | `nrp-hub` speaks only raw Spinel frames to clients, so it can be rewritten (e.g. in Go) without touching tools if profiling ever requires it. | Keeps the performance escape hatch cheap. |
| D9 | The driver API gains an RX reception mode (`struct dectnrp_rx_mode.reception`: single-shot = 0, the default and current behaviour; semi-continuous; continuous), exposed in `DRV_OP_QUEUE` flags. The nRF91 driver maps it to `NRF_MODEM_DECT_PHY_RX_MODE_*` and keeps per-message RX state, so one op can deliver several frames. | Single-shot RX ends a window at the first frame, so the rest of the window is not listened to. Continuous RX closes that gap, but the modem cannot receive PHY header type 2 format 0 in continuous mode, and that is the format `dectnrp-driver` sends. Semi-continuous stops on any unicast PDC, whatever its receiver. The hardware probe confirmed it, so the sniffer defaults to single-shot with 20 ms windows (configurable with `--window`); continuous stays available for broadcast and format 1 traffic. |
| D11 | The driver API reports **modem latencies** through `get_capabilities()`, in modem ticks and vendor-neutral terms: the minimum transition between scheduled ops, the extra startup for scheduled ops, the RX, RSSI1 and TX startup and shutdown latencies, and the stop-to-RF-off latency. Modem ticks are already the time unit of the whole driver API; each driver declares its tick rate (`DECTNRP_MODEMTICKS_PER_SYMBOL`), so the unit is uniform across drivers. The nRF91 driver keeps the values `nrf_modem_dect_phy_latency_get()` returns at init for its active radio mode. They are exposed in `DRV_CAPS`. | Measured on nRF9131 (LOW_LATENCY mode): back-to-back scheduled ops are rejected (`OP_SCHEDULING_CONFLICT`) unless spaced by `scheduled_operation_transition` (25 920 ticks, 375 µs). A host can only schedule correctly if it knows this value. |
| D10 | At driver level (`CONFIG_DECTNRP_SPINEL_LEVEL_DRIVER`, and only then) the Spinel interface **registers the DECTNRP L2 itself**; otherwise the application provides it, as the samples do today: the L2 is implemented on the host, and the device never provides one in this configuration. Future Spinel levels on top of a device L2 will not do this; that is not anticipated now. | The driver needs an L2 hook (`dectnrp_l2_init`), and currently only the samples define one. |

## 3. Architecture

```
 Linux host (PC or gateway)                                  nRF9151 (Zephyr)
 ┌──────────────────────────────────────────┐        ┌───────────────────────────────────┐
 │ tools: nrp-sniffer · nrp-ping · (L2…)    │        │ samples/dectnrp-spinel (firmware) │
 │   │  dectnrp client API (ops, tokens,    │        │   └─ CONFIG_DECTNRP_SPINEL=y      │
 │   │  pipelining, events)                 │        │ opener subsys/spinel              │
 │   │  spinel codec (vendored pyspinel)    │        │   core: frame dispatch, props,    │
 │   │  transport: uart │ spi │ tcp         │        │         op slot pool + tokens     │
 │   ▼                                      │  SPI   │   transport: uart-hdlc │ spi      │
 │ nrp-hub (gateway): owns the link         │◄──────►│   codec: vendored spinel.c (BSD-3)│
 │   ├ control: unix sock + TCP (exclusive) │ or UART│   ▼                               │
 │   └ monitor: websocket web UI + TCP tap  │        │ dectnrp_driver_api (nrf91x1)      │
 └──────────────────────────────────────────┘        └───────────────────────────────────┘
```

### 3.1 Code locations

Opener fork (`opener-samples/deps/dectnrp-opener`), all built only with `CONFIG_DECTNRP_SPINEL=y`:

| Path | Content |
|---|---|
| `subsys/spinel/` | core (dispatch, property table, op broker, event forwarding) and the driver-level DECTNRP L2 registration (D10) |
| `subsys/spinel/transport/uart_hdlc.c`, `spi.c` | transports |
| `subsys/spinel/lib/spinel.c`, `spinel.h`, `LICENSE` | vendored OpenThread codec, original BSD-3 header kept, upstream commit recorded |
| `include/zephyr/net/dectnrp_spinel.h` | DECT NR+ property / command / capability / status IDs — **single source of truth** |
| `dts/bindings/opener,spinel-spi.yaml` | SPI transport binding (SPI device + `irq-gpios` + `busy-gpios`) |
| `doc/spinel.rst` | protocol specification (§4 of this document) for upstream |
| `tests/subsys/spinel/` | ztest suites + native_sim simulated device |

opener-samples: `samples/dectnrp-spinel/` — thin application enabling the subsystem; board files for `nrf9151dk/nrf9151/ns` (Spinel on VCOM UART, logs on RTT) and `cod_nrp_gw/nrf9151/ns` (Spinel on `spi2`, IRQ P0.09, BUSY P0.26, logs on `uart0`).

linux-tools: Python package (working name `opener_tools`):

| Module | Content |
|---|---|
| `transport/` | `uart`, `spi`, `tcp` transports |
| `spinel/` | vendored pyspinel subset (codec, constants, HDLC) + DECT NR+ constants |
| `dectnrp/` | driver-level client API, timing helpers, `RxPipeline` |
| `capture/` | DECT NR TAP record builder, pcap writer, Wireshark extcap |
| `hub/` | `nrp-hub` service and static web UI |
| `cli/` | `nrp-sniffer`, `nrp-ping`, `nrp-hub` entry points |

## 4. Protocol

### 4.1 Framing and commands

Standard Spinel frames: header byte `FLG=0b10 | IID=0 | TID`. Host requests use TID 1–15; the device echoes the TID in its reply. Unsolicited frames (events, reset notification) use TID 0.

Commands used: `CMD_NOOP`, `CMD_RESET`, `CMD_PROP_VALUE_GET`, `CMD_PROP_VALUE_SET`, `CMD_PROP_VALUE_INSERT`; replies `CMD_PROP_VALUE_IS`, `CMD_PROP_VALUE_INSERTED`. Errors are returned as `CMD_PROP_VALUE_IS(PROP_LAST_STATUS, status)` with the request TID.

Status codes: standard Spinel statuses (`OK`, `FAILURE`, `INVALID_ARGUMENT`, `PARSE_ERROR`, `PROP_NOT_FOUND`, `INVALID_COMMAND`, `NOMEM`, `BUSY`, `ALREADY`, `RESET_*`). Driver errnos are returned as vendor status `STATUS_DECTNRP_ERRNO_BASE + errno` (vendor status range 15360–16383).

Maximum frame size: `CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE`, default 1300 (Spinel default), which fits `DECTNRP_MTU` (1024) plus headers. Hosts read it from `DRV_CAPS`.

### 4.2 Identity (standard core properties)

| Property | Value |
|---|---|
| `PROP_PROTOCOL_VERSION` | 4.3 |
| `PROP_NCP_VERSION` | `OPENER/<opener version>; <driver name>; <build date>` |
| `PROP_INTERFACE_TYPE` | `0x4E52` ("NR"), DECT NR+ — new value, to be agreed upstream |
| `PROP_VENDOR_ID` | Opener Initiative vendor ID (to be agreed upstream; 0 until then) |
| `PROP_CAPS` | `CAP_DECTNRP_DRV`; later `CAP_DECTNRP_MAC`, `CAP_DECTNRP_DLC`, `CAP_DECTNRP_CVG` (vendor CAP range) |
| `PROP_LAST_STATUS` | as per Spinel; `RESET_POWER_ON` sent unsolicited at boot, `RESET_SOFTWARE` after `CMD_RESET` |

A host discovers which levels a firmware exposes from `PROP_CAPS`.

### 4.3 Vendor property ranges

Vendor property range is `0x3C00–0x3FFF`; `0x3C00–0x3C7F` is avoided (used by ESP-IDF).

| Range | Level |
|---|---|
| `0x3C80–0x3CFF` | Opener core (stats, log level, …) |
| `0x3D00–0x3DFF` | **driver** (this project) |
| `0x3E00–0x3EFF` | MAC (reserved) |
| `0x3F00–0x3F7F` | DLC (reserved) |
| `0x3F80–0x3FFF` | CVG / app (reserved) |

### 4.4 Driver-level properties

All multi-byte integers are Spinel little-endian types; `d` is Spinel length-prefixed data.

| Property | ID | Access | Spinel format | Maps to |
|---|---|---|---|---|
| `DRV_ENABLED` | `0x3D00` | get/set | `b` | `start()` / `stop()` |
| `DRV_CAPS` | `0x3D01` | get | see below | `get_capabilities()` (hw caps, latencies), RDCAP Kconfig values |
| `DRV_TIME` | `0x3D02` | get | `X` (u64) | `get_time()` |
| `DRV_NETWORK_ID` | `0x3D03` | get/set | `L` | `configure(NETWORK_ID)` |
| `DRV_FILTER_SHORT_NWID` | `0x3D04` | get/set | `bC` (active, value) | `configure(FILTER_SHORT_NETWORK_ID)` |
| `DRV_FILTER_SHORT_RXADDR` | `0x3D05` | get/set | `bS` (active, value) | `configure(FILTER_SHORT_RECEIVER_ADDR)` |
| `DRV_OP_QUEUE` | `0x3D10` | insert | see §4.5 | `schedule()` |
| `DRV_EVENT` | `0x3D11` | async stream | see §4.6 | driver event callback |
| `DRV_STATS` | `0x3D20` | get | see below | counters |

`DRV_CAPS` (`L` hw_caps bitmask, then `C` release, `C` power class max, `C` NSS RX max, `C` RX/TX diversity max, `c` RX gain dB, `C` MCS max, `L` soft buffer size, `C` HARQ processes max, `C` HARQ feedback delay, `C` mu max, `C` beta max, `L` modem ticks per symbol, `S` max frame size, `C` op slots, then the D11 latencies in modem ticks: `L` scheduled-op transition, `L` scheduled-op startup, `L` RX startup, `L` RX shutdown, `L` RSSI1 shutdown, `L` TX startup, `L` TX shutdown, `L` stop to RF off). Getters return the values most recently set (filters, network ID).

`DRV_STATS` (`L` each): ops accepted, ops rejected, events sent, events dropped, frames received from host, frames rejected (parse/CRC), transport errors.

### 4.5 `DRV_OP_QUEUE` (insert)

Common prefix: `S token` (host-chosen, unique among in-flight ops), `C type` (0 TX, 1 RX, 2 RSSI1), `S channel`, `C flags`, `X start_time` (modem ticks; used only if `is_scheduled`).

`flags` bits: 0 `is_scheduled`, 1 `is_lbt` (TX), 2 `is_harq` (TX), 3 `is_beacon` (TX/RX), 5..4 RX reception mode (RX only: 0 single-shot, 1 semi-continuous, 2 continuous; 3 → `INVALID_ARGUMENT`; must be 0 for TX/RSSI1).

Type-specific suffix:

| Type | Fields |
|---|---|
| TX | `C phy_header_type` (0 = type 1, 5 B; 1 = type 2, 10 B), `S tbs`, `C mu`, `C beta`, `C mcs`, `C slots`, `L lbt_period`, `C lbt_rssi_threshold`, `d data` (PCC ‖ PDC) |
| RX | `L duration` (modem ticks), `C expected_rssi` |
| RSSI1 | `L subslots` |

Reply: `CMD_PROP_VALUE_INSERTED(DRV_OP_QUEUE, token)` once `schedule()` succeeded. Failures: `NOMEM` (no free slot), `ALREADY` (token in flight), `INVALID_ARGUMENT` / `PARSE_ERROR` (bad fields, data larger than PCC+PDC limits), driver errno status (`schedule()` failed). After `INSERTED`, the device guarantees exactly one `OP_FINISHED` event for the token, unless a reset (`CMD_RESET` or reboot, signalled by `LAST_STATUS = RESET_*`) intervenes; a reset invalidates all in-flight tokens.

### 4.6 `DRV_EVENT` (async stream)

Sent as unsolicited `CMD_PROP_VALUE_IS(DRV_EVENT, …)`. Common prefix: `S token`, `C code`, `l status` (driver status, 0 = OK), `S dropped` (number of events dropped since the previous delivered event, saturating).

| Code | Value | Suffix |
|---|---|---|
| `MSG_RECEIVED` | 1 | `C phy_type`, `C valid` (bit0 rssi2, bit1 snr), `X start_time`, `C rssi2`, `c snr`, `S channel`, `d pcc`, `d pdc` |
| `MSG_ERROR` | 2 | `C phy_type`, `C valid` (bit0 rssi2, bit1 snr, bit2 pcc), `X start_time`, `C rssi2`, `c snr`, `S channel`, `d pcc` (empty if not valid) |
| `OP_RESULTS` | 3 | `S channel`, `d rssi1` (one byte per subslot, `DECTNRP_SUBSLOTS_PER_FRAME` per frame) |
| `OP_FINISHED` | 4 | — (always the last event for the token; frees its slot) |

`rssi2` and `snr` are forwarded raw, as encoded by the driver (ETSI TS 103 636-2 §8.3 / §8.4). `channel` is the channel of the op that produced the event.

## 5. Device design

### 5.1 Startup

`SYS_INIT` at application level: look up the dectnrp `net_if` (default iface, overridable by Kconfig), register the Spinel layer as the driver event handler, initialise the transport, send `LAST_STATUS = RESET_POWER_ON`. The driver stays stopped until the host sets `DRV_ENABLED = true`.

### 5.2 Threads and buffers

- **Spinel RX thread**: receives frames from the transport, decodes with `spinel_datatype_unpack()`, dispatches, calls the driver, enqueues the reply (same TID).
- **Driver event callback** (modem event context): must not block; PCC/PDC buffers are valid only during the call. It copies the event into a buffer from a preallocated `net_buf` pool and puts it on a `k_fifo`.
- **Spinel TX thread**: encodes queued replies and events and writes them to the transport, in order.
- **Op slot pool**: `CONFIG_DECTNRP_SPINEL_OP_SLOTS` (default 8). Each slot holds a `struct dectnrp_driver_op`, its token, the TX `net_pkt`, the RSSI1 result storage, and a preallocated `OP_FINISHED` event. A slot is freed after its `OP_FINISHED` is sent.

### 5.3 Overload behaviour

- If the event pool is exhausted, `MSG_RECEIVED`, `MSG_ERROR` and `OP_RESULTS` events are dropped and counted (`DRV_STATS`, and `dropped` field of the next delivered event).
- `OP_FINISHED` is never dropped (per-slot storage), so slots cannot leak and the host never waits forever.

### 5.4 UART transport (HDLC-lite)

Spinel HDLC-lite: flag `0x7E`, escape `0x7D` (XOR `0x20`) for `0x7E`, `0x7D`, `0x11`, `0x13`, `0xF8`; FCS-16 (CRC-16/X-25, Zephyr CRC helpers) appended little-endian. Interrupt-driven UART, `current-speed` 1 000 000 by default. Frames with bad FCS are counted and dropped. Selected with `chosen { opener,spinel-uart = &uartN; }`.

### 5.5 SPI transport (slave)

Spinel SPI framing (OpenThread `src/lib/spinel/spi_frame.hpp`): 5-byte header `HDR u8 | RECV_LEN u16 LE | DATA_LEN u16 LE`, followed by the raw Spinel frame (no HDLC). `HDR`: bit7 `RST`, bit6 `CRC`, bit5 `CCF`, bits1..0 pattern `0b10`. Optional CRC-16 trailer only when both sides set `CRC` (off by default).

- The device keeps one `spi_transceive()` armed in slave mode (`nordic,nrf-spis`), with a TX buffer containing the header (`RECV_LEN` = its RX capacity, `DATA_LEN` = length of the pending outbound frame, or 0) and the frame.
- **IRQ** (P0.09, active high; host side rising edge on GPIO4_A0) is asserted while an outbound frame is pending and released when a transaction carrying it completes.
- **BUSY** (P0.26, active high; host GPIO1_A1) is asserted while the device is not armed for a transaction (processing, re-arming). The host waits for BUSY low before clocking. BUSY is an extension to the Spinel SPI spec.
- A frame is considered sent only if the host clocked at least `5 + DATA_LEN` bytes; otherwise it stays pending. An inbound frame is accepted only if `DATA_LEN ≤ RECV_LEN` advertised by the device. Headers with a bad pattern are ignored (counted). `RST` is set in the first header after boot.
- IRQ polarity is active high (rising edge), unlike OpenThread's falling-edge convention; documented in `doc/spinel.rst`.

Devicetree binding `opener,spinel-spi`: child of the SPI slave controller, properties `irq-gpios`, `busy-gpios`. On `cod_nrp_gw` these point to the existing `nrp_isr` (P0.09) and `nrp_busy` (P0.26) lines.

### 5.6 Errors and reset

Malformed frame → `PARSE_ERROR`; unknown property → `PROP_NOT_FOUND`; unsupported command → `INVALID_COMMAND`; driver errno → vendor errno status. `CMD_RESET` stops the driver, waits for outstanding ops to finish (bounded timeout), frees all slots, and sends `LAST_STATUS = RESET_SOFTWARE`. No reboot.

## 6. Host design

Python ≥ 3.11, asyncio, single `pyproject.toml`; optional extras `[spi]` (`spidev`, `gpiod` ≥ 2) and `[hub]` (`aiohttp`); base dependency `pyserial`.

### 6.1 Transports

All transports exchange whole Spinel frames and are selected by URL:

| URL | Implementation |
|---|---|
| `uart:///dev/ttyACM0?baud=1000000` | pyserial + HDLC-lite |
| `spi:///dev/spidev3.0?irq=gpiochip4:0&busy=gpiochip1:1&speed=8000000` | spidev + gpiod 2.x edge events, framing as §5.5 |
| `tcp://<host>:<port>` | `nrp-hub` control port, frames as `[len u16 LE][frame]` |

The SPI transport waits on IRQ edges (or a pending outbound frame), waits for BUSY low, clocks one transaction of `5 + max(outbound, device DATA_LEN, small default)` bytes, and issues a second transaction if the device's pending frame was longer than what was clocked. On the NR+Gate `/dev/spidev3.0` is obtained by binding spidev to `spi3.0` via `driver_override` (no kernel driver for `codium,nrp-spi`).

### 6.2 Spinel layer

Vendored pyspinel subset: codec (`encode_fields` / `parse_fields`, packed uint), constants, HDLC. pyspinel's threaded `WpanApi` is not used. A small async client handles TID allocation and matching, request timeouts, `LAST_STATUS` → exception mapping, and fan-out of unsolicited frames to subscribers. DECT NR+ constants live in `spinel/dectnrp_constants.py`, mirroring `dectnrp_spinel.h`.

### 6.3 Driver-level client API (`dectnrp/`)

- `Radio(client)`: `reset()`, `enable(bool)`, `caps()`, `time()`, `set_network_id()`, `set_filter_*()`, `stats()`, `schedule_rx()`, `schedule_tx()`, `schedule_rssi1()`, `events()` (async iterator of all events).
- `schedule_*()` allocate a token and return an `Op`, an awaitable that completes on `OP_FINISHED` and exposes the events received for that token.
- Timing helpers derived from `DRV_CAPS`: modem ticks per symbol / subslot / slot / frame; ms ↔ ticks.
- `RxPipeline(radio, channel, window, depth=3, reception=…)`: reads `DRV_TIME`, schedules `depth` RX windows back to back (`start = previous start + duration + scheduled-op transition`), schedules a new one on each `OP_FINISHED`, and yields received / error events. If the device rejects an op or a window would start in the past, it resyncs from `DRV_TIME` and reports the coverage gap (start, length).

### 6.4 Capture (`capture/`)

DECT NR TAP record builder and libpcap writer (link type 304), derived from the Deveritec sniffer host code (Apache-2.0, attribution kept). TLVs: data type, header status (PDC CRC error → status from `MSG_ERROR`), PCC/PDC RSSI2 and SNR when valid, real channel (from the event), transaction ID (token), padding to the reference dissector's minimum record length. pcap timestamps are host time. The DECT-NR-TAP format defines TLV types 1–9 only; on request (`--modem-time-tlv`) the records also carry the frame start in modem time as TLVs 10 (hi) and 11 (lo), an Opener addition that Wireshark shows as unknown TLVs. Wireshark extcap entry point shares the same writer.

### 6.5 Tools

- **`nrp-sniffer`**: `--transport URL`, `--channel`, `--network-id`, `--rx-mode single|semi|continuous`, `--window` (ms), `--depth`, `-w FILE|-`; also usable as a Wireshark extcap (`--extcap-interfaces`, `--capture`, `--fifo`, …). Flow: reset → set network ID → enable → `RxPipeline` → TAP/pcap. Ctrl-C/SIGTERM: stop scheduling, wait for outstanding ops, disable. Prints stats (records, CRC errors, dropped events, coverage gaps).
- **`nrp-ping`**: reproduces `samples/dectnrp-driver` behaviour (`src/main.c`, `src/dectnrp_mac_helper.c`): same network ID default, random short ID, same PCC type / transport parameters / `Hello World!` payload, unsynchronised RX-then-TX period (`--period`, default 3000 ms), synchronisation to a peer from the `start_time` of its received frame (TX at period/2 offset), fallback to unsynchronised when nothing is received. PCC field helpers ported from `dectnrp_mac_helper.c`.
- **`nrp-hub`**: owns one transport of any type (systemd unit on the NR+Gate with `spi://…`; also usable on a PC with a DK over `uart://…`).
  - Control: Unix socket and TCP port (default 4950); one controller at a time, others refused. Frames are relayed unchanged in both directions. On controller disconnect, the hub sends `CMD_RESET` so the next controller starts clean.
  - Monitor TCP tap (default 4951): read-only stream of `[ts_ns u64 LE][dir u8: 0 host→dev, 1 dev→host][len u16 LE][frame]`. Input from monitor clients is ignored.
  - Web UI (HTTP, default 8080): aiohttp serving static HTML/JS (no build step) and a read-only websocket. Shows controller connection state, a live decoded Spinel trace (commands, replies, events), driver stats, and received frames (decoded PCC fields). Updates batched (~100 ms). Can start/stop a pcap (TAP) recording on the gateway from observed `MSG_RECEIVED` / `MSG_ERROR` events, independent of which tool controls the link; recordings downloadable from the UI.

## 7. Error handling summary

| Situation | Behaviour |
|---|---|
| Device reboot | Host sees unsolicited `RESET_POWER_ON`; client fails in-flight ops with `DeviceReset`; tools re-initialise or exit (`nrp-sniffer`: re-init and continue, logging a gap). |
| Request timeout | Client raises `Timeout`; tools retry idempotent gets, abort on op inserts after resync. |
| Event drops on device | `dropped` field and `DRV_STATS`; sniffer logs and counts. |
| Link errors (FCS, bad SPI header) | Counted on both sides; frame dropped; requests recovered by timeout/retry. |
| Pipeline falls behind | `RxPipeline` resync from `DRV_TIME`, gap reported. |
| Second controller connects to hub | Refused with an error message; monitors unaffected. |

## 8. Testing

### 8.1 Device (opener fork, `tests/subsys/spinel/`, ztest on `native_sim`)

- Mock dectnrp driver (`native_sim` fake device implementing `dectnrp_driver_api`): records scheduled ops, emits scripted events.
- Unit suites: dispatch and error statuses; slot pool and token rules; `OP_FINISHED` never dropped under event-pool exhaustion; HDLC encode/decode; SPI header build/parse.
- Simulated device image: Spinel core + UART transport + mock driver; native_sim exposes the UART as a pty, used by host integration tests.
- Run: `west twister -T deps/dectnrp-opener/tests -p native_sim`.

### 8.2 Host (linux-tools, pytest + ruff)

- Unit: codec, HDLC, SPI framing (fake spidev / gpiod), client TID handling and timeouts, `RxPipeline` with a fake modem clock (back-to-back, resync, gap report), TAP / pcap output cross-checked against the Deveritec record format, constants vs `dectnrp_spinel.h` (test parses the C header).
- Integration: sniffer and client flows against the native_sim simulated device over its pty.

### 8.3 Hardware validation

1. Risk probe (first): on a DK, schedule 3 RX windows back to back at modem time; confirm all accepted and executed without gaps. If the modem rejects queued ops, revisit D2. After the D9 driver change, repeat with each reception mode against a `dectnrp-driver` peer (type 2 format 0 frames) to choose the sniffer's default mode and window.
2. DK over UART: smoke (reset, caps, time, enable); `nrp-sniffer` captures a second DK running `dectnrp-driver`; Wireshark 4.7.4 decodes the capture.
3. NR+Gate over SPI: same sniffer locally on the gateway, then from a PC through `nrp-hub` over TCP, with the web UI monitoring.
4. `nrp-ping` on the NR+Gate ↔ DK running `dectnrp-driver`: DK logs `SYNCHRONIZED` and the `Hello World!` payload; host receives the DK's frames.

### 8.4 CI

- opener-samples: build `samples/dectnrp-spinel` for `nrf9151dk/nrf9151/ns`.
- opener: existing clang-format and DCO checks, plus the twister run.
- linux-tools: pytest + ruff.

## 9. Milestones

1. Device core + UART transport; host codec, UART transport, client; native_sim tests; risk probe; DK smoke test.
2. `nrp-sniffer` over UART, end to end with Wireshark.
3. SPI transport on both sides; sniffer on the NR+Gate.
4. `nrp-ping` interoperability with `dectnrp-driver` DKs.
5. `nrp-hub`: control and monitor endpoints, then the web UI.

## 10. Open points for upstream discussion

- `PROP_INTERFACE_TYPE` value for DECT NR+ (`0x4E52` proposed) and `PROP_VENDOR_ID`.
- Vendor range split (§4.3) as the Opener-wide allocation for future levels.
- The RX reception mode (D9), the latencies in device capabilities (D11) and the nRF91 RX-state fix, as driver-API change proposals.
- Running the Spinel ztests: they run in opener-samples' CI (opener is not a west workspace); to be discussed with the community.
