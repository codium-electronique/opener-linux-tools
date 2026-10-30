# nrp-sniffer

`nrp-sniffer` captures DECT NR+ frames with a device running the Opener
`dectnrp` driver and its Spinel interface (firmware sample
`opener-samples/samples/dectnrp-spinel`). All the capture logic runs on the
host: it initialises the radio, keeps RX windows queued on the device and
writes each received frame as a pcap record. The device only runs the driver.

The output is a pcap with link type `DECT_NR_TAP` (304), the DECT-NR-TAP
format of the Deveritec `dectnrp-sniffer` sample. Wireshark 4.7.4 or later
decodes it (TAP header, then the PCC and MAC PDU).

## Command line

```bash
nrp-sniffer --transport 'uart:///dev/ttyACM0?baud=1000000' -w capture.pcap
```

Stop it with Ctrl-C (or SIGTERM): the queued windows finish, the radio is
disabled and the stats go to stderr, for example:

```
20 records, 0 CRC errors, 0 dropped events, 0 gaps (0.0 ms), 0 device resets
```

Without `-w`, or with `-w -`, the pcap goes to stdout, flushed after each
record, to pipe it into Wireshark:

```bash
nrp-sniffer --transport uart:///dev/ttyACM0 | wireshark -k -i -
```

| Option | Default | |
|---|---|---|
| `--transport URL` | (required) | `uart:///dev/ttyACM0?baud=1000000` (1 Mbaud is the default baud rate) or `tcp://host:port` |
| `--channel` | 1677 | DECT NR+ carrier number |
| `--network-id` | `0x32545279` | 32-bit network ID, decimal or `0x` hex |
| `--rx-mode` | `single` | `single`, `semi` or `continuous`, see below |
| `--window MS` | 20 | RX window length, fractional values accepted |
| `--depth` | 4 | RX windows queued on the device |
| `--lead MS` | 20 | how far ahead of the device time windows are scheduled |
| `--modem-time-tlv` | off | add the frame start modem time to each record, see below |
| `-v` | | debug logs |

The channel and network ID defaults are those of the `dectnrp-driver` sample.

If the device resets during a capture, `nrp-sniffer` logs it, initialises the
device again and goes on in the same file.

## Wireshark extcap

`nrp-sniffer` also works as a Wireshark extcap, so Wireshark lists a
"DECT NR+ Sniffer (Opener Spinel)" interface and shows the options above in
its capture options dialog. Install the wrapper with the venv where
opener-tools is installed active:

```bash
scripts/install-extcap.sh            # into ~/.local/lib/wireshark/extcap
scripts/install-extcap.sh DIR        # or into DIR
```

The wrapper runs the venv's `nrp-sniffer`, whatever environment Wireshark
starts it with. To use another folder without installing, set
`WIRESHARK_EXTCAP_DIR` to it before starting Wireshark or tshark. From the
command line:

```bash
tshark -i nrp-sniffer -o 'extcap.nrp_sniffer.transport:uart:///dev/ttyACM0?baud=1000000' -a duration:60 -w capture.pcapng
```

Wireshark reports anything an extcap writes to stderr as an error, so in extcap
mode `nrp-sniffer` logs warnings and errors only. Capture filters are ignored.

## Reception modes and windows

The nRF91 modem does not receive every frame format in every RX mode:

- `single` (single-shot, the default): receives every format. A received frame
  ends the window, so the rest of that window is not listened to; short
  windows keep that uncovered remainder short.
- `continuous`: the modem cannot receive PHY header type 2 format 0 in this
  mode, the format `dectnrp-driver` sends. Use it for broadcast or format 1
  traffic.
- `semi` (semi-continuous): stops at any unicast frame, whatever its receiver.

### Blind time between windows

The modem rejects exactly adjacent windows. They are separated by the minimum
transition between scheduled operations that the device reports (375 µs on the
nRF9131). A frame that starts in that transition, or too late in a window to fit
in it, is not received. Measured on an nRF9131 EK with 1-slot frames and 20 ms
windows, frames starting 0.3 ms before a window ends or 0.2 ms before the next
one starts were missed. Frames starting 0.5 ms before a window ends or right at
the start of the next one were received. That is about 0.7 ms out of every 20.4 ms (3.5 %).

These losses are not counted as gaps. With periodic traffic they come in runs:
the 3 s period of `dectnrp-driver` lands on the window grid at only three
phases, which drift by about 0.1 ms every three frames. A capture can then
miss one frame in three for a while, then none for minutes. Longer windows
make the blind time a smaller share, at the cost of a longer uncovered
remainder after each frame in single-shot mode.

Windows of 3 ms or less are too short for the UART link: the host falls behind
(the resyncs are logged as gaps) and then the device stops answering. Use
5 ms or more.

## Records

Each record carries the TAP header, then TLVs: data type, header status (PDC
CRC error from a `MSG_ERROR` event), transaction ID (the op token), channel,
and the PCC or PDC RSSI2 and SNR when the device reports them. The driver
reports RSSI2 and SNR as ETSI TS 103 636-2 §8.3 and §8.4 codes (1 dBm and
0.5 dB steps); the records carry them in the TAP units, 0.5 dBm and 0.25 dB.
Records shorter than the 72 bytes the Wireshark dissector requires are padded
with type 0 TLVs, which Wireshark shows as unknown TLVs.

pcap timestamps are host time. With `--modem-time-tlv`, each record also
carries the frame start in modem time ticks (69.12 MHz on nRF91) as TLVs 10
(high 32 bits) and 11 (low 32 bits). DECT-NR-TAP defines TLV types 1 to 9
only, so Wireshark shows these two as unknown TLVs.
