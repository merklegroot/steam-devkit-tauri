use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Manager};

const API_HOST: &str = "127.0.0.1";
const API_PORT: u16 = 32100;

pub struct BackendState {
    child: Mutex<Option<Child>>,
}

impl BackendState {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
        }
    }
}

fn project_root() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .expect("src-tauri has a parent directory")
        .to_path_buf()
}

fn resolve_python(project_root: &PathBuf) -> PathBuf {
    let venv_python = project_root.join("python-client/.venv/bin/python");
    if venv_python.is_file() {
        return venv_python;
    }
    PathBuf::from("python3")
}

fn start_backend(app: &AppHandle) -> Result<(), String> {
    let root = project_root();
    let python = resolve_python(&root);
    let script = root.join("python-client/api_server.py");

    if !script.is_file() {
        return Err(format!("API server script not found: {}", script.display()));
    }

    let child = Command::new(&python)
        .arg(&script)
        .arg("--host")
        .arg(API_HOST)
        .arg("--port")
        .arg(API_PORT.to_string())
        .current_dir(root.join("python-client"))
        .env(
            "PYTHONPATH",
            root.join("python-client/client").display().to_string(),
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to start Python backend ({python:?}): {e}"))?;

    let state = app.state::<BackendState>();
    state
        .child
        .lock()
        .map_err(|e| e.to_string())?
        .replace(child);

    Ok(())
}

fn stop_backend(state: &BackendState) {
    if let Ok(mut guard) = state.child.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn wait_for_health() -> bool {
    let url = format!("http://{API_HOST}:{API_PORT}/api/health");
    for _ in 0..60 {
        if let Ok(response) = ureq::get(&url).call() {
            if response.status() == 200 {
                return true;
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

#[tauri::command]
fn api_base_url() -> String {
    format!("http://{API_HOST}:{API_PORT}")
}

#[tauri::command]
fn backend_ready() -> bool {
    wait_for_health()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(BackendState::new())
        .setup(|app| {
            start_backend(app.handle())?;
            if !wait_for_health() {
                eprintln!(
                    "Warning: Python API server did not respond on http://{API_HOST}:{API_PORT}"
                );
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![api_base_url, backend_ready])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(state) = window.app_handle().try_state::<BackendState>() {
                    stop_backend(&state);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<BackendState>() {
                    stop_backend(&state);
                }
            }
        });
}
