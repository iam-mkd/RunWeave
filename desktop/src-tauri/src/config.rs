//! The project folder and Python interpreter the developer chose, kept in the app's
//! config folder between launches.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

pub const FILE_NAME: &str = "desktop.json";
pub const PROJECT_FILE: &str = "runweave.toml";

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppConfig {
    /// The project's runweave.toml. Without one the workspace runs only its own agents.
    pub project: Option<PathBuf>,
    /// The interpreter that runs a Python project's agents (with runweave installed).
    pub python: Option<PathBuf>,
}

impl AppConfig {
    /// Reads the saved choices; a missing or unreadable file means none were made yet.
    pub fn load(folder: &Path) -> AppConfig {
        fs::read_to_string(folder.join(FILE_NAME))
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, folder: &Path) -> Result<(), String> {
        fs::create_dir_all(folder)
            .map_err(|error| format!("Cannot create {}: {error}", folder.display()))?;
        let text = serde_json::to_string_pretty(self).map_err(|error| error.to_string())?;
        let path = folder.join(FILE_NAME);
        let staging = folder.join(format!("{FILE_NAME}.tmp"));
        fs::write(&staging, text)
            .map_err(|error| format!("Cannot write {}: {error}", staging.display()))?;
        fs::rename(&staging, &path)
            .map_err(|error| format!("Cannot write {}: {error}", path.display()))
    }

    /// Checks the choices before the service is started with them.
    pub fn validate(&self) -> Result<(), String> {
        if let Some(project) = &self.project {
            if project.file_name().and_then(|name| name.to_str()) != Some(PROJECT_FILE)
                || !project.is_file()
            {
                return Err(format!(
                    "{} is not a {PROJECT_FILE} file",
                    project.display()
                ));
            }
        }
        if let Some(python) = &self.python {
            if !python.is_file() {
                return Err(format!(
                    "Python interpreter not found: {}",
                    python.display()
                ));
            }
        }
        Ok(())
    }
}

/// The project file inside a folder the developer picked, if there is one.
pub fn project_in(folder: &Path) -> Result<PathBuf, String> {
    let file = folder.join(PROJECT_FILE);
    if file.is_file() {
        Ok(file)
    } else {
        Err(format!(
            "No {PROJECT_FILE} in {}. Create a project there with `runweave init`.",
            folder.display()
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let folder =
            std::env::temp_dir().join(format!("runweave-config-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&folder);
        fs::create_dir_all(&folder).unwrap();
        folder
    }

    #[test]
    fn saves_and_loads_the_choices() {
        let folder = scratch("roundtrip");
        assert_eq!(AppConfig::load(&folder), AppConfig::default());
        let chosen = AppConfig {
            project: Some(folder.join(PROJECT_FILE)),
            python: Some(folder.join("python")),
        };
        chosen.save(&folder).unwrap();
        assert_eq!(AppConfig::load(&folder), chosen);
        assert!(!folder.join(format!("{FILE_NAME}.tmp")).exists());
    }

    #[test]
    fn a_damaged_file_is_ignored() {
        let folder = scratch("damaged");
        fs::write(folder.join(FILE_NAME), "{\"project\": 7, \"extra\": true}").unwrap();
        assert_eq!(AppConfig::load(&folder), AppConfig::default());
    }

    #[test]
    fn validates_the_project_file_and_interpreter() {
        let folder = scratch("validate");
        assert!(AppConfig::default().validate().is_ok());
        let missing = AppConfig {
            project: Some(folder.join(PROJECT_FILE)),
            python: None,
        };
        assert!(missing
            .validate()
            .unwrap_err()
            .contains("is not a runweave.toml"));
        fs::write(folder.join("other.toml"), "").unwrap();
        let wrong = AppConfig {
            project: Some(folder.join("other.toml")),
            python: None,
        };
        assert!(wrong.validate().is_err());
        fs::write(folder.join(PROJECT_FILE), "").unwrap();
        let python = AppConfig {
            project: Some(folder.join(PROJECT_FILE)),
            python: Some(folder.join("nope")),
        };
        assert!(python
            .validate()
            .unwrap_err()
            .contains("interpreter not found"));
        fs::write(folder.join("python"), "").unwrap();
        let good = AppConfig {
            project: Some(folder.join(PROJECT_FILE)),
            python: Some(folder.join("python")),
        };
        assert!(good.validate().is_ok());
    }

    #[test]
    fn finds_the_project_file_in_a_folder() {
        let folder = scratch("find");
        assert!(project_in(&folder).unwrap_err().contains("runweave init"));
        fs::write(folder.join(PROJECT_FILE), "").unwrap();
        assert_eq!(project_in(&folder).unwrap(), folder.join(PROJECT_FILE));
    }
}
