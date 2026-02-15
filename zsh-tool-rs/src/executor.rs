use std::io::{self, Read, Write};
use std::os::fd::FromRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::thread;
use std::time::Instant;

use crate::meta::ExecResult;

/// Wrap command to send pipestatus to fd 3.
fn wrap_command(command: &str) -> String {
    format!("{}; echo \"${{pipestatus[*]}}\" >&3", command)
}

/// Parse pipestatus string "1 0 0" into Vec<i32>.
fn parse_pipestatus(raw: &str) -> Vec<i32> {
    raw.split_whitespace()
        .filter_map(|s| s.parse::<i32>().ok())
        .collect()
}

pub fn execute_pipe(command: &str, timeout_secs: u64) -> Result<ExecResult, String> {
    let start = Instant::now();

    // Create metadata pipe (fd 3 sideband)
    // Use libc::pipe() directly — nix::unistd::pipe() sets O_CLOEXEC which
    // could interfere with fd inheritance across exec.
    let (meta_read_raw, meta_write_raw) = {
        let mut fds = [0i32; 2];
        if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
            return Err("pipe() failed".into());
        }
        (fds[0], fds[1])
    };

    let wrapped = wrap_command(command);

    // Spawn zsh with fd 3 mapped to metadata pipe
    let mut child = unsafe {
        Command::new("/bin/zsh")
            .args(["-c", &wrapped])
            .stdout(Stdio::piped())
            .stdin(Stdio::piped())
            .stderr(Stdio::null()) // We merge via dup2 in pre_exec
            .pre_exec(move || {
                // New process group so we can kill all children on timeout
                libc::setpgid(0, 0);
                // Merge stderr into stdout
                libc::dup2(1, 2);
                // Close read end first — it's not needed in child, and if
                // it landed on fd 3 (likely, since 0-2 are taken), closing
                // it after dup2 would destroy the fd we just set up.
                libc::close(meta_read_raw);
                // Set up fd 3 for metadata sideband
                libc::dup2(meta_write_raw, 3);
                if meta_write_raw != 3 {
                    libc::close(meta_write_raw);
                }
                Ok(())
            })
            .spawn()
            .map_err(|e| format!("spawn: {}", e))?
    };

    // Close write end of metadata pipe in parent
    unsafe { libc::close(meta_write_raw); }

    // Take ownership of child stdout for streaming
    let child_stdout = child.stdout.take()
        .ok_or("no stdout")?;

    // Stream child stdout -> our stdout (in a thread to avoid blocking)
    let stdout_handle = thread::spawn(move || {
        let mut reader = child_stdout;
        let mut stdout = io::stdout().lock();
        let mut buf = [0u8; 4096];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    let _ = stdout.write_all(&buf[..n]);
                    let _ = stdout.flush();
                }
                Err(_) => break,
            }
        }
    });

    // Forward our stdin -> child stdin (for interactive input)
    let child_stdin = child.stdin.take();
    let _stdin_handle = child_stdin.map(|mut child_in| {
        thread::spawn(move || {
            let stdin = io::stdin();
            let mut buf = [0u8; 4096];
            loop {
                match stdin.lock().read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        if child_in.write_all(&buf[..n]).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        })
    });

    // Wait for child with timeout
    let timed_out;
    let exit_code;

    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                timed_out = false;
                exit_code = status.code().unwrap_or(-1);
                break;
            }
            Ok(None) => {
                if start.elapsed().as_secs() >= timeout_secs {
                    // Kill entire process group (child + its subprocesses)
                    let pid = child.id() as i32;
                    unsafe { libc::kill(-pid, libc::SIGKILL); }
                    let _ = child.wait();
                    timed_out = true;
                    exit_code = -1;
                    break;
                }
                thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => {
                return Err(format!("wait: {}", e));
            }
        }
    }

    // Wait for stdout thread to finish draining
    let _ = stdout_handle.join();

    // Read metadata from fd 3 pipe
    let mut meta_raw = String::new();
    unsafe {
        let mut meta_file = std::fs::File::from_raw_fd(meta_read_raw);
        let _ = meta_file.read_to_string(&mut meta_raw);
        // File dropped here, closes the fd
    }

    let mut pipestatus = parse_pipestatus(&meta_raw);
    // If pipestatus is empty (e.g. `exit N` killed the shell before the
    // echo >&3 ran), synthesize it from the process exit code.
    if pipestatus.is_empty() {
        pipestatus.push(exit_code);
    }
    let final_exit = *pipestatus.last().unwrap();

    let elapsed_ms = start.elapsed().as_millis() as u64;

    Ok(ExecResult {
        pipestatus,
        exit_code: final_exit,
        elapsed_ms,
        timed_out,
    })
}

