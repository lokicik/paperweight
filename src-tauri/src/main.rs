#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{path::PathBuf, sync::Mutex, time::Duration};
use tauri::{webview::DownloadEvent, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

struct EngineChild(Mutex<Option<CommandChild>>);

/// A PDF passed on the command line: double-click, "Open with", or a file
/// dropped onto the app icon.
fn launch_pdf() -> Option<PathBuf> {
    std::env::args_os().skip(1).map(PathBuf::from).find(|path| {
        path.is_file() && path.extension().is_some_and(|ext| ext.eq_ignore_ascii_case("pdf"))
    })
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let opening = launch_pdf();
            let mut args = vec!["--port".to_string(), "0".to_string(), "--managed".to_string()];
            if let Some(path) = &opening {
                args.push("--sample".into());
                args.push(path.to_string_lossy().into_owned());
            }
            let (mut events, child) = app.shell().sidecar("paperweight-engine")?.args(args).spawn()?;
            app.manage(EngineChild(Mutex::new(Some(child))));
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let ready = tokio::time::timeout(Duration::from_secs(45), async {
                    while let Some(event) = events.recv().await {
                        match event {
                            CommandEvent::Stdout(line) => {
                                if let Ok(value) = serde_json::from_slice::<serde_json::Value>(&line) {
                                    if value["event"] == "ready" {
                                        if let Some(url) = value["url"].as_str() {
                                            return url.parse::<tauri::Url>().ok();
                                        }
                                    }
                                }
                            }
                            CommandEvent::Terminated(_) | CommandEvent::Error(_) => return None,
                            _ => {}
                        }
                    }
                    None
                }).await;
                match ready {
                    Ok(Some(mut url)) if url.scheme() == "http" && url.host_str() == Some("127.0.0.1") => {
                        if opening.is_some() {
                            url.set_query(Some("sample=0"));
                        }
                        let window = WebviewWindowBuilder::new(&handle, "main", WebviewUrl::External(url))
                            .title("Paperweight")
                            .inner_size(1360.0, 900.0)
                            .min_inner_size(900.0, 600.0)
                            .maximized(true)
                            .disable_drag_drop_handler()
                            .on_download(|webview, event| match event {
                                // Saving a compressed copy asks where to put it, like any desktop app.
                                DownloadEvent::Requested { destination, .. } => {
                                    let name = destination
                                        .file_name()
                                        .map(|n| n.to_string_lossy().into_owned())
                                        .unwrap_or_else(|| "compressed.pdf".into());
                                    let window = webview.window();
                                    let (kind, ext) = if name.to_lowercase().ends_with(".zip") { ("ZIP archive", "zip") } else { ("PDF document", "pdf") };
                                    match rfd::FileDialog::new()
                                        .set_title("Save smaller copy")
                                        .set_file_name(name)
                                        .add_filter(kind, &[ext])
                                        .set_parent(&window)
                                        .save_file()
                                    {
                                        Some(path) => {
                                            *destination = path;
                                            true
                                        }
                                        None => false,
                                    }
                                }
                                _ => true,
                            })
                            .build();
                        if window.is_err() {
                            handle.dialog().message("Could not open the application window.")
                                .title("Paperweight").kind(MessageDialogKind::Error).blocking_show();
                            handle.exit(1);
                        }
                        // Keep draining stdout so the engine cannot block on a full pipe.
                        while let Some(event) = events.recv().await {
                            if matches!(event, CommandEvent::Terminated(_)) {
                                if handle.get_webview_window("main").is_some() {
                                    handle.dialog().message("The PDF engine stopped. Close and reopen Paperweight to continue.")
                                        .title("Paperweight").kind(MessageDialogKind::Error).blocking_show();
                                }
                                break;
                            }
                        }
                    }
                    _ => {
                        handle.dialog().message("The PDF engine could not start. Reopen Paperweight or check the installation.")
                            .title("Paperweight").kind(MessageDialogKind::Error).blocking_show();
                        handle.exit(1);
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Could not initialize Paperweight");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<EngineChild>().0.lock().unwrap().take() {
                // The engine cancels analyses, closes workers and removes its temporary copies.
                let _ = child.write(b"shutdown\n");
            }
        }
    });
}
