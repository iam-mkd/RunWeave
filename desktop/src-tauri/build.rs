fn main() {
    // App commands must be granted explicitly (capabilities/default.json), so the remote
    // workspace page never gets them.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "current_choices",
            "choose_project",
            "choose_python",
            "start",
        ]),
    ))
    .expect("tauri build configuration");
}
