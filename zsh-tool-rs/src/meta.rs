use serde::Serialize;
use std::fs;
use std::path::Path;

#[derive(Debug, Serialize)]
pub struct ExecResult {
    pub pipestatus: Vec<i32>,
    pub exit_code: i32,
    pub elapsed_ms: u64,
    pub timed_out: bool,
}

pub fn write_meta(path: &str, result: &ExecResult) -> Result<(), String> {
    let json = serde_json::to_string(result).map_err(|e| format!("json: {}", e))?;
    fs::write(Path::new(path), json).map_err(|e| format!("write: {}", e))
}
