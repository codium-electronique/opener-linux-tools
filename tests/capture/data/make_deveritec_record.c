/*
 * Copyright (c) 2026 Codium Electronique
 * SPDX-License-Identifier: Apache-2.0
 *
 * Writes deveritec_record.bin with the Deveritec sniff_build_tap_record, for
 * the frame of test_tap.py. See README.md.
 */
#include <stdio.h>

#include "sniffer_codec.h"

int main(void)
{
	static const uint8_t body[] = {1, 2, 3, 4, 5, 'H', 'e', 'l', 'l', 'o', ' ',
				       'W', 'o', 'r', 'l', 'd', '!'};
	struct sniff_rx_info info = {
		.phy_type = 0,
		.pdc_len = 12,
		.channel = 1677,
		.seq = 7,
		.has_metrics = true,
		/* -35 dBm in Q14.1, -1.5 dB in Q13.2. */
		.metrics = {.pdc_rssi2 = -70, .pdc_snr = -6},
	};
	uint8_t record[SNIFF_TAP_MAX_RECORD(sizeof(body))];
	size_t len = sniff_build_tap_record(record, sizeof(record), &info, body);

	fwrite(record, 1, len, stdout);
	return 0;
}
