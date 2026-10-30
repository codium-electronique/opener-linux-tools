`deveritec_record.bin` is the DECT-NR-TAP record that the Deveritec sniffer's
`sniff_build_tap_record` builds for the frame of `test_tap.py`. It was produced
from `opener-samples` branch `deveritec/dectnrp-sniffer` with:

```bash
git show deveritec/dectnrp-sniffer:samples/dectnrp-sniffer/src/sniffer_codec.c > sniffer_codec.c
git show deveritec/dectnrp-sniffer:samples/dectnrp-sniffer/src/sniffer_codec.h > sniffer_codec.h
cc -I. -o gen make_deveritec_record.c sniffer_codec.c && ./gen > deveritec_record.bin
```
