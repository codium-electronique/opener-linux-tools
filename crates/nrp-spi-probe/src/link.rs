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

use crate::frame::{HEADER_LEN, build_header, parse_header, tid_of};

pub type Result<T> = std::result::Result<T, Box<dyn Error>>;

const CONSUMER: &str = "nrp-spi-probe";
const TIMEOUT: Duration = Duration::from_millis(200);
const MAX_FRAME: usize = 1300;

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
            .request()?;
        let busy_req = Request::builder()
            .on_chip(format!("/dev/{}", busy.chip))
            .with_consumer(CONSUMER)
            .with_line(busy.line)
            .as_input()
            .request()?;

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
        })
    }

    /// Send request and return the device frame with the same transaction id.
    /// Frames with another id (unsolicited ones) are skipped.
    pub fn request(&mut self, request: &[u8]) -> Result<Vec<u8>> {
        let tid = tid_of(request);
        let deadline = Instant::now() + TIMEOUT;

        let mut sent = false;
        while !sent {
            self.wait_not_busy(deadline)?;
            let (accepted, frame) = self.exchange(request)?;
            sent = accepted;
            let reply = frame.filter(|f| tid_of(f) == tid);
            if let Some(reply) = reply {
                return Ok(reply);
            }
        }

        loop {
            self.wait_irq(deadline)?;
            let (_, frame) = self.exchange(&[])?;
            let reply = frame.filter(|f| tid_of(f) == tid);
            if let Some(reply) = reply {
                return Ok(reply);
            }
        }
    }

    /// One transaction sending frame, plus a second one if the device frame
    /// was longer than clocked. Returns whether the device accepted frame
    /// and the device frame, if any.
    fn exchange(&mut self, frame: &[u8]) -> Result<(bool, Option<Vec<u8>>)> {
        let clock = self.clock.max(frame.len());
        let header = self.transfer(frame, clock)?;
        let accepted = usize::from(header.recv_len) >= frame.len();
        let data_len = usize::from(header.data_len);
        if data_len == 0 || data_len > MAX_FRAME {
            return Ok((accepted, None));
        }
        if data_len > clock {
            self.wait_not_busy(Instant::now() + TIMEOUT)?;
            let again = self.transfer(&[], data_len)?;
            if usize::from(again.data_len) != data_len {
                // Another frame than announced: the next IRQ brings it.
                return Ok((accepted, None));
            }
        }
        let received = self.rx[HEADER_LEN..HEADER_LEN + data_len].to_vec();
        Ok((accepted, Some(received)))
    }

    /// Clock a header, frame and padding up to clock payload bytes.
    fn transfer(&mut self, frame: &[u8], clock: usize) -> Result<crate::frame::SpiHeader> {
        let total = HEADER_LEN + clock;
        let header = build_header(clock as u16, frame.len() as u16);
        self.tx.clear();
        self.tx.extend_from_slice(&header);
        self.tx.extend_from_slice(frame);
        self.tx.resize(total, 0);

        let mut xfer = SpidevTransfer::read_write(&self.tx, &mut self.rx[..total]);
        self.spi.transfer(&mut xfer)?;
        self.transactions += 1;

        let parsed = parse_header(&self.rx[..total]);
        parsed.ok_or_else(|| "invalid SPI header from the device".into())
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
