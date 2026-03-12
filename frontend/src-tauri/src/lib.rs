#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|_app| {
            // In production: spawn the bundled server sidecar.
            // Build step: copy dist/server.exe to
            //   src-tauri/binaries/server-x86_64-pc-windows-msvc.exe
            // and re-enable externalBin + tauri-plugin-shell before `tauri build`.
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application")
}
