// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Codium Electronique

//! Host side of the Spinel SPI link, blocking: the SPI bus, IRQ (rising
//! edge, active high: a frame is pending) and BUSY (active high: the device
//! is not armed). The device sets IRQ before it releases BUSY, so IRQ is
//! current once BUSY is inactive.

use std::error::Error;
use std::time::{Duration, Instant};

use gpiocdev::Request;
use gpiocdev::line::{EdgeDetection, Value};
use spidev::{SpiModeFlags, Spidev, SpidevOptions, SpidevTransfer};

use crate::frame::{HEADER_LEN, build_header, clocked_frame, parse_header, tid_of};

pub type Result<T> = std::result::Result<T, Box<dyn Error>>;

const CONSUMER: &str = "nrp-spi-probe";
const TIMEOUT: Duration = Duration::from_millis(200);
const MAX_FRAME: usize = 1300;
/// Pause before retrying after an invalid header or a refused frame.
const RETRY_DELAY: Duration = Duration::from_millis(1);

pub struct LineSpec {
    pub chip: String,
    pub line: u32,
}

pub struct Link {
    spi: Spidev,
    irq: Request,
    irq_line: u32,
    busy: Request,
    busy_line: u32,
    /// Payload bytes clocked when no longer frame is known.
    clock: usize,
    tx: Vec<u8>,
    rx: Vec<u8>,
    pub transactions: u64,
    /// Transactions with an invalid device header (device not armed).
    pub invalid_headers: u64,
}

impl Link {
    pub fn open(
        path: &str,
        speed: u32,
        irq: &LineSpec,
        busy: &LineSpec,
        clock: usize,
    ) -> Result<Link> {
        let mut spi = Spidev::open(path).map_err(|e| format!("{path}: {e}"))?;
        let options = SpidevOptions::new()
            .bits_per_word(8)
            .max_speed_hz(speed)
            .mode(SpiModeFlags::SPI_MODE_0)
            .build();
        spi.configure(&options)?;

        let irq_req = Request::builder()
            .on_chip(format!("/dev/{}", irq.chip))
            .with_consumer(CONSUMER)
            .with_line(irq.line)
            .as_input()
            .with_edge_detection(EdgeDetection::RisingEdge)
            .request()
            .map_err(|e| format!("/dev/{} line {}: {e}", irq.chip, irq.line))?;
        let busy_req = Request::builder()
            .on_chip(format!("/dev/{}", busy.chip))
            .with_consumer(CONSUMER)
            .with_line(busy.line)
            .as_input()
            .request()
            .map_err(|e| format!("/dev/{} line {}: {e}", busy.chip, busy.line))?;

        let buf_len = HEADER_LEN + MAX_FRAME;
        Ok(Link {
            spi,
            irq: irq_req,
            irq_line: irq.line,
            busy: busy_req,
            busy_line: busy.line,
            clock,
            tx: Vec::with_capacity(buf_len),
            rx: vec![0; buf_len],
            transactions: 0,
            invalid_headers: 0,
        })
    }

    /// Send request and return the device frame with the same transaction id.
    /// Frames with another id (unsolicited ones) are skipped.
    pub fn request(&mut self, request: &[u8]) -> Result<Vec<u8>> {
        let tid = tid_of(request);
        let deadline = Instant::now() + TIMEOUT;

        let mut sent = false;
        loop {
            if Instant::now() > deadline {
                return Err("no reply from the device".into());
            }
            let mut out: &[u8] = &[];
            if sent {
                self.wait_irq(deadline)?;
            } else {
                self.wait_not_busy(deadline)?;
                out = request;
            }
            let (accepted, frame) = self.exchange(out)?;
            if !accepted {
                std::thread::sleep(RETRY_DELAY);
            }
            if !out.is_empty() && accepted {
                sent = true;
            }
            let reply = frame.filter(|f| tid_of(f) == tid);
            if let Some(reply) = reply {
                return Ok(reply);
            }
        }
    }

    /// One transaction sending frame, plus a second one if the device frame
    /// was longer than clocked. Returns whether the device header was valid
    /// and accepted frame, and the device frame clocked whole, if any.
    fn exchange(&mut self, frame: &[u8]) -> Result<(bool, Option<Vec<u8>>)> {
        let clock = self.clock.max(frame.len());
        let total = self.transfer(frame, clock)?;
        let header = parse_header(&self.rx[..total]);
        let Some(header) = header else {
            self.invalid_headers += 1;
            return Ok((false, None));
        };
        let accepted = usize::from(header.recv_len) >= frame.len();
        let data_len = usize::from(header.data_len);
        let mut total = total;
        if data_len > clock && data_len <= MAX_FRAME {
            self.wait_not_busy(Instant::now() + TIMEOUT)?;
            total = self.transfer(&[], data_len)?;
            let again = parse_header(&self.rx[..total]);
            if again.is_none() {
                self.invalid_headers += 1;
            }
        }
        // The device may have armed another frame for the second
        // transaction: any frame clocked whole is sent, so deliver it.
        let received = clocked_frame(&self.rx[..total]).map(<[u8]>::to_vec);
        Ok((accepted, received))
    }

    /// Clock a header, frame and padding up to clock payload bytes. Returns
    /// the number of bytes clocked.
    fn transfer(&mut self, frame: &[u8], clock: usize) -> Result<usize> {
        let total = HEADER_LEN + clock;
        let header = build_header(clock as u16, frame.len() as u16);
        self.tx.clear();
        self.tx.extend_from_slice(&header);
        self.tx.extend_from_slice(frame);
        self.tx.resize(total, 0);

        let mut xfer = SpidevTransfer::read_write(&self.tx, &mut self.rx[..total]);
        self.spi.transfer(&mut xfer)?;
        self.transactions += 1;
        Ok(total)
    }

    fn wait_not_busy(&self, deadline: Instant) -> Result<()> {
        loop {
            let busy = self.busy.value(self.busy_line)?;
            if busy == Value::Inactive {
                return Ok(());
            }
            if Instant::now() > deadline {
                return Err("device stays busy".into());
            }
            std::hint::spin_loop();
        }
    }

    /// Wait until the device has a frame pending, BUSY inactive.
    fn wait_irq(&self, deadline: Instant) -> Result<()> {
        loop {
            self.wait_not_busy(deadline)?;
            while self.irq.has_edge_event()? {
                self.irq.read_edge_event()?;
            }
            let irq = self.irq.value(self.irq_line)?;
            if irq == Value::Active {
                return Ok(());
            }
            let left = deadline.saturating_duration_since(Instant::now());
            let edge = self.irq.wait_edge_event(left)?;
            if !edge {
                return Err("no reply from the device".into());
            }
        }
    }
}
