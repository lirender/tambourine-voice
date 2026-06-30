//! System-audio meeting capture via a bundled ScreenCaptureKit helper.
//!
//! The app spawns the `meeting_recorder` binary as a child process. Because the
//! child inherits the app's TCC identity, Screen Recording permission is
//! requested once against the Tambourine bundle ID (clean, unlike driving a CLI
//! through a terminal). The recorder writes a 16 kHz mono WAV and stops
//! gracefully on SIGTERM, finalizing the file.

use std::path::PathBuf;
use std::process::Child;
use std::sync::Mutex;

use serde::Serialize;
use tauri::{AppHandle, Manager};

/// Upper bound on a single capture (safety cap if a meeting is never stopped).
const MAX_CAPTURE_SECS: u64 = 4 * 60 * 60;

#[derive(Default)]
pub struct MeetingCapture {
    inner: Mutex<Option<Active>>,
}

struct Active {
    child: Child,
    output: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
pub struct CaptureStatus {
    pub recording: bool,
    pub output: Option<String>,
}

fn recorder_path(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(res) = app
        .path()
        .resolve("meeting_recorder", tauri::path::BaseDirectory::Resource)
    {
        if res.exists() {
            return Some(res);
        }
    }
    let dev = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("helpers")
        .join("meeting_recorder");
    dev.exists().then_some(dev)
}

fn meetings_dir(app: &AppHandle) -> PathBuf {
    let base = app
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir());
    let dir = base.join("meetings");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn epoch_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Begin capturing system audio. Returns the output WAV path.
#[tauri::command]
pub fn start_meeting_capture(app: AppHandle) -> Result<String, String> {
    let capture = app.state::<MeetingCapture>();
    let mut guard = capture.inner.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("A meeting capture is already in progress".into());
    }

    let bin = recorder_path(&app).ok_or("meeting_recorder binary not found")?;
    let output = meetings_dir(&app).join(format!("meeting-{}.wav", epoch_secs()));

    let child = std::process::Command::new(&bin)
        .arg("--duration")
        .arg(MAX_CAPTURE_SECS.to_string())
        .arg("--output")
        .arg(&output)
        .spawn()
        .map_err(|e| format!("Failed to start recorder: {e}"))?;

    log::info!("meeting capture started -> {}", output.display());
    *guard = Some(Active {
        child,
        output: output.clone(),
    });
    Ok(output.to_string_lossy().into_owned())
}

/// Stop the active capture (SIGTERM so the recorder finalizes the WAV).
/// Returns the finished WAV path.
#[tauri::command]
pub fn stop_meeting_capture(app: AppHandle) -> Result<String, String> {
    let capture = app.state::<MeetingCapture>();
    let mut guard = capture.inner.lock().map_err(|e| e.to_string())?;
    let Some(mut active) = guard.take() else {
        return Err("No meeting capture in progress".into());
    };

    // Send SIGTERM so the recorder stops the stream and finalizes the WAV.
    #[cfg(unix)]
    unsafe {
        libc::kill(active.child.id() as i32, libc::SIGTERM);
    }

    // Give it a moment to finalize, then reap.
    let _ = active.child.wait();
    log::info!("meeting capture stopped -> {}", active.output.display());
    Ok(active.output.to_string_lossy().into_owned())
}

/// Report whether a capture is currently running.
#[tauri::command]
pub fn meeting_capture_status(app: AppHandle) -> CaptureStatus {
    let capture = app.state::<MeetingCapture>();
    let guard = capture.inner.lock().ok();
    match guard.as_ref().and_then(|g| g.as_ref()) {
        Some(active) => CaptureStatus {
            recording: true,
            output: Some(active.output.to_string_lossy().into_owned()),
        },
        None => CaptureStatus {
            recording: false,
            output: None,
        },
    }
}
