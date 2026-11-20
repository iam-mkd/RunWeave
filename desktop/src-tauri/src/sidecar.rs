//! Starts and stops the RunWeave desktop service (the frozen Python program).
//!
//! The service announces `RUNWEAVE_DESKTOP_READY <url>` on stdout once the workspace is
//! served, reports a failure as a `RUNWEAVE_DESKTOP_ERROR` line on stderr, and shuts down
//! (agents, then PostgreSQL) when its stdin closes. Closing stdin is therefore how the
//! window stops it, and also what happens if the window process dies.

use std::collections::VecDeque;
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

pub const READY_PREFIX: &str = "RUNWEAVE_DESKTOP_READY ";
pub const ERROR_PREFIX: &str = "RUNWEAVE_DESKTOP_ERROR ";
const STDERR_LINES: usize = 30;

/// The workspace URL from a ready line: only `http://127.0.0.1:<port>` is accepted.
pub fn parse_ready(line: &str) -> Option<String> {
    let url = line.trim_end().strip_prefix(READY_PREFIX)?;
    let port = url.strip_prefix("http://127.0.0.1:")?;
    let number: u16 = port.parse().ok()?;
    (number != 0 && port == number.to_string()).then(|| url.to_string())
}

/// What the developer should read when the service did not start.
pub fn failure_text(stderr: &[String]) -> String {
    if let Some(line) = stderr
        .iter()
        .rev()
        .find_map(|line| line.strip_prefix(ERROR_PREFIX))
    {
        return line.replace(" | ", "\n");
    }
    let tail: Vec<&str> = stderr
        .iter()
        .map(String::as_str)
        .filter(|line| !line.trim().is_empty())
        .collect();
    if tail.is_empty() {
        "The RunWeave service stopped without saying why.".to_string()
    } else {
        tail[tail.len().saturating_sub(10)..].join("\n")
    }
}

pub struct Launch {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub env: Vec<(OsString, OsString)>,
}

pub struct Sidecar {
    child: Child,
    stdin: Option<ChildStdin>,
    stderr: Arc<Mutex<VecDeque<String>>>,
    pub url: String,
}

fn drain<R: Read + Send + 'static>(stream: R, keep: Arc<Mutex<VecDeque<String>>>) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let mut lines = keep.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            if lines.len() == STDERR_LINES {
                lines.pop_front();
            }
            lines.push_back(line.chars().take(500).collect());
        }
    });
}

