//! The RunWeave desktop window.
//!
//! It shows a small local setup page (choose a project and a Python interpreter), starts
//! the RunWeave service with those choices, then shows the workspace the service serves
//! on 127.0.0.1. The workspace page gets no access to the app's commands: they are granted
//! only to the bundled setup page (capabilities/default.json), and navigation is limited to
//! the setup page and the service's own origin. Quitting stops the service, which stops its
//! agents and PostgreSQL.

mod config;
mod sidecar;

use config::AppConfig;
use serde::Serialize;
use sidecar::{Launch, Sidecar};
use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;

const START_TIMEOUT: Duration = Duration::from_secs(180);
const STOP_GRACE: Duration = Duration::from_secs(20);
#[cfg(windows)]
const SERVICE: &str = "runweave-desktop-service.exe";
#[cfg(not(windows))]
const SERVICE: &str = "runweave-desktop-service";

#[derive(Default)]
struct AppState {
    sidecar: Mutex<Option<Sidecar>>,
    /// The service's origin once it is ready; the only remote page the window may show.
    workspace: Mutex<Option<String>>,
}

#[derive(Serialize)]
struct Choices {
    project: Option<String>,
    python: Option<String>,
    valid: bool,
}

fn text(path: &Option<PathBuf>) -> Option<String> {
    path.as_ref().map(|path| path.display().to_string())
}

fn config_folder(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_config_dir()
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn current_choices(app: AppHandle) -> Result<Choices, String> {
    let saved = AppConfig::load(&config_folder(&app)?);
    Ok(Choices {
        project: text(&saved.project),
        python: text(&saved.python),
        valid: saved.project.is_some() && saved.validate().is_ok(),
    })
}

#[tauri::command]
async fn choose_project(app: AppHandle) -> Result<Option<String>, String> {
    let Some(folder) = app
        .dialog()
        .file()
        .set_title("Choose your RunWeave project folder")
        .blocking_pick_folder()
    else {
        return Ok(None);
    };
    let folder = folder.into_path().map_err(|error| error.to_string())?;
    config::project_in(&folder).map(|file| Some(file.display().to_string()))
}

#[tauri::command]
async fn choose_python(app: AppHandle) -> Result<Option<String>, String> {
    let picked = app
        .dialog()
        .file()
        .set_title("Choose the Python interpreter that runs your agents")
        .blocking_pick_file();
    match picked {
        None => Ok(None),
        Some(file) => file
            .into_path()
            .map(|path| Some(path.display().to_string()))
            .map_err(|error| error.to_string()),
    }
}

fn launch(app: &AppHandle, choices: &AppConfig) -> Result<Launch, String> {
    let resources = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?;
    let state = app
        .path()
        .app_local_data_dir()
        .map_err(|error| error.to_string())?;
    let mut args: Vec<OsString> = vec![
        "--state-dir".into(),
        state.into_os_string(),
        "--postgres-archive".into(),
        resources
            .join("postgres")
            .join("postgres.txz")
            .into_os_string(),
    ];
    if let Some(project) = &choices.project {
        args.extend(["--project".into(), project.clone().into_os_string()]);
    }
    if let Some(python) = &choices.python {
        args.extend(["--python".into(), python.clone().into_os_string()]);
    }
    Ok(Launch {
        program: resources.join("sidecar").join(SERVICE),
        args,
        env: vec![],
    })
}

/// Saves the choices, starts the service and shows its workspace.
#[tauri::command]
async fn start(
    app: AppHandle,
    project: Option<String>,
    python: Option<String>,
) -> Result<(), String> {
    let choices = AppConfig {
        project: project.filter(|value| !value.is_empty()).map(PathBuf::from),
        python: python.filter(|value| !value.is_empty()).map(PathBuf::from),
    };
    choices.validate()?;
    choices.save(&config_folder(&app)?)?;
    let state = app.state::<AppState>();
    if let Some(mut running) = state.sidecar.lock().map_err(|_| "busy")?.take() {
        running.stop(STOP_GRACE);
    }
    let plan = launch(&app, &choices)?;
    let started =
        tauri::async_runtime::spawn_blocking(move || Sidecar::start(&plan, START_TIMEOUT))
            .await
            .map_err(|error| error.to_string())??;
    let url: Url = started
        .url
        .parse()
        .map_err(|_| "The service reported an invalid address")?;
    *state.workspace.lock().map_err(|_| "busy")? = Some(started.url.clone());
    *state.sidecar.lock().map_err(|_| "busy")? = Some(started);
    let window = app.get_webview_window("main").ok_or("The window is gone")?;
    window.navigate(url).map_err(|error| error.to_string())
}

/// The bundled setup page, or the service's workspace once it is ready.
fn allowed(app: &AppHandle, url: &Url) -> bool {
    if url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost") {
        return true;
    }
    let workspace = app
        .state::<AppState>()
        .workspace
        .lock()
        .ok()
        .and_then(|origin| origin.clone());
    match workspace.and_then(|origin| origin.parse::<Url>().ok()) {
        Some(origin) => {
            url.scheme() == origin.scheme()
                && url.host_str() == origin.host_str()
                && url.port() == origin.port()
        }
        None => false,
    }
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            current_choices,
            choose_project,
            choose_python,
            start
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("RunWeave")
                .inner_size(1360.0, 900.0)
                .min_inner_size(900.0, 600.0)
                .on_navigation(move |url| allowed(&handle, url))
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the RunWeave window could not start");
    app.run(|app, event| {
        if let RunEvent::Exit = event {
            let state = app.state::<AppState>();
            let running = state
                .sidecar
                .lock()
                .ok()
                .and_then(|mut sidecar| sidecar.take());
            if let Some(mut sidecar) = running {
                sidecar.stop(STOP_GRACE);
            }
        }
    });
}
