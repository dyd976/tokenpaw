use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    Emitter, Manager,
};
#[cfg(not(debug_assertions))]
use tauri_plugin_shell::ShellExt;

#[tauri::command]
fn show_pet_context_menu(
    app: tauri::AppHandle,
    language: String,
    window_label: String,
) -> Result<(), String> {
    let window = app
        .get_webview_window(&window_label)
        .ok_or_else(|| format!("window not found: {window_label}"))?;
    let english = language == "en";
    let open_label = if english { "Open token center" } else { "상세 관제 열기" };
    let search_label = if english { "Search" } else { "검색" };
    let alerts_label = if english { "Recent alerts" } else { "최근 알림" };
    let settings_label = if english { "Pet settings" } else { "펫 설정" };
    let scan_label = if english { "Scan logs now" } else { "지금 로그 스캔" };
    let quit_label = if english { "Quit" } else { "종료" };

    let open = MenuItem::with_id(&app, "pet_open", open_label, true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let search = MenuItem::with_id(&app, "pet_search", search_label, true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let alerts = MenuItem::with_id(&app, "pet_alerts", alerts_label, true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let settings = MenuItem::with_id(&app, "pet_settings", settings_label, true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let separator = PredefinedMenuItem::separator(&app).map_err(|error| error.to_string())?;
    let scan = MenuItem::with_id(&app, "pet_scan", scan_label, true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let quit = MenuItem::with_id(&app, "pet_quit", quit_label, true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let menu = Menu::with_items(&app, &[&open, &search, &alerts, &settings, &separator, &scan, &quit])
        .map_err(|error| error.to_string())?;

    window.popup_menu(&menu).map_err(|error| error.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![show_pet_context_menu])
        .setup(|app| {
            #[cfg(not(debug_assertions))]
            {
                let data_dir = app.path().app_data_dir()?;
                std::fs::create_dir_all(&data_dir)?;
                let db_path = data_dir.join("token-monitor.db");
                let db_path_string = db_path.to_string_lossy().to_string();
                let (mut events, _child) = app
                    .shell()
                    .sidecar("agent-token-monitor-sidecar")?
                    .env("TOKEN_MONITOR_PORT", "8765")
                    .env("TOKEN_MONITOR_AUTOSCAN", "1")
                    .env("TOKEN_MONITOR_POLLING_INTERVAL", "5")
                    .env("TOKEN_MONITOR_STORE_PROMPT_CONTENT", "1")
                    .env("TOKEN_MONITOR_REPROCESS_PROMPTS", "1")
                    .env("TOKEN_MONITOR_DB", db_path_string)
                    .spawn()?;
                tauri::async_runtime::spawn(async move {
                    while events.recv().await.is_some() {}
                });
            }

            let open = MenuItem::with_id(app, "open", "검색 열기", true, None::<&str>)?;
            let scan = MenuItem::with_id(app, "scan", "지금 스캔", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "종료", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &scan, &quit])?;

            TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("TokenPaw · AI Agent Token Observatory")
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "scan" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.emit("request-scan", ());
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            app.handle().on_menu_event(|app, event| match event.id().as_ref() {
                "pet_open" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.emit("open-panel-request", serde_json::json!({ "mode": "home" }));
                    }
                }
                "pet_search" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.emit("open-panel-request", serde_json::json!({ "mode": "search" }));
                    }
                }
                "pet_alerts" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.emit("open-panel-request", serde_json::json!({ "mode": "alerts" }));
                    }
                }
                "pet_settings" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.emit("open-panel-request", serde_json::json!({ "mode": "settings" }));
                    }
                }
                "pet_scan" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.emit("request-scan", ());
                    }
                }
                "pet_quit" => app.exit(0),
                _ => {}
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running desktop application");
}
