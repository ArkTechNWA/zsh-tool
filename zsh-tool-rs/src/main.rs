use std::env;
use std::process;

use zsh_tool_exec::alan;
use zsh_tool_exec::executor;
use zsh_tool_exec::meta;
use zsh_tool_exec::serve;

fn print_usage() {
    eprintln!("Usage:");
    eprintln!("  zsh-tool serve                          — MCP server over stdio");
    eprintln!("  zsh-tool exec --meta <path> [--timeout <secs>] [--pty] [--db <path> --session-id <id>] -- <command>");
    process::exit(2);
}

struct ExecArgs {
    meta_path: String,
    timeout_secs: u64,
    pty: bool,
    command: String,
    db_path: Option<String>,
    session_id: Option<String>,
}

fn parse_exec_args(args: &[String]) -> ExecArgs {
    let mut meta_path = String::new();
    let mut timeout_secs: u64 = 120;
    let mut pty = false;
    let mut command = String::new();
    let mut db_path: Option<String> = None;
    let mut session_id: Option<String> = None;
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
            "--db" => {
                i += 1;
                db_path = Some(args.get(i).cloned().unwrap_or_else(|| {
                    print_usage();
                    unreachable!()
                }));
            }
            "--session-id" => {
                i += 1;
                session_id = Some(args.get(i).cloned().unwrap_or_else(|| {
                    print_usage();
                    unreachable!()
                }));
            }
            "--pty" => pty = true,
            "--" => after_dashdash = true,
            _ => {
                command = args[i..].join(" ");
                break;
            }
        }
        i += 1;
    }

    if meta_path.is_empty() || command.is_empty() {
        print_usage();
    }

    ExecArgs {
        meta_path,
        timeout_secs,
        pty,
        command,
        db_path,
        session_id,
    }
}

fn run_exec(args: ExecArgs) {
    let result = if args.pty {
        executor::execute_pty(&args.command, args.timeout_secs)
    } else {
        executor::execute_pipe(&args.command, args.timeout_secs)
    };

    match result {
        Ok(exec_result) => {
            if let Err(e) = meta::write_meta(&args.meta_path, &exec_result) {
                eprintln!("zsh-tool exec: failed to write meta: {}", e);
            }

            // ALAN recording (if --db provided)
            if let (Some(ref db_path), Some(ref session_id)) =
                (&args.db_path, &args.session_id)
            {
                match alan::open_db(db_path) {
                    Ok(conn) => {
                        if let Err(e) = alan::record(
                            &conn,
                            session_id,
                            &args.command,
                            exec_result.exit_code,
                            exec_result.elapsed_ms,
                            exec_result.timed_out,
                            "",
                            &exec_result.pipestatus,
                        ) {
                            eprintln!("zsh-tool exec: alan record failed: {}", e);
                        }
                    }
                    Err(e) => {
                        eprintln!("zsh-tool exec: alan db open failed: {}", e);
                    }
                }
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
            eprintln!("zsh-tool exec: {}", e);
            process::exit(127);
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() < 2 {
        print_usage();
    }

    match args[1].as_str() {
        "serve" => {
            serve::run_server();
        }
        "exec" => {
            let exec_args = parse_exec_args(&args[2..]);
            run_exec(exec_args);
        }
        // Backwards compat: if first arg is --meta, treat as exec mode
        "--meta" => {
            let exec_args = parse_exec_args(&args[1..]);
            run_exec(exec_args);
        }
        _ => {
            // Try legacy mode (no subcommand)
            let exec_args = parse_exec_args(&args[1..]);
            run_exec(exec_args);
        }
    }
}
