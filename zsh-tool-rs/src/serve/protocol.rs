//! MCP JSON-RPC 2.0 protocol types and framing.
//!
//! Supports both Content-Length framed and bare newline-delimited JSON.
//! Auto-detects on first message; responds in the same format.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};

/// Whether the client uses bare JSON (no Content-Length framing).
static BARE_JSON_MODE: AtomicBool = AtomicBool::new(false);

/// JSON-RPC 2.0 request.
#[derive(Debug, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    pub id: Option<Value>,
    pub method: String,
    #[serde(default)]
    pub params: Option<Value>,
}

/// JSON-RPC 2.0 response.
#[derive(Debug, Serialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
}

#[derive(Debug, Serialize)]
pub struct JsonRpcError {
    pub code: i64,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl JsonRpcResponse {
    pub fn success(id: Option<Value>, result: Value) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            id,
            result: Some(result),
            error: None,
        }
    }

    pub fn error(id: Option<Value>, code: i64, message: String) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            id,
            result: None,
            error: Some(JsonRpcError {
                code,
                message,
                data: None,
            }),
        }
    }
}

/// MCP initialize result.
pub fn initialize_result(server_name: &str, version: &str) -> Value {
    serde_json::json!({
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {}
        },
        "serverInfo": {
            "name": server_name,
            "version": version
        }
    })
}

/// MCP tool definition.
pub fn tool_def(name: &str, description: &str, schema: Value) -> Value {
    serde_json::json!({
        "name": name,
        "description": description,
        "inputSchema": schema
    })
}

/// Build a text content response (MCP tools/call result format).
pub fn text_content(text: &str) -> Value {
    serde_json::json!({
        "content": [{
            "type": "text",
            "text": text
        }]
    })
}

/// Build an error text content response.
pub fn error_content(text: &str) -> Value {
    serde_json::json!({
        "content": [{
            "type": "text",
            "text": text
        }],
        "isError": true
    })
}

/// Read a JSON-RPC message from stdin.
/// Auto-detects bare JSON lines vs Content-Length framing.
/// Returns None on EOF.
pub fn read_message(reader: &mut impl std::io::BufRead) -> Option<JsonRpcRequest> {
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) => {
            eprintln!("[zsh-tool:proto] EOF on stdin");
            return None;
        }
        Ok(_) => {}
        Err(e) => {
            eprintln!("[zsh-tool:proto] Read error: {}", e);
            return None;
        }
    }

    let trimmed = line.trim();
    if trimmed.is_empty() {
        // Blank line — try again (could be between messages)
        return read_message(reader);
    }

    // Detect: does this line start with '{' (bare JSON) or 'Content-Length:' (framed)?
    if trimmed.starts_with('{') {
        // Bare JSON mode
        if !BARE_JSON_MODE.load(Ordering::Relaxed) {
            eprintln!("[zsh-tool:proto] Detected bare JSON mode");
            BARE_JSON_MODE.store(true, Ordering::Relaxed);
        }
        match serde_json::from_str(trimmed) {
            Ok(req) => Some(req),
            Err(e) => {
                eprintln!("[zsh-tool:proto] JSON parse error: {} — line: {:?}", e, trimmed);
                None
            }
        }
    } else if let Some(len_str) = trimmed.strip_prefix("Content-Length:") {
        // Content-Length framed mode
        let content_length: usize = match len_str.trim().parse() {
            Ok(l) => l,
            Err(_) => {
                eprintln!("[zsh-tool:proto] Bad Content-Length: {:?}", len_str.trim());
                return None;
            }
        };
        eprintln!("[zsh-tool:proto] Content-Length: {}", content_length);

        // Read remaining headers until empty line
        loop {
            let mut header = String::new();
            match reader.read_line(&mut header) {
                Ok(0) => {
                    eprintln!("[zsh-tool:proto] EOF during headers");
                    return None;
                }
                Ok(_) => {
                    if header.trim().is_empty() {
                        break;
                    }
                }
                Err(e) => {
                    eprintln!("[zsh-tool:proto] Header read error: {}", e);
                    return None;
                }
            }
        }

        // Read body
        let mut body = vec![0u8; content_length];
        if let Err(e) = std::io::Read::read_exact(reader, &mut body) {
            eprintln!("[zsh-tool:proto] Body read error: {} (expected {} bytes)", e, content_length);
            return None;
        }

        match serde_json::from_slice(&body) {
            Ok(req) => Some(req),
            Err(e) => {
                eprintln!("[zsh-tool:proto] JSON parse error: {} — body: {:?}",
                    e, String::from_utf8_lossy(&body));
                None
            }
        }
    } else {
        eprintln!("[zsh-tool:proto] Unexpected line: {:?}", trimmed);
        None
    }
}

/// Write a JSON-RPC response to stdout.
/// Uses bare JSON or Content-Length framing to match the client.
pub fn write_message(writer: &mut impl std::io::Write, response: &JsonRpcResponse) {
    let body = serde_json::to_string(response).unwrap_or_default();
    eprintln!("[zsh-tool:proto] Writing {} bytes (bare={})", body.len(), BARE_JSON_MODE.load(Ordering::Relaxed));

    if BARE_JSON_MODE.load(Ordering::Relaxed) {
        // Bare JSON: one line + newline
        if let Err(e) = writer.write_all(body.as_bytes()) {
            eprintln!("[zsh-tool:proto] Write error: {}", e);
            return;
        }
        if let Err(e) = writer.write_all(b"\n") {
            eprintln!("[zsh-tool:proto] Newline write error: {}", e);
            return;
        }
    } else {
        // Content-Length framed
        let header = format!("Content-Length: {}\r\n\r\n", body.len());
        if let Err(e) = writer.write_all(header.as_bytes()) {
            eprintln!("[zsh-tool:proto] Header write error: {}", e);
            return;
        }
        if let Err(e) = writer.write_all(body.as_bytes()) {
            eprintln!("[zsh-tool:proto] Body write error: {}", e);
            return;
        }
    }
    if let Err(e) = writer.flush() {
        eprintln!("[zsh-tool:proto] Flush error: {}", e);
    }
}
