use tauri_plugin_shell::ShellExt;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Spawn the bundled Python server sidecar.
            // In dev mode the sidecar binary won't exist — that's fine,
            // the user runs `python server.py` separately instead.
            match app.shell().sidecar("server") {
                Ok(cmd) => { let _ = cmd.spawn(); }
                Err(_)  => { /* dev mode — no sidecar present */ }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application")
}
