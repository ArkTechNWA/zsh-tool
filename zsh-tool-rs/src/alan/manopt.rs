//! Man page option parsing and caching.
//! Replaces the Python `scripts/manopt` script.

use regex::Regex;
use rusqlite::Connection;
use std::process::Command;

/// Run `man <command>` and parse options into a formatted table.
/// Returns the table string, or None if no man page or no options found.
pub fn parse_manopt(base_command: &str, max_width: usize) -> Option<String> {
    let man_text = get_man_text(base_command)?;
    let section = extract_options_section(&man_text);

    let lines = if section.is_empty() {
        // Fall back to scanning the whole page
        man_text.lines().map(|s| s.to_string()).collect()
    } else {
        section
    };

    let entries = parse_options(&lines);
    if entries.is_empty() {
        return None;
    }

    Some(build_table(base_command, &entries, max_width))
}

/// Run manopt, cache the result, return the table text.
pub fn run_and_cache(conn: &Connection, base_command: &str) -> Option<String> {
    let text = parse_manopt(base_command, 120)?;
    let now_iso = chrono::Utc::now().to_rfc3339();

    let _ = conn.execute(
        "INSERT OR REPLACE INTO manopt_cache (base_command, options_text, created_at)
         VALUES (?1, ?2, ?3)",
        rusqlite::params![base_command, text, now_iso],
    );

    Some(text)
}

/// Get cached manopt text for a command.
pub fn get_cached(conn: &Connection, base_command: &str) -> Option<String> {
    conn.query_row(
        "SELECT options_text FROM manopt_cache WHERE base_command = ?",
        rusqlite::params![base_command],
        |row| row.get(0),
    )
    .ok()
}

// --- Internals ---

fn get_man_text(command: &str) -> Option<String> {
    let man_output = Command::new("man").arg(command).output().ok()?;

    if !man_output.status.success() {
        return None;
    }

    // Strip backspace-based formatting using `col -b`
    let col_output = Command::new("col")
        .arg("-b")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .ok()
        .and_then(|mut child| {
            use std::io::Write;
            if let Some(ref mut stdin) = child.stdin {
                let _ = stdin.write_all(&man_output.stdout);
            }
            child.wait_with_output().ok()
        })?;

    String::from_utf8(col_output.stdout).ok()
}

fn extract_options_section(text: &str) -> Vec<String> {
    let lines: Vec<&str> = text.lines().collect();
    let header_re = Regex::new(r"^[A-Z][A-Z /()-]+$").unwrap();

    // Map section names -> (start, end) ranges
    let mut sections: Vec<(&str, usize)> = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let stripped = line.trim();
        if stripped.len() > 2 && header_re.is_match(stripped) {
            sections.push((stripped, i));
        }
    }

    let mut section_ranges: std::collections::HashMap<&str, Vec<(usize, usize)>> =
        std::collections::HashMap::new();
    for (idx, &(name, start)) in sections.iter().enumerate() {
        let end = if idx + 1 < sections.len() {
            sections[idx + 1].1
        } else {
            lines.len()
        };
        section_ranges
            .entry(name)
            .or_default()
            .push((start + 1, end));
    }

    let preferred = [
        "ALL OPTIONS",
        "OPTIONS",
        "COMMAND OPTIONS",
        "GENERAL OPTIONS",
        "GLOBAL OPTIONS",
        "COMMON OPTIONS",
    ];
    let fallback = ["DESCRIPTION"];

    let mut result = Vec::new();
    for name in &preferred {
        if let Some(ranges) = section_ranges.get(name) {
            for &(start, end) in ranges {
                for line in lines.iter().take(end).skip(start) {
                    result.push(line.to_string());
                }
            }
        }
    }

    if result.is_empty() {
        for name in &fallback {
            if let Some(ranges) = section_ranges.get(name) {
                for &(start, end) in ranges {
                    for line in lines.iter().take(end).skip(start) {
                        result.push(line.to_string());
                    }
                }
            }
        }
    }

    result
}

