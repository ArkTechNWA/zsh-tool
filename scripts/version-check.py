#!/usr/bin/env python3
"""
Version Bloodhound
==================
Scans codebase for version declarations. Fails if they disagree.

Not "check these 3 files" but "find everything that claims to be a version."
"""

import re
import sys
from pathlib import Path
from dataclasses import dataclass


@dataclass
class VersionMatch:
    """A version found in the codebase."""
    file: Path
    line_num: int
    line: str
    version: str
    context: str  # Why we think this is a version declaration


# Patterns that indicate a VERSION DECLARATION (not just a mention)
VERSION_PATTERNS = [
    # Python: __version__ = "0.4.80"
    (r'__version__\s*=\s*["\']([0-9]+\.[0-9]+\.[0-9]+)["\']', "Python __version__"),

    # TOML/pyproject: version = "0.4.80"
    (r'^version\s*=\s*["\']([0-9]+\.[0-9]+\.[0-9]+)["\']', "TOML version"),

    # JSON: "version": "0.4.80"
    (r'"version"\s*:\s*"([0-9]+\.[0-9]+\.[0-9]+)"', "JSON version"),

    # Markdown status badge: (v0.4.80)
    (r'\*\*Status:\*\*.*\(v([0-9]+\.[0-9]+\.[0-9]+)\)', "README status"),
]

# Files/patterns to skip entirely
SKIP_PATTERNS = [
    r'\.git/',
    r'__pycache__/',
    r'\.venv/',
    r'\.pytest_cache/',
    r'node_modules/',
    r'\.egg-info/',
    r'dist/',
    r'build/',
    r'uv\.lock$',
    r'\.pyc$',
    r'_reference/',
    r'docs/plans/',
    r'Cargo\.lock$',
    r'\.cargo/',
    r'target/',
]

# Line-level exclusions (changelog entries, comments about old versions, etc.)
EXCLUDE_LINE_PATTERNS = [
    r'^###\s+[0-9]+\.[0-9]+',      # Changelog headers: ### 0.4.75
    r'changelog',                   # Changelog mentions
    r'previous.*version',           # "previous version was..."
    r'since\s+v?[0-9]+\.[0-9]+',   # "since v0.3.0"
    r'deprecated.*[0-9]+\.[0-9]+', # Deprecation notes
    r'^\s*#.*[0-9]+\.[0-9]+',      # Python comments with versions
    r'//.*[0-9]+\.[0-9]+',         # JS comments with versions
]


def should_skip_file(path: Path) -> bool:
    """Check if file should be skipped entirely."""
    path_str = str(path)
    return any(re.search(p, path_str) for p in SKIP_PATTERNS)


def should_exclude_line(line: str) -> bool:
    """Check if line should be excluded (changelog, comment, etc.)."""
    line_lower = line.lower()
    return any(re.search(p, line_lower, re.IGNORECASE) for p in EXCLUDE_LINE_PATTERNS)


def scan_file(path: Path) -> list[VersionMatch]:
    """Scan a single file for version declarations."""
    matches = []

    try:
        content = path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return matches

    for line_num, line in enumerate(content.splitlines(), 1):
        # Skip excluded lines
        if should_exclude_line(line):
            continue

        for pattern, context in VERSION_PATTERNS:
            match = re.search(pattern, line)
            if match:
                matches.append(VersionMatch(
                    file=path,
                    line_num=line_num,
                    line=line.strip(),
                    version=match.group(1),
                    context=context
                ))

    return matches


def scan_codebase(root: Path) -> list[VersionMatch]:
    """Scan entire codebase for version declarations."""
    all_matches = []

    # File extensions to scan
    extensions = {'.py', '.toml', '.json', '.md', '.yaml', '.yml'}

    for path in root.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue
        if should_skip_file(path):
            continue

        all_matches.extend(scan_file(path))

    return all_matches


def check_versions(matches: list[VersionMatch]) -> tuple[bool, dict[str, list[VersionMatch]]]:
    """Check if all versions match. Returns (success, grouped_by_version)."""
    by_version: dict[str, list[VersionMatch]] = {}

    for match in matches:
        by_version.setdefault(match.version, []).append(match)

    success = len(by_version) <= 1
    return success, by_version


def main():
    root = Path(__file__).parent.parent

    print("Version Bloodhound")
    print("=" * 50)
    print(f"Scanning: {root}")
    print()

    matches = scan_codebase(root)

    if not matches:
        print("No version declarations found.")
        print("This might be a problem - expected at least __version__ and pyproject.toml")
        sys.exit(1)

    success, by_version = check_versions(matches)

    # Report findings
    print(f"Found {len(matches)} version declaration(s):")
    print()

    for version, version_matches in sorted(by_version.items(), reverse=True):
        print(f"  {version}:")
        for m in version_matches:
            rel_path = m.file.relative_to(root)
            print(f"    {rel_path}:{m.line_num} ({m.context})")
        print()

    if success:
        version = list(by_version.keys())[0]
        print(f"All versions match: {version}")
        sys.exit(0)
    else:
        print("VERSION MISMATCH DETECTED")
        print("-" * 50)
        versions = sorted(by_version.keys(), reverse=True)
        print(f"Expected: {versions[0]} (highest)")
        print(f"Found: {', '.join(versions)}")
        print()
        print("Fix: Update all version declarations to match.")
        sys.exit(1)


if __name__ == "__main__":
    main()