impl Sidecar {
    /// Starts the service and waits until it is ready, it fails, or `timeout` passes.
    pub fn start(launch: &Launch, timeout: Duration) -> Result<Sidecar, String> {
        let mut command = Command::new(&launch.program);
        command
            .args(&launch.args)
            .envs(launch.env.iter().map(|(key, value)| (key, value)))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Cannot start {}: {error}", launch.program.display()))?;
        let stdin = child.stdin.take();
        let stderr = Arc::new(Mutex::new(VecDeque::new()));
        drain(child.stderr.take().expect("piped stderr"), stderr.clone());
        let stdout = child.stdout.take().expect("piped stdout");
        let (ready, announced) = mpsc::channel();
        thread::spawn(move || {
            let mut lines = BufReader::new(stdout).lines().map_while(Result::ok);
            for line in lines.by_ref() {
                if let Some(url) = parse_ready(&line) {
                    let _ = ready.send(url);
                    break;
                }
            }
            for _ in lines {} // keep reading so the service never blocks on a full pipe
        });
        let deadline = Instant::now() + timeout;
        loop {
            match announced.recv_timeout(Duration::from_millis(100)) {
                Ok(url) => {
                    return Ok(Sidecar {
                        child,
                        stdin,
                        stderr,
                        url,
                    })
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {}
            }
            let exited = matches!(child.try_wait(), Ok(Some(_)));
            if exited || Instant::now() >= deadline {
                let mut sidecar = Sidecar {
                    child,
                    stdin,
                    stderr,
                    url: String::new(),
                };
                sidecar.stop(Duration::from_secs(5));
                thread::sleep(Duration::from_millis(100)); // let stderr finish draining
                let lines: Vec<String> = sidecar
                    .stderr
                    .lock()
                    .map(|lines| lines.iter().cloned().collect())
                    .unwrap_or_default();
                let reason = if exited {
                    failure_text(&lines)
                } else {
                    format!(
                        "The RunWeave service did not start within {} seconds.\n{}",
                        timeout.as_secs(),
                        failure_text(&lines)
                    )
                };
                return Err(reason);
            }
        }
    }

    /// Closes stdin so the service shuts down cleanly; kills it after `grace`.
    /// Returns true when it stopped on its own.
    pub fn stop(&mut self, grace: Duration) -> bool {
        drop(self.stdin.take());
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            match self.child.try_wait() {
                Ok(Some(_)) => return true,
                Ok(None) => thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
        false
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            self.stop(Duration::from_secs(20));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn accepts_only_a_loopback_ready_line() {
        assert_eq!(
            parse_ready("RUNWEAVE_DESKTOP_READY http://127.0.0.1:51234\r\n"),
            Some("http://127.0.0.1:51234".into())
        );
        assert_eq!(
            parse_ready("RUNWEAVE_DESKTOP_READY http://127.0.0.1:0"),
            None
        );
        assert_eq!(
            parse_ready("RUNWEAVE_DESKTOP_READY http://127.0.0.1:080"),
            None
        );
        assert_eq!(
            parse_ready("RUNWEAVE_DESKTOP_READY http://example.com:80"),
            None
        );
        assert_eq!(
            parse_ready("RUNWEAVE_DESKTOP_READY http://127.0.0.1:80/evil"),
            None
        );
        assert_eq!(
            parse_ready("RUNWEAVE_DESKTOP_READY https://127.0.0.1:443"),
            None
        );
        assert_eq!(parse_ready("running 1 test"), None);
    }

    #[test]
    fn explains_a_failure_from_the_error_line_or_the_log_tail() {
        let lines = vec![
            "warning".to_string(),
            "RUNWEAVE_DESKTOP_ERROR initdb failed: | disk full".to_string(),
        ];
        assert_eq!(failure_text(&lines), "initdb failed:\ndisk full");
        assert_eq!(
            failure_text(&["Traceback".into(), "".into(), "boom".into()]),
            "Traceback\nboom"
        );
        assert!(failure_text(&[]).contains("without saying why"));
    }

    // The test binary doubles as a fake service: run with RUNWEAVE_FAKE_SIDECAR set, the
    // test below behaves like the real service in the requested way.
    #[test]
    fn fake_service() {
        let Ok(mode) = std::env::var("RUNWEAVE_FAKE_SIDECAR") else {
            return;
        };
        match mode.as_str() {
            "ready" | "stubborn" => {
                // libtest has already printed "test <name> ... " without a newline.
                println!(
                    "
RUNWEAVE_DESKTOP_READY http://127.0.0.1:43210"
                );
                std::io::stdout().flush().unwrap();
                let mut sink = Vec::new();
                let _ = std::io::stdin().read_to_end(&mut sink);
                if mode == "stubborn" {
                    thread::sleep(Duration::from_secs(60));
                }
                std::process::exit(0);
            }
            "fail" => {
                eprintln!("RUNWEAVE_DESKTOP_ERROR RunWeave is not installed in C:\\py.exe | Install it there");
                std::process::exit(1);
            }
            _ => thread::sleep(Duration::from_secs(60)), // "silent": never ready
        }
    }

    fn fake(mode: &str) -> Launch {
        Launch {
            program: std::env::current_exe().unwrap(),
            args: [
                "sidecar::tests::fake_service",
                "--exact",
                "--nocapture",
                "--test-threads=1",
            ]
            .map(OsString::from)
            .to_vec(),
            env: vec![("RUNWEAVE_FAKE_SIDECAR".into(), mode.into())],
        }
    }

    #[test]
    fn starts_reads_the_url_and_stops_by_closing_stdin() {
        let mut sidecar = Sidecar::start(&fake("ready"), Duration::from_secs(30)).unwrap();
        assert_eq!(sidecar.url, "http://127.0.0.1:43210");
        assert!(
            sidecar.stop(Duration::from_secs(10)),
            "stopped on its own when stdin closed"
        );
    }

    #[test]
    fn kills_a_service_that_does_not_stop() {
        let mut sidecar = Sidecar::start(&fake("stubborn"), Duration::from_secs(30)).unwrap();
        let started = Instant::now();
        assert!(!sidecar.stop(Duration::from_millis(500)));
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(matches!(sidecar.child.try_wait(), Ok(Some(_))));
    }

    #[test]
    fn reports_why_the_service_failed() {
        let error = Sidecar::start(&fake("fail"), Duration::from_secs(30))
            .err()
            .unwrap();
        assert!(error.contains("RunWeave is not installed"), "{error}");
        assert!(error.contains("\nInstall it there"), "{error}");
    }

    #[test]
    fn gives_up_on_a_service_that_never_gets_ready() {
        let started = Instant::now();
        let error = Sidecar::start(&fake("silent"), Duration::from_secs(2))
            .err()
            .unwrap();
        assert!(error.contains("did not start within 2 seconds"), "{error}");
        assert!(started.elapsed() < Duration::from_secs(15));
    }

    #[test]
    fn reports_a_missing_program() {
        let launch = Launch {
            program: PathBuf::from("no-such-runweave-service"),
            args: vec![],
            env: vec![],
        };
        assert!(Sidecar::start(&launch, Duration::from_secs(1))
            .err()
            .unwrap()
            .starts_with("Cannot start"));
    }
}