fn parse_options(section_lines: &[String]) -> Vec<(String, String)> {
    let opt_line = Regex::new(r"^( {4,12})(-.+)$").unwrap();
    let desc_line = Regex::new(r"^[\t][\t ]*(\S.*)$").unwrap();
    let flag_extract = Regex::new(
        r"^(-\w(?:,\s+--[^\s]+)?|--[^\s]+)(?:\s+(.+))?$",
    )
    .unwrap();

    let mut entries: Vec<(String, String)> = Vec::new();
    let mut current_flags: Option<String> = None;
    let mut current_desc_lines: Vec<String> = Vec::new();

    let flush = |flags: &Option<String>,
                 desc_lines: &[String],
                 entries: &mut Vec<(String, String)>| {
        if let Some(ref f) = flags {
            let desc = desc_lines.join(" ").trim().to_string();
            entries.push((f.trim().to_string(), desc));
        }
    };

    for line in section_lines {
        if let Some(om) = opt_line.captures(line) {
            flush(&current_flags, &current_desc_lines, &mut entries);
            let raw = om.get(2).unwrap().as_str().trim();
            if let Some(fm) = flag_extract.captures(raw) {
                current_flags = Some(fm.get(1).unwrap().as_str().to_string());
                current_desc_lines = fm
                    .get(2)
                    .map(|m| vec![m.as_str().to_string()])
                    .unwrap_or_default();
            } else {
                current_flags = Some(raw.to_string());
                current_desc_lines = Vec::new();
            }
        } else if let Some(dm) = desc_line.captures(line) {
            if current_flags.is_some() {
                current_desc_lines.push(dm.get(1).unwrap().as_str().to_string());
            }
        }
    }
    flush(&current_flags, &current_desc_lines, &mut entries);

    // Deduplicate
    let mut seen = std::collections::HashSet::new();
    entries
        .into_iter()
        .filter_map(|(flags, desc)| {
            let canon = flags.trim_end_matches(&['.', ',', ';', ':'][..]).to_string();
            if canon.is_empty() || seen.contains(&canon) {
                None
            } else {
                seen.insert(canon.clone());
                Some((canon, desc))
            }
        })
        .collect()
}

fn build_table(command: &str, entries: &[(String, String)], max_width: usize) -> String {
    if entries.is_empty() {
        return format!("No options found for '{}'.", command);
    }

    let flag_width = entries
        .iter()
        .map(|(f, _)| f.len())
        .max()
        .unwrap_or(10)
        .min(34);
    let desc_width = max_width.saturating_sub(flag_width + 7); // borders + padding

    let header = format!("  {} options", command);
    let mut lines = Vec::new();

    lines.push(format!(
        "\u{250c}{}\u{2510}",
        "\u{2500}".repeat(max_width - 2)
    ));
    lines.push(format!(
        "\u{2502}{:^width$}\u{2502}",
        header,
        width = max_width - 2
    ));
    lines.push(format!(
        "\u{251c}{}\u{252c}{}\u{2524}",
        "\u{2500}".repeat(flag_width + 2),
        "\u{2500}".repeat(desc_width + 2)
    ));

    for (flags, desc) in entries {
        let mut f = flags.clone();
        let mut d = desc.clone();
        if f.len() > flag_width {
            f.truncate(flag_width - 1);
            f.push('\u{2026}');
        }
        if d.len() > desc_width {
            d.truncate(desc_width - 1);
            d.push('\u{2026}');
        }
        lines.push(format!(
            "\u{2502} {:<fw$} \u{2502} {:<dw$} \u{2502}",
            f,
            d,
            fw = flag_width,
            dw = desc_width
        ));
    }

    lines.push(format!(
        "\u{2514}{}\u{2534}{}\u{2518}",
        "\u{2500}".repeat(flag_width + 2),
        "\u{2500}".repeat(desc_width + 2)
    ));

    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_options_basic() {
        let lines = vec![
            "       -a, --all".to_string(),
            "\tdo not ignore entries starting with .".to_string(),
            "       -l".to_string(),
            "\tuse a long listing format".to_string(),
        ];
        let entries = parse_options(&lines);
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].0, "-a, --all");
        assert!(entries[0].1.contains("do not ignore"));
        assert_eq!(entries[1].0, "-l");
    }

    #[test]
    fn test_build_table_output() {
        let entries = vec![
            ("-a, --all".to_string(), "show all".to_string()),
            ("-l".to_string(), "long format".to_string()),
        ];
        let table = build_table("ls", &entries, 60);
        assert!(table.contains("ls options"));
        assert!(table.contains("-a, --all"));
        assert!(table.contains("-l"));
    }

    #[test]
    fn test_parse_manopt_ls() {
        // This test requires `man` and `ls` to be installed
        if let Some(table) = parse_manopt("ls", 100) {
            assert!(table.contains("ls options"));
            assert!(table.contains("-l") || table.contains("--all"));
        }
        // If man page not available, test just passes (CI may not have man)
    }
}
