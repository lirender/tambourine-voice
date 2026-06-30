//! Calendar integration via a bundled Swift EventKit helper.
//!
//! The helper (`calendar_helper`) reads the macOS Calendar (which aggregates
//! Google / iCloud / Exchange accounts) and prints upcoming meetings as JSON.
//! A background poller fires `meeting-prep` ~5 minutes before each meeting and
//! `meeting-ended` when a meeting's end time passes, so the frontend can start
//! recording / prompt to end the session.

use std::collections::HashSet;
use std::process::Command;
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};

use crate::events::EventName;

/// How long before a meeting's start we fire the prep event.
const PREP_LEAD_SECS: f64 = 5.0 * 60.0;
/// Poll interval for the calendar watcher.
const POLL_INTERVAL: Duration = Duration::from_secs(60);
/// Look-ahead window passed to the helper.
const LOOKAHEAD_HOURS: u32 = 12;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Attendee {
    pub name: String,
    pub email: Option<String>,
    #[serde(rename = "isOrganizer", default)]
    pub is_organizer: bool,
    #[serde(rename = "isCurrentUser", default)]
    pub is_current_user: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Meeting {
    pub id: String,
    pub title: String,
    pub start: String,
    pub end: String,
    #[serde(rename = "startEpoch")]
    pub start_epoch: f64,
    #[serde(rename = "endEpoch")]
    pub end_epoch: f64,
    pub location: Option<String>,
    pub notes: Option<String>,
    pub organizer: Option<String>,
    #[serde(default)]
    pub attendees: Vec<Attendee>,
}

#[derive(Debug, Clone, Deserialize)]
struct HelperOutput {
    authorized: bool,
    #[serde(default)]
    meetings: Vec<Meeting>,
    #[serde(default)]
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct UpcomingMeetings {
    pub authorized: bool,
    pub meetings: Vec<Meeting>,
    pub error: Option<String>,
}

/// Tracks which meetings we've already fired events for (avoid duplicates).
#[derive(Default)]
pub struct CalendarState {
    prepped: Mutex<HashSet<String>>,
    ended: Mutex<HashSet<String>>,
    /// False until the first poll has seeded already-past meetings, so we never
    /// retroactively notify for meetings that ended while the app was closed.
    seeded: std::sync::atomic::AtomicBool,
}

/// Resolve the path to the bundled helper binary.
///
/// In a bundled `.app`, the helper is a resource; in `tauri dev` we fall back
/// to the source tree location.
fn helper_path(app: &AppHandle) -> Option<std::path::PathBuf> {
    if let Ok(resource) = app
        .path()
        .resolve("calendar_helper", tauri::path::BaseDirectory::Resource)
    {
        if resource.exists() {
            return Some(resource);
        }
    }
    // Dev fallback: src-tauri/helpers/calendar_helper relative to CARGO_MANIFEST_DIR.
    let dev = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("helpers")
        .join("calendar_helper");
    if dev.exists() {
        return Some(dev);
    }
    None
}

fn run_helper(app: &AppHandle, extra_arg: Option<&str>) -> Result<HelperOutput, String> {
    let path = helper_path(app).ok_or_else(|| "calendar_helper binary not found".to_string())?;
    let mut cmd = Command::new(&path);
    cmd.arg("--hours").arg(LOOKAHEAD_HOURS.to_string());
    if let Some(a) = extra_arg {
        cmd.arg(a);
    }
    let output = cmd
        .output()
        .map_err(|e| format!("Failed to run calendar helper: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str::<HelperOutput>(stdout.trim())
        .map_err(|e| format!("Failed to parse calendar helper output: {e}; raw: {stdout}"))
}

/// Tauri command: fetch upcoming meetings (triggers TCC permission prompt on first call).
#[tauri::command]
pub fn get_upcoming_meetings(app: AppHandle) -> Result<UpcomingMeetings, String> {
    let out = run_helper(&app, None)?;
    Ok(UpcomingMeetings {
        authorized: out.authorized,
        meetings: out.meetings,
        error: out.error,
    })
}

/// Tauri command: explicitly request calendar access (shows the prompt).
#[tauri::command]
pub fn request_calendar_access(app: AppHandle) -> Result<bool, String> {
    let out = run_helper(&app, Some("--request"))?;
    Ok(out.authorized)
}

#[derive(Debug, Clone, Serialize)]
pub struct MeetingEventPayload {
    pub meeting: Meeting,
    /// Seconds until the meeting starts (negative if already started).
    pub seconds_until_start: f64,
}

/// Spawn the background calendar watcher thread.
pub fn start_watcher(app: AppHandle) {
    app.manage(CalendarState::default());

    std::thread::spawn(move || loop {
        if let Err(e) = poll_once(&app) {
            log::debug!("calendar poll: {e}");
        }
        std::thread::sleep(POLL_INTERVAL);
    });
}

fn now_epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn poll_once(app: &AppHandle) -> Result<(), String> {
    let out = run_helper(app, None)?;
    if !out.authorized {
        return Ok(());
    }
    let state = app.state::<CalendarState>();
    let now = now_epoch();

    // First poll after launch: mark every meeting that has already entered its
    // prep window or already ended as "handled" so we don't fire a flood of
    // retroactive notifications for meetings that happened while the app was off.
    if !state.seeded.swap(true, std::sync::atomic::Ordering::SeqCst) {
        let mut prepped = state.prepped.lock().unwrap();
        let mut ended = state.ended.lock().unwrap();
        for m in &out.meetings {
            if m.start_epoch - now <= PREP_LEAD_SECS {
                prepped.insert(m.id.clone());
            }
            if now >= m.end_epoch {
                ended.insert(m.id.clone());
            }
        }
        log::info!("calendar watcher seeded; {} meetings marked handled", out.meetings.len());
        return Ok(());
    }

    for m in out.meetings {
        let until_start = m.start_epoch - now;

        // Pre-meeting prep: within the lead window and not yet started.
        if until_start <= PREP_LEAD_SECS && until_start > -30.0 {
            let mut prepped = state.prepped.lock().unwrap();
            if !prepped.contains(&m.id) {
                prepped.insert(m.id.clone());
                drop(prepped);
                log::info!("meeting-prep: {} ({}s to start)", m.title, until_start as i64);
                let _ = app.emit(
                    EventName::MeetingPrep.as_str(),
                    MeetingEventPayload {
                        meeting: m.clone(),
                        seconds_until_start: until_start,
                    },
                );
            }
        }

        // Post-meeting: end time has just passed.
        if now >= m.end_epoch && now - m.end_epoch < 300.0 {
            let mut ended = state.ended.lock().unwrap();
            if !ended.contains(&m.id) {
                ended.insert(m.id.clone());
                drop(ended);
                log::info!("meeting-ended: {}", m.title);
                let _ = app.emit(
                    EventName::MeetingEnded.as_str(),
                    MeetingEventPayload {
                        meeting: m.clone(),
                        seconds_until_start: until_start,
                    },
                );
            }
        }
    }
    Ok(())
}
