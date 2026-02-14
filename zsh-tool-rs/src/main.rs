use std::env;
use std::process;

mod executor;
mod meta;

fn print_usage() {
    eprintln!("Usage: zsh-tool-exec --meta <path> [--timeout <secs>] [--pty] -- <command>");
    process::exit(2);
}

struct Args {
    meta_path: String,
    timeout_secs: u64,
    pty: bool,
    command: String,
}

fn parse_args() -> Args {
    let args: Vec<String> = env::args().skip(1).collect();
    let mut meta_path = String::new();
    let mut timeout_secs: u64 = 120;
    let mut pty = false;
    let mut command = String::new();
    let mut i = 0;
    let mut after_dashdash = false;

    while i < args.len() {
        if after_dashdash {
            command = args[i..].join(" ");
            break;
        }
        match args[i].as_str() {
            "--meta" => {
                i += 1;
                meta_path = args.get(i).cloned().unwrap_or_else(|| {
                    print_usage();
                    unreachable!()
                });
            }
            "--timeout" => {
                i += 1;
                timeout_secs = args.get(i).and_then(|s| s.parse().ok()).unwrap_or(120);
            }
            "--pty" => pty = true,
            "--" => after_dashdash = true,
            _ => {
                // If no --, treat remaining as command
                command = args[i..].join(" ");
                break;
            }
        }
        i += 1;
    }

    if meta_path.is_empty() || command.is_empty() {
        print_usage();
    }

    Args {
        meta_path,
        timeout_secs,
        pty,
        command,
    }
}

fn main() {
    let args = parse_args();

    let result = if args.pty {
        executor::execute_pty(&args.command, args.timeout_secs)
    } else {
        executor::execute_pipe(&args.command, args.timeout_secs)
    };

    match result {
        Ok(exec_result) => {
            if let Err(e) = meta::write_meta(&args.meta_path, &exec_result) {
                eprintln!("zsh-tool-exec: failed to write meta: {}", e);
            }
            process::exit(exec_result.exit_code);
        }
        Err(e) => {
            let err_result = meta::ExecResult {
                pipestatus: vec![],
                exit_code: 127,
                elapsed_ms: 0,
                timed_out: false,
            };
            let _ = meta::write_meta(&args.meta_path, &err_result);
            eprintln!("zsh-tool-exec: {}", e);
            process::exit(127);
        }
    }
}
