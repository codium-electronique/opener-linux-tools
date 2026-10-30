// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Codium Electronique

//! Checks the NR+Gate SPI link to the nRF9151 running dectnrp-spinel: reads
//! PROTOCOL_VERSION and NCP_VERSION, times PROTOCOL_VERSION and DRV_TIME round
//! trips, and checks the modem clock against the host's.

mod frame;
mod link;

use std::process::ExitCode;
use std::time::{Duration, Instant};

use frame::{
    PROP_DRV_ENABLED, PROP_DRV_TIME, PROP_NCP_VERSION, PROP_PROTOCOL_VERSION, decode_packed,
    prop_value_get, prop_value_is, prop_value_set,
};
use link::{LineSpec, Link, Result};

/// One modem tick is 1/69120000 s (Opener dectnrp_driver.h).
const MODEM_TICKS_PER_S: f64 = 69_120_000.0;

const USAGE: &str = "usage: nrp-spi-probe [--spi PATH] [--irq CHIP:LINE] [--busy CHIP:LINE] \
                     [--speed HZ] [--clock BYTES] [--count N]";

struct Args {
    spi: String,
    irq: LineSpec,
    busy: LineSpec,
    speed: u32,
    clock: usize,
    count: usize,
}

fn parse_line(value: &str) -> Result<LineSpec> {
    let (chip, line) = value.split_once(':').ok_or("expected CHIP:LINE")?;
    let line = line.parse()?;
    Ok(LineSpec {
        chip: chip.to_string(),
        line,
    })
}

fn parse_args() -> Result<Args> {
    let mut args = Args {
        spi: "/dev/spidev3.0".to_string(),
        irq: LineSpec {
            chip: "gpiochip4".to_string(),
            line: 0,
        },
        busy: LineSpec {
            chip: "gpiochip1".to_string(),
            line: 1,
        },
        speed: 8_000_000,
        clock: 16,
        count: 1000,
    };
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let value = it.next().ok_or(USAGE)?;
        match flag.as_str() {
            "--spi" => args.spi = value,
            "--irq" => args.irq = parse_line(&value)?,
            "--busy" => args.busy = parse_line(&value)?,
            "--speed" => args.speed = value.parse()?,
            "--clock" => args.clock = value.parse()?,
            "--count" => args.count = value.parse()?,
            _ => return Err(USAGE.into()),
        }
    }
    Ok(args)
}

/// Transaction ids 1..=15, as Spinel reserves 0 for unsolicited frames.
struct Tids(u8);

impl Tids {
    fn next(&mut self) -> u8 {
        self.0 = self.0 % 15 + 1;
        self.0
    }
}

fn get(link: &mut Link, tids: &mut Tids, prop: u32) -> Result<Vec<u8>> {
    let request = prop_value_get(tids.next(), prop);
    let reply = link.request(&request)?;
    let value = prop_value_is(&reply, prop).ok_or("unexpected reply")?;
    Ok(value.to_vec())
}

fn set(link: &mut Link, tids: &mut Tids, prop: u32, value: &[u8]) -> Result<Vec<u8>> {
    let request = prop_value_set(tids.next(), prop, value);
    let reply = link.request(&request)?;
    let value = prop_value_is(&reply, prop).ok_or("value refused")?;
    Ok(value.to_vec())
}

fn micros(d: Duration) -> f64 {
    d.as_secs_f64() * 1e6
}

/// One timed get: the value, the round trip, and the host time of its
/// midpoint, in seconds from epoch.
struct Sample {
    value: Vec<u8>,
    round_trip: Duration,
    midpoint: f64,
}

/// Times count gets of prop and prints the round trip statistics.
fn bench(
    link: &mut Link,
    tids: &mut Tids,
    args: &Args,
    prop: u32,
    name: &str,
) -> Result<Vec<Sample>> {
    let epoch = Instant::now();
    let first = link.transactions;
    let mut samples = Vec::with_capacity(args.count);
    for _ in 0..args.count {
        let start = Instant::now();
        let value = get(link, tids, prop)?;
        let round_trip = start.elapsed();
        let midpoint = (start - epoch).as_secs_f64() + round_trip.as_secs_f64() / 2.0;
        samples.push(Sample {
            value,
            round_trip,
            midpoint,
        });
    }
    if samples.is_empty() {
        return Ok(samples);
    }

    let n = samples.len();
    let transactions = (link.transactions - first) as f64 / n as f64;
    let mut times: Vec<Duration> = samples.iter().map(|s| s.round_trip).collect();
    let total: Duration = times.iter().sum();
    times.sort();
    println!(
        "{name}: {n} round trips at {} Hz, {} B clocked: mean {:.0} us, min {:.0}, median {:.0}, \
         p99 {:.0}, max {:.0}, {:.2} transactions each",
        args.speed,
        args.clock,
        micros(total) / n as f64,
        micros(times[0]),
        micros(times[n / 2]),
        micros(times[n * 99 / 100]),
        micros(times[n - 1]),
        transactions,
    );
    Ok(samples)
}

