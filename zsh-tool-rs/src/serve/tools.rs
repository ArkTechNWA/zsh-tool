//! MCP tool definitions — the 10 tools exposed to Claude Code.

use serde_json::{json, Value};

use super::protocol::tool_def;

pub fn list_tools(timeout_default: u64, timeout_max: u64, yield_after: f64) -> Value {
    json!({
        "tools": [
            tool_def("zsh",
                "Execute a zsh command with yield-based oversight. Returns after yield_after seconds with partial output if still running. Use zsh_poll to continue collecting output.",
                json!({
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The zsh command to execute"
                        },
                        "timeout": {
                            "type": "integer",
                            "description": format!("Max execution time in seconds (default: {}, max: {})", timeout_default, timeout_max)
                        },
                        "yield_after": {
                            "type": "number",
                            "description": format!("Return control after this many seconds if still running (default: {})", yield_after)
                        },
                        "description": {
                            "type": "string",
                            "description": "Human-readable description of what this command does"
                        },
                        "pty": {
                            "type": "boolean",
                            "description": "Use PTY (pseudo-terminal) mode for full terminal emulation. Enables proper handling of interactive prompts, colors, and programs that require a TTY."
                        }
                    },
                    "required": ["command"]
                })
            ),
            tool_def("zsh_poll",
                "Get more output from a running task. Call repeatedly until status is not 'running'.",
                json!({
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID returned from zsh command"
                        }
                    },
                    "required": ["task_id"]
                })
            ),
            tool_def("zsh_send",
                "Send input to a running task's stdin. Use for interactive commands that need input.",
                json!({
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID of the running command"
                        },
                        "input": {
                            "type": "string",
                            "description": "Text to send to stdin (newline added automatically)"
                        }
                    },
                    "required": ["task_id", "input"]
                })
            ),
            tool_def("zsh_kill",
                "Kill a running task.",
                json!({
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID to kill"
                        }
                    },
                    "required": ["task_id"]
                })
            ),
            tool_def("zsh_tasks",
                "List all active tasks with their status.",
                json!({"type": "object", "properties": {}})
            ),
            tool_def("zsh_health",
                "Get health status of zsh-tool including NEVERHANG and A.L.A.N. status",
                json!({"type": "object", "properties": {}})
            ),
            tool_def("zsh_alan_stats",
                "Get A.L.A.N. learning database statistics",
                json!({"type": "object", "properties": {}})
            ),
            tool_def("zsh_alan_query",
                "Query A.L.A.N. for insights about a command pattern",
                json!({
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Command to query pattern stats for"
                        }
                    },
                    "required": ["command"]
                })
            ),
            tool_def("zsh_neverhang_status",
                "Get NEVERHANG circuit breaker status",
                json!({"type": "object", "properties": {}})
            ),
            tool_def("zsh_neverhang_reset",
                "Reset NEVERHANG circuit breaker to closed state",
                json!({"type": "object", "properties": {}})
            )
        ]
    })
}
