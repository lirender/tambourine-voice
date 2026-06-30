//! ScreenCaptureKit Proof-of-Concept: Capture system audio from a target app.
//!
//! Usage:
//!   cargo run                    # List running apps, then capture all system audio for 10s
//!   cargo run -- --app "Teams"   # Capture only Teams audio for 10s
//!   cargo run -- --duration 30   # Capture for 30 seconds
//!   cargo run -- --list          # Just list running apps

use anyhow::{Context, Result};
use hound::{SampleFormat, WavSpec, WavWriter};
use screencapturekit::prelude::*;
use std::env;
use std::io::BufWriter;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

const SAMPLE_RATE: u32 = 16_000;
const CHANNELS: u16 = 1;

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();

    let list_only = args.contains(&"--list".to_string());
    let duration_secs = args
        .iter()
        .position(|a| a == "--duration")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(10);
    let app_filter = args
        .iter()
        .position(|a| a == "--app")
        .and_then(|i| args.get(i + 1))
        .cloned();
    // Explicit output path (the app passes this); falls back to a /tmp default.
    let output_override = args
        .iter()
        .position(|a| a == "--output")
        .and_then(|i| args.get(i + 1))
        .cloned();

    // Get shareable content (triggers permission prompt on first run)
    println!("Requesting screen capture permission...");
    let content = SCShareableContent::get().context("Failed to get shareable content. Grant Screen Recording permission in System Settings.")?;

    let apps = content.applications();
    let displays = content.displays();

    println!("\n=== Running Applications ({}) ===", apps.len());
    for app in &apps {
        let name = app.application_name();
        let bundle = app.bundle_identifier();
        let pid = app.process_id();
        if !name.is_empty() {
            println!("  {name} ({bundle}) [PID: {pid}]");
        }
    }

    if list_only {
        return Ok(());
    }

    let display = displays
        .into_iter()
        .next()
        .context("No display found")?;

    // Build content filter
    let filter = if let Some(ref app_name) = app_filter {
        let target_app = apps
            .iter()
            .find(|a| {
                let name = a.application_name().to_lowercase();
                let bundle = a.bundle_identifier().to_lowercase();
                let search = app_name.to_lowercase();
                name.contains(&search) || bundle.contains(&search)
            })
            .with_context(|| format!("App '{app_name}' not found in running applications"))?;

        println!(
            "\nCapturing audio from: {} ({})",
            target_app.application_name(),
            target_app.bundle_identifier()
        );

        SCContentFilter::create()
            .with_display(&display)
            .with_including_applications(&[target_app], &[])
            .build()
    } else {
        println!("\nCapturing ALL system audio (no app filter)");
        SCContentFilter::create()
            .with_display(&display)
            .with_excluding_windows(&[])
            .build()
    };

    // Configure audio-only capture
    let config = SCStreamConfiguration::new()
        .with_captures_audio(true)
        .with_excludes_current_process_audio(true)
        .with_sample_rate(i32::try_from(SAMPLE_RATE).unwrap())
        .with_channel_count(i32::from(CHANNELS))
        // A valid (non-1x1) video size is required even for audio-only capture;
        // 1x1 makes start_capture fail. We ignore the video frames.
        .with_width(640)
        .with_height(480);

    // Set up WAV writer
    let output_path = output_override
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/tmp/sckit-poc-{}.wav", chrono_timestamp())));
    let spec = WavSpec {
        channels: CHANNELS,
        sample_rate: SAMPLE_RATE,
        bits_per_sample: 32,
        sample_format: SampleFormat::Float,
    };

    let wav_file = std::fs::File::create(&output_path)
        .with_context(|| format!("Failed to create {}", output_path.display()))?;
    let wav_writer = Arc::new(Mutex::new(WavWriter::new(BufWriter::new(wav_file), spec)?));
    let sample_count = Arc::new(AtomicUsize::new(0));
    let is_capturing = Arc::new(AtomicBool::new(true));

    let writer_clone = wav_writer.clone();
    let count_clone = sample_count.clone();

    // Create stream with audio handler
    let mut stream = SCStream::new(&filter, &config);
    stream.add_output_handler(
        move |sample: CMSampleBuffer, of_type: SCStreamOutputType| {
            if of_type != SCStreamOutputType::Audio {
                return;
            }

            let Some(block_buffer) = sample.data_buffer() else {
                return;
            };

            let Some(data) = block_buffer.as_slice() else {
                return;
            };

            // SCKit delivers float32 PCM samples
            let float_samples: &[f32] = unsafe {
                std::slice::from_raw_parts(
                    data.as_ptr().cast::<f32>(),
                    data.len() / std::mem::size_of::<f32>(),
                )
            };

            if let Ok(mut writer) = writer_clone.lock() {
                for &sample in float_samples {
                    let _ = writer.write_sample(sample);
                }
            }

            count_clone.fetch_add(float_samples.len(), Ordering::Relaxed);
        },
        SCStreamOutputType::Audio,
    );

    // Start capture
    println!("Starting capture for {duration_secs}s...");
    println!("Output: {}", output_path.display());
    stream.start_capture()?;

    // Progress reporting
    let count_for_progress = sample_count.clone();
    let capturing_for_progress = is_capturing.clone();
    std::thread::spawn(move || {
        let mut last = 0;
        while capturing_for_progress.load(Ordering::Relaxed) {
            std::thread::sleep(std::time::Duration::from_secs(1));
            let current = count_for_progress.load(Ordering::Relaxed);
            let delta = current - last;
            let elapsed_secs = current as f64 / f64::from(SAMPLE_RATE);
            print!("\r  Samples: {current}, Audio: {elapsed_secs:.1}s, Rate: {delta}/s   ");
            last = current;
        }
    });

    // Wait until either the duration elapses or we receive SIGTERM/SIGINT
    // (the app stops a meeting by sending SIGTERM, which finalizes the WAV).
    let term = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(signal_hook::consts::SIGTERM, term.clone())
        .context("register SIGTERM")?;
    signal_hook::flag::register(signal_hook::consts::SIGINT, term.clone())
        .context("register SIGINT")?;

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(duration_secs);
    while std::time::Instant::now() < deadline && !term.load(Ordering::Relaxed) {
        std::thread::sleep(std::time::Duration::from_millis(200));
    }

    // Stop capture
    is_capturing.store(false, Ordering::Relaxed);
    stream.stop_capture()?;
    println!();

    // Finalize WAV
    let total_samples = sample_count.load(Ordering::Relaxed);
    if let Ok(writer) = Arc::try_unwrap(wav_writer) {
        writer.into_inner()?.finalize()?;
    }

    let file_size = std::fs::metadata(&output_path)?.len();
    let audio_secs = total_samples as f64 / f64::from(SAMPLE_RATE);

    println!("\n=== Results ===");
    println!("  File: {}", output_path.display());
    println!("  Size: {} KB", file_size / 1024);
    println!("  Samples: {total_samples}");
    println!("  Duration: {audio_secs:.1}s");

    if total_samples == 0 {
        println!("\n  WARNING: No audio samples captured!");
        println!("  Possible causes:");
        println!("    - No audio playing from the target app");
        println!("    - Screen Recording permission not granted");
        println!("    - App uses a different audio subsystem");
        println!("  Try: cargo run -- --list  (to verify the app is visible)");
    } else {
        println!("\n  SUCCESS! Audio captured. Play with:");
        println!("    afplay {}", output_path.display());
    }

    Ok(())
}

fn chrono_timestamp() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("{secs}")
}
