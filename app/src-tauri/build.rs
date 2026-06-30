use std::path::Path;
use std::process::Command;

fn main() {
    // Compile the Swift EventKit calendar helper on macOS so the bundled
    // resource is always current. Failures here are non-fatal.
    #[cfg(target_os = "macos")]
    {
        let manifest = env!("CARGO_MANIFEST_DIR");
        let src = Path::new(manifest).join("helpers/calendar_helper.swift");
        let out = Path::new(manifest).join("helpers/calendar_helper");
        println!("cargo:rerun-if-changed=helpers/calendar_helper.swift");
        if src.exists() {
            match Command::new("swiftc")
                .args(["-O", src.to_str().unwrap(), "-o", out.to_str().unwrap()])
                .status()
            {
                Ok(s) if s.success() => {}
                Ok(s) => println!("cargo:warning=swiftc exited with {s} building calendar_helper"),
                Err(e) => println!("cargo:warning=failed to run swiftc for calendar_helper: {e}"),
            }
        }

        // The bundled `meeting_recorder` (ScreenCaptureKit) links the Swift
        // runtime via @rpath/libswift_Concurrency.dylib but ships without an
        // LC_RPATH, so it fails to launch as a child of the app. Ensure the
        // /usr/lib/swift rpath is present (idempotent; "would duplicate" is fine).
        let recorder = Path::new(manifest).join("helpers/meeting_recorder");
        println!("cargo:rerun-if-changed=helpers/meeting_recorder");
        if recorder.exists() {
            // Idempotent: only add the rpath if it isn't already present, so a
            // re-build doesn't fail with install_name_tool's "would duplicate".
            let has_rpath = Command::new("otool")
                .args(["-l", recorder.to_str().unwrap()])
                .output()
                .map(|o| String::from_utf8_lossy(&o.stdout).contains("/usr/lib/swift"))
                .unwrap_or(false);
            if !has_rpath {
                let _ = Command::new("install_name_tool")
                    .args(["-add_rpath", "/usr/lib/swift", recorder.to_str().unwrap()])
                    .status();
            }
        }
    }

    tauri_build::build();
}