pub fn execute_pty(command: &str, timeout_secs: u64) -> Result<ExecResult, String> {
    use nix::pty::{openpty, OpenptyResult};
    use nix::sys::signal::{kill, Signal};
    use nix::sys::wait::{waitpid, WaitPidFlag, WaitStatus};
    use nix::unistd::{execvp, fork, ForkResult};
    use std::ffi::CString;
    use std::os::fd::IntoRawFd;

    let start = Instant::now();

    // Create metadata pipe (fd 3 sideband) — libc::pipe to avoid O_CLOEXEC
    let (meta_read_raw, meta_write_raw) = {
        let mut fds = [0i32; 2];
        if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
            return Err("pipe() failed".into());
        }
        (fds[0], fds[1])
    };

    // Open PTY pair
    let OpenptyResult { master, slave } = openpty(None, None)
        .map_err(|e| format!("openpty: {}", e))?;
    let master_raw = master.into_raw_fd();
    let slave_raw = slave.into_raw_fd();

    let wrapped = wrap_command(command);

    // Fork manually (can't use Command for PTY — need direct control)
    let fork_result = unsafe { fork() }
        .map_err(|e| format!("fork: {}", e))?;

    match fork_result {
        ForkResult::Child => {
            // New session (becomes session leader, detaches controlling terminal)
            let _ = nix::unistd::setsid();

            // Set slave PTY as controlling terminal via TIOCSCTTY
            unsafe { libc::ioctl(slave_raw, libc::TIOCSCTTY, 0); }

            // Close master side of PTY
            unsafe { libc::close(master_raw); }

            // Set up slave as stdin/stdout/stderr
            unsafe {
                libc::dup2(slave_raw, 0);
                libc::dup2(slave_raw, 1);
                libc::dup2(slave_raw, 2);
                if slave_raw > 2 {
                    libc::close(slave_raw);
                }
            }

            // Close read end of meta pipe first (fd collision prevention)
            unsafe { libc::close(meta_read_raw); }
            // Set up fd 3 for metadata sideband
            unsafe {
                libc::dup2(meta_write_raw, 3);
                if meta_write_raw != 3 {
                    libc::close(meta_write_raw);
                }
            }

            // Exec zsh
            let zsh = CString::new("/bin/zsh").unwrap();
            let c_flag = CString::new("-c").unwrap();
            let c_cmd = CString::new(wrapped.as_str()).unwrap();
            let _ = execvp(&zsh, &[&zsh, &c_flag, &c_cmd]);
            // If exec fails
            unsafe { libc::_exit(127); }
        }
        ForkResult::Parent { child } => {
            // Close slave side of PTY and write end of metadata pipe
            unsafe {
                libc::close(slave_raw);
                libc::close(meta_write_raw);
            }

            // Read from PTY master → our stdout (in a thread)
            let master_read_fd = master_raw;
            let stdout_handle = thread::spawn(move || {
                let mut stdout = io::stdout().lock();
                let mut buf = [0u8; 4096];
                loop {
                    let n = unsafe {
                        libc::read(master_read_fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len())
                    };
                    if n <= 0 { break; }
                    let _ = stdout.write_all(&buf[..n as usize]);
                    let _ = stdout.flush();
                }
            });

            // Wait for child with timeout
            let timed_out;
            let raw_exit_code;
            loop {
                match waitpid(child, Some(WaitPidFlag::WNOHANG)) {
                    Ok(WaitStatus::Exited(_, code)) => {
                        raw_exit_code = code;
                        timed_out = false;
                        break;
                    }
                    Ok(WaitStatus::Signaled(_, sig, _)) => {
                        raw_exit_code = 128 + sig as i32;
                        timed_out = false;
                        break;
                    }
                    Ok(WaitStatus::StillAlive) => {
                        if start.elapsed().as_secs() >= timeout_secs {
                            // Kill entire session (child is session leader via setsid)
                            let _ = kill(child, Signal::SIGKILL);
                            let _ = waitpid(child, None);
                            timed_out = true;
                            raw_exit_code = -1;
                            break;
                        }
                        thread::sleep(std::time::Duration::from_millis(50));
                    }
                    _ => {
                        raw_exit_code = -1;
                        timed_out = false;
                        break;
                    }
                }
            }

            // Close master PTY to signal EOF to stdout reader thread
            unsafe { libc::close(master_raw); }
            let _ = stdout_handle.join();

            // Read metadata from fd 3 pipe
            let mut meta_raw = String::new();
            unsafe {
                let mut meta_file = std::fs::File::from_raw_fd(meta_read_raw);
                let _ = meta_file.read_to_string(&mut meta_raw);
            }

            let mut pipestatus = parse_pipestatus(&meta_raw);
            if pipestatus.is_empty() {
                pipestatus.push(raw_exit_code);
            }
            let final_exit = *pipestatus.last().unwrap();

            Ok(ExecResult {
                pipestatus,
                exit_code: final_exit,
                elapsed_ms: start.elapsed().as_millis() as u64,
                timed_out,
            })
        }
    }
}
