/// Split command on unquoted pipe characters.
///
/// Handles:
/// - Simple pipes: cmd1 | cmd2
/// - Quoted pipes: echo "a|b" | grep a (the | in quotes is NOT a delimiter)
/// - Escaped pipes: echo a\|b | grep a
/// - Logical OR (||) is NOT a pipe delimiter
///
/// Matches Python's `_parse_pipeline()`.
pub fn parse_pipeline(command: &str) -> Vec<String> {
    let mut segments = Vec::new();
    let mut current = String::new();
    let chars: Vec<char> = command.chars().collect();
    let mut i = 0;
    let mut in_single_quote = false;
    let mut in_double_quote = false;
    let mut escape_next = false;

    while i < chars.len() {
        let ch = chars[i];

        if escape_next {
            current.push(ch);
            escape_next = false;
            i += 1;
            continue;
        }

        if ch == '\\' {
            escape_next = true;
            current.push(ch);
            i += 1;
            continue;
        }

        if ch == '\'' && !in_double_quote {
            in_single_quote = !in_single_quote;
            current.push(ch);
            i += 1;
            continue;
        }

        if ch == '"' && !in_single_quote {
            in_double_quote = !in_double_quote;
            current.push(ch);
            i += 1;
            continue;
        }

        if ch == '|' && !in_single_quote && !in_double_quote {
            // Check for || (logical OR) — not a pipe
            if i + 1 < chars.len() && chars[i + 1] == '|' {
                current.push('|');
                current.push('|');
                i += 2;
                continue;
            }
            // It's a pipe delimiter
            let segment = current.trim().to_string();
            if !segment.is_empty() {
                segments.push(segment);
            }
            current.clear();
            i += 1;
            continue;
        }

        current.push(ch);
        i += 1;
    }

    let final_seg = current.trim().to_string();
    if !final_seg.is_empty() {
        segments.push(final_seg);
    }

    segments
}