/// Fits the modem time against the host time of each sample's midpoint and
/// prints the modem clock rate against the host's, and how far the samples
/// stray from the fit: how well the host can predict the modem time.
fn check_modem_clock(samples: &[Sample]) -> Result<()> {
    let mut points = Vec::with_capacity(samples.len());
    for sample in samples {
        let bytes: [u8; 8] = sample
            .value
            .as_slice()
            .try_into()
            .map_err(|_| "bad DRV_TIME")?;
        let ticks = u64::from_le_bytes(bytes);
        points.push((sample.midpoint, ticks));
    }
    if points.len() < 2 {
        return Ok(());
    }

    // Least squares of the modem seconds since the first sample against the
    // host seconds.
    let first_ticks = points[0].1;
    let n = points.len() as f64;
    let xy: Vec<(f64, f64)> = points
        .iter()
        .map(|&(x, ticks)| (x, (ticks - first_ticks) as f64 / MODEM_TICKS_PER_S))
        .collect();
    let mean_x = xy.iter().map(|p| p.0).sum::<f64>() / n;
    let mean_y = xy.iter().map(|p| p.1).sum::<f64>() / n;
    let sxx: f64 = xy.iter().map(|p| (p.0 - mean_x).powi(2)).sum();
    let sxy: f64 = xy.iter().map(|p| (p.0 - mean_x) * (p.1 - mean_y)).sum();
    let slope = sxy / sxx;
    let intercept = mean_y - slope * mean_x;
    let residuals: Vec<f64> = xy.iter().map(|p| p.1 - (intercept + slope * p.0)).collect();
    let rms = (residuals.iter().map(|r| r * r).sum::<f64>() / n).sqrt();
    let worst = residuals.iter().fold(0.0_f64, |acc, r| acc.max(r.abs()));
    let span = xy[xy.len() - 1].0 - xy[0].0;

    println!(
        "modem clock over {span:.2} s: {:+.1} ppm against the host, residual rms {:.0} us, max {:.0} us",
        (slope - 1.0) * 1e6,
        rms * 1e6,
        worst * 1e6,
    );
    Ok(())
}

fn run(args: &Args) -> Result<()> {
    let mut link = Link::open(&args.spi, args.speed, &args.irq, &args.busy, args.clock)?;
    let mut tids = Tids(0);

    let version = get(&mut link, &mut tids, PROP_PROTOCOL_VERSION)?;
    let (major, len) = decode_packed(&version).ok_or("bad PROTOCOL_VERSION")?;
    let (minor, _) = decode_packed(&version[len..]).ok_or("bad PROTOCOL_VERSION")?;
    let ncp = get(&mut link, &mut tids, PROP_NCP_VERSION)?;
    let ncp = String::from_utf8_lossy(&ncp);
    let ncp = ncp.trim_end_matches('\0');
    println!("PROTOCOL_VERSION {major}.{minor}, NCP_VERSION {ncp}");

    bench(
        &mut link,
        &mut tids,
        args,
        PROP_PROTOCOL_VERSION,
        "PROTOCOL_VERSION",
    )?;

    // DRV_TIME needs the driver started; leave it as it was found.
    let enabled = get(&mut link, &mut tids, PROP_DRV_ENABLED)?;
    let was_enabled = enabled.first() == Some(&1);
    if !was_enabled {
        set(&mut link, &mut tids, PROP_DRV_ENABLED, &[1])?;
    }
    let result = bench(&mut link, &mut tids, args, PROP_DRV_TIME, "DRV_TIME");
    if !was_enabled {
        set(&mut link, &mut tids, PROP_DRV_ENABLED, &[0])?;
    }
    let samples = result?;
    check_modem_clock(&samples)
}

fn main() -> ExitCode {
    let result = parse_args().and_then(|args| run(&args));
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("nrp-spi-probe: {err}");
            ExitCode::FAILURE
        }
    }
}
