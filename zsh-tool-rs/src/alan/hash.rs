use regex::Regex;
use sha2::{Digest, Sha256};
use std::sync::LazyLock;

static RE_WHITESPACE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());
static RE_DOUBLE_QUOTED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#""[^"]*""#).unwrap());
static RE_SINGLE_QUOTED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"'[^']*'").unwrap());
static RE_BARE_NUMBERS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\b\d+\b").unwrap());

/// Hash a command for pattern matching (SHA-256, first 16 hex chars).
///
/// Normalization matches Python's `_hash_command()`:
/// - Collapse whitespace
/// - Replace quoted strings with empty quotes
/// - Replace bare numbers with N
pub fn hash_command(command: &str) -> String {
    let normalized = command.trim();
    let normalized = RE_WHITESPACE.replace_all(normalized, " ");
    let normalized = RE_DOUBLE_QUOTED.replace_all(&normalized, r#""""#);
    let normalized = RE_SINGLE_QUOTED.replace_all(&normalized, "''");
    let normalized = RE_BARE_NUMBERS.replace_all(&normalized, "N");

    let mut hasher = Sha256::new();
    hasher.update(normalized.as_bytes());
    let result = hasher.finalize();
    format!("{:x}", result)[..16].to_string()
}

/// Known base commands that take subcommands (e.g. `git push`, `docker run`).
const BASE_COMMANDS: &[&str] = &[
    "git", "npm", "yarn", "pip", "docker", "kubectl", "make", "cargo", "go", "python", "node",
    "ruby", "curl", "wget", "cat", "grep", "find", "ls", "cd", "rm", "cp", "mv", "mkdir",
    "systemctl", "journalctl", "apt", "pacman", "brew",
];

/// Create a fuzzy template for similarity matching.
///
/// Matches Python's `_template_command()`:
/// - Keep base command + subcommand
/// - Keep flags (start with -)
/// - Replace positional args with *
pub fn template_command(command: &str) -> String {
    let normalized = RE_WHITESPACE.replace_all(command.trim(), " ");
    let parts: Vec<&str> = normalized.split(' ').collect();
    if parts.is_empty() {
        return String::new();
    }

    let mut template_parts: Vec<String> = Vec::new();
    let mut found_base = false;

    for (i, part) in parts.iter().enumerate() {
        if !found_base {
            template_parts.push(part.to_string());
            if BASE_COMMANDS.contains(&part.to_lowercase().as_str()) {
                // Include next part if it looks like a subcommand
                if i + 1 < parts.len() && !parts[i + 1].starts_with('-') {
                    continue;
                }
                found_base = true;
            } else if i >= 1 {
                // After 2 parts, assume we have base
                found_base = true;
            }
        } else {
            // Replace arguments with wildcards, keep flags
            if part.starts_with('-') {
                template_parts.push(part.to_string());
            } else if template_parts.last().is_none_or(|l| l != "*") {
                template_parts.push("*".to_string());
            }
        }
    }

    template_parts.join(" ")
}
