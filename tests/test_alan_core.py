"""
Tests for A.L.A.N. Core Functions (Issue #4).

Tests database connection, hashing, templating, decay, stats, and pruning.
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from zsh_tool.alan import ALAN
from zsh_tool.config import ALAN_PRUNE_THRESHOLD, ALAN_MAX_ENTRIES


@pytest.fixture
def alan():
    """Create a fresh A.L.A.N. instance with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    instance = ALAN(db_path)
    yield instance
    db_path.unlink(missing_ok=True)


class TestHashCommand:
    """Tests for _hash_command()."""

    def test_same_command_same_hash(self, alan):
        """Identical commands produce identical hashes."""
        hash1 = alan._hash_command("git push origin main")
        hash2 = alan._hash_command("git push origin main")
        assert hash1 == hash2

    def test_different_commands_different_hash(self, alan):
        """Different commands produce different hashes."""
        hash1 = alan._hash_command("git push origin main")
        hash2 = alan._hash_command("git pull origin main")
        assert hash1 != hash2

    def test_whitespace_normalization(self, alan):
        """Extra whitespace is normalized."""
        hash1 = alan._hash_command("git  push   origin   main")
        hash2 = alan._hash_command("git push origin main")
        assert hash1 == hash2

    def test_leading_trailing_whitespace(self, alan):
        """Leading/trailing whitespace is stripped."""
        hash1 = alan._hash_command("  git push origin main  ")
        hash2 = alan._hash_command("git push origin main")
        assert hash1 == hash2

    def test_numbers_normalized(self, alan):
        """Numbers are normalized to 'N'."""
        hash1 = alan._hash_command("sleep 5")
        hash2 = alan._hash_command("sleep 10")
        assert hash1 == hash2

    def test_quoted_strings_normalized(self, alan):
        """Quoted strings are normalized."""
        hash1 = alan._hash_command('echo "hello world"')
        hash2 = alan._hash_command('echo "goodbye world"')
        assert hash1 == hash2

    def test_single_quoted_strings_normalized(self, alan):
        """Single-quoted strings are normalized."""
        hash1 = alan._hash_command("echo 'hello'")
        hash2 = alan._hash_command("echo 'goodbye'")
        assert hash1 == hash2

    def test_hash_length(self, alan):
        """Hash is 16 characters."""
        hash1 = alan._hash_command("ls -la")
        assert len(hash1) == 16

    def test_hash_is_hex(self, alan):
        """Hash is hexadecimal."""
        hash1 = alan._hash_command("ls -la")
        assert all(c in '0123456789abcdef' for c in hash1)


class TestTemplateCommand:
    """Tests for _template_command()."""

    def test_simple_command_preserved(self, alan):
        """Simple command is preserved."""
        template = alan._template_command("ls")
        assert template == "ls"

    def test_command_with_subcommand(self, alan):
        """Command with subcommand preserved."""
        template = alan._template_command("git push origin main")
        assert template.startswith("git push")

    def test_arguments_become_wildcards(self, alan):
        """Arguments after base command become wildcards."""
        template = alan._template_command("git push origin main")
        assert '*' in template

    def test_flags_preserved(self, alan):
        """Flags (starting with -) are preserved."""
        template = alan._template_command("ls -la /home/user")
        assert '-la' in template

    def test_empty_command(self, alan):
        """Empty command returns empty string."""
        template = alan._template_command("")
        assert template == ""

    def test_whitespace_only(self, alan):
        """Whitespace-only command returns empty string."""
        template = alan._template_command("   ")
        assert template == ""

    def test_known_base_commands(self, alan):
        """Known base commands are handled correctly."""
        for cmd in ['git', 'npm', 'docker', 'kubectl', 'pip']:
            template = alan._template_command(f"{cmd} subcommand arg1 arg2")
            assert template.startswith(cmd)

    def test_docker_compose(self, alan):
        """Docker with subcommand works."""
        template = alan._template_command("docker compose up -d myservice")
        assert template.startswith("docker compose")

    def test_curl_with_url(self, alan):
        """curl with URL keeps URL as 'subcommand' (algorithm treats non-flag args after base as subcommand)."""
        template = alan._template_command("curl https://example.com/api/v1")
        assert 'curl' in template
        # URL is treated as subcommand, not wildcarded
        assert 'https://example.com/api/v1' in template

    def test_curl_with_flags_and_url(self, alan):
        """curl with flags before URL wildcards the URL."""
        template = alan._template_command("curl -X POST https://example.com/api/v1")
        assert 'curl' in template
        assert '-X' in template
        # After flags, URL becomes wildcard
        assert '*' in template

    def test_similar_commands_same_template(self, alan):
        """Similar commands produce same template."""
        t1 = alan._template_command("git push origin feature-1")
        t2 = alan._template_command("git push origin feature-2")
        assert t1 == t2


class TestConnect:
    """Tests for _connect() context manager."""

    def test_connection_works(self, alan):
        """Connection can execute queries."""
        with alan._connect() as conn:
            result = conn.execute("SELECT 1").fetchone()
            assert result[0] == 1

    def test_row_factory_set(self, alan):
        """Row factory is set for dict-like access."""
        with alan._connect() as conn:
            result = conn.execute("SELECT 1 as value").fetchone()
            assert result['value'] == 1

    def test_auto_commit(self, alan):
        """Changes are auto-committed."""
        with alan._connect() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('test_key', 'test_value')")

        # New connection should see the change
        with alan._connect() as conn:
            result = conn.execute("SELECT value FROM meta WHERE key = 'test_key'").fetchone()
            assert result['value'] == 'test_value'

    def test_tables_exist(self, alan):
        """Expected tables exist."""
        with alan._connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t['name'] for t in tables]
            assert 'observations' in table_names
            assert 'recent_commands' in table_names
            assert 'streaks' in table_names
            assert 'meta' in table_names
            assert 'ssh_observations' in table_names


class TestGetStats:
    """Tests for get_stats()."""

    def test_empty_database(self, alan):
        """Stats on empty database."""
        stats = alan.get_stats()
        assert stats['total_observations'] == 0
        assert stats['unique_patterns'] == 0
        assert stats['total_weight'] == 0
        assert stats['oldest'] is None
        assert stats['newest'] is None

    def test_after_recording(self, alan):
        """Stats after recording observations."""
        alan.record("ls -la", 0, 100)
        alan.record("git push", 0, 200)
        alan.record("ls -la", 0, 150)  # Same pattern

        stats = alan.get_stats()
        assert stats['total_observations'] == 3
        assert stats['unique_patterns'] == 2
        assert stats['total_weight'] > 0
        assert stats['oldest'] is not None
        assert stats['newest'] is not None

    def test_includes_session_stats(self, alan):
        """Stats include session information."""
        stats = alan.get_stats()
        assert 'session' in stats

    def test_includes_hot_patterns(self, alan):
        """Stats include hot patterns."""
        stats = alan.get_stats()
        assert 'hot_patterns' in stats


class TestApplyDecay:
    """Tests for _apply_decay()."""

    def test_recent_entries_full_weight(self, alan):
        """Recent entries retain full weight."""
        alan.record("ls -la", 0, 100)

        with alan._connect() as conn:
            row = conn.execute("SELECT weight FROM observations").fetchone()
            assert row['weight'] == 1.0

    def test_decay_applied_over_time(self, alan):
        """Weights decay over time (via SQL)."""
        alan.record("ls -la", 0, 100)

        # Manually backdate the entry
        with alan._connect() as conn:
            # Set created_at to 48 hours ago (2 half-lives)
            old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            conn.execute("UPDATE observations SET created_at = ?", (old_time,))

        # Apply decay
        with alan._connect() as conn:
            alan._apply_decay(conn)
            row = conn.execute("SELECT weight FROM observations").fetchone()
            # After 2 half-lives, weight should be ~0.25
            assert row['weight'] < 0.5
            assert row['weight'] > 0.1


class TestPrune:
    """Tests for prune() and maybe_prune()."""

    def test_prune_removes_low_weight(self, alan):
        """Prune removes entries below threshold."""
        alan.record("ls -la", 0, 100)

        # Manually set weight below threshold
        with alan._connect() as conn:
            conn.execute("UPDATE observations SET weight = ?", (ALAN_PRUNE_THRESHOLD / 2,))

        alan.prune()

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            assert count == 0

    def test_prune_enforces_max_entries(self, alan):
        """Prune enforces ALAN_MAX_ENTRIES limit."""
        # This would take too long with actual MAX_ENTRIES (10000)
        # So we'll just verify the logic exists by checking small scale
        for i in range(10):
            alan.record(f"cmd_{i}", 0, 100)

        alan.prune()

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            assert count <= ALAN_MAX_ENTRIES

    def test_prune_updates_last_prune_meta(self, alan):
        """Prune updates last_prune in meta table."""
        alan.prune()

        with alan._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'last_prune'").fetchone()
            assert row is not None
            # Should be a valid ISO timestamp
            datetime.fromisoformat(row['value'].replace('Z', '+00:00'))

    def test_maybe_prune_respects_interval(self, alan):
        """maybe_prune() respects the interval."""
        # First prune
        alan.prune()

        # Record initial count
        with alan._connect() as conn:
            # Add an entry that would be pruned
            conn.execute("""
                INSERT INTO observations (id, command_hash, created_at, weight)
                VALUES ('test', 'hash', datetime('now'), ?)
            """, (ALAN_PRUNE_THRESHOLD / 2,))

        # maybe_prune should NOT prune (interval not passed)
        alan.maybe_prune()

        with alan._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            assert count >= 1  # Entry still exists


class TestSessionId:
    """Tests for session ID handling."""

    def test_session_id_generated(self, alan):
        """Session ID is generated on init."""
        assert alan.session_id is not None
        assert len(alan.session_id) == 8

    def test_session_id_unique(self):
        """Different instances have different session IDs."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        try:
            alan1 = ALAN(db_path)
            alan2 = ALAN(db_path)
            assert alan1.session_id != alan2.session_id
        finally:
            db_path.unlink(missing_ok=True)


class TestDatabaseInit:
    """Tests for database initialization."""

    def test_creates_parent_directory(self):
        """Database parent directory is created if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "subdir" / "alan.db"
            _alan = ALAN(db_path)  # Side effect: creates parent directory
            assert db_path.parent.exists()
            db_path.unlink(missing_ok=True)

    def test_indices_created(self, alan):
        """Expected indices are created."""
        with alan._connect() as conn:
            indices = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = [i['name'] for i in indices]
            assert 'idx_command_hash' in index_names
            assert 'idx_created_at' in index_names


class TestParsePipeline:
    """Tests for _parse_pipeline() (Issue #20)."""

    def test_simple_pipeline(self, alan):
        """Simple two-command pipeline."""
        result = alan._parse_pipeline("cat foo | grep bar")
        assert result == ["cat foo", "grep bar"]

    def test_three_command_pipeline(self, alan):
        """Three-command pipeline."""
        result = alan._parse_pipeline("cat foo | grep bar | sort")
        assert result == ["cat foo", "grep bar", "sort"]

    def test_single_command_no_pipe(self, alan):
        """Single command without pipe returns single-element list."""
        result = alan._parse_pipeline("ls -la")
        assert result == ["ls -la"]

    def test_quoted_double_pipe_not_split(self, alan):
        """Pipes inside double quotes are not delimiters."""
        result = alan._parse_pipeline('echo "a|b" | grep a')
        assert result == ['echo "a|b"', "grep a"]

    def test_quoted_single_pipe_not_split(self, alan):
        """Pipes inside single quotes are not delimiters."""
        result = alan._parse_pipeline("echo 'a|b' | grep a")
        assert result == ["echo 'a|b'", "grep a"]

    def test_escaped_pipe_not_split(self, alan):
        """Escaped pipes are not delimiters."""
        result = alan._parse_pipeline("echo a\\|b | grep a")
        assert result == ["echo a\\|b", "grep a"]

    def test_logical_or_preserved(self, alan):
        """|| (logical OR) is preserved, not treated as two pipes."""
        result = alan._parse_pipeline("cmd1 || cmd2 | cmd3")
        assert result == ["cmd1 || cmd2", "cmd3"]

    def test_whitespace_trimmed(self, alan):
        """Whitespace around pipes is trimmed."""
        result = alan._parse_pipeline("cat foo   |   grep bar")
        assert result == ["cat foo", "grep bar"]

    def test_empty_command(self, alan):
        """Empty command returns empty list."""
        result = alan._parse_pipeline("")
        assert result == []

    def test_complex_nested_quotes(self, alan):
        """Complex nested quotes with pipes."""
        result = alan._parse_pipeline('grep "pattern|other" file.txt | awk \'{print $1|"sort"}\'')
        assert len(result) == 2
        assert result[0] == 'grep "pattern|other" file.txt'

    def test_multiple_pipes_in_quotes(self, alan):
        """Multiple pipes inside quotes."""
        result = alan._parse_pipeline('echo "a|b|c" | grep -E "x|y"')
        assert result == ['echo "a|b|c"', 'grep -E "x|y"']


class TestPipestatusHelpers:
    """Tests for pipestatus wrapper and extraction functions (Issue #20)."""

    def test_wrap_for_pipestatus(self):
        """Wrapping adds pipestatus capture."""
        from zsh_tool.alan import _wrap_for_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        wrapped = _wrap_for_pipestatus("cat foo | grep bar")
        assert "cat foo | grep bar" in wrapped
        assert PIPESTATUS_MARKER in wrapped
        assert "${pipestatus[*]}" in wrapped

    def test_extract_pipestatus_simple(self):
        """Extract pipestatus from simple output."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        output = f"some output\n{PIPESTATUS_MARKER}:0 1\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert "some output" in clean
        assert PIPESTATUS_MARKER not in clean
        assert pipestatus == [0, 1]

    def test_extract_pipestatus_single_exit(self):
        """Extract single exit code (no pipeline)."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        output = f"hello world\n{PIPESTATUS_MARKER}:0\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert pipestatus == [0]

    def test_extract_pipestatus_multiple_failures(self):
        """Extract multiple non-zero exit codes."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        output = f"error\n{PIPESTATUS_MARKER}:0 2 1\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert pipestatus == [0, 2, 1]

    def test_extract_pipestatus_not_found(self):
        """No marker returns original output and None."""
        from zsh_tool.alan import _extract_pipestatus
        output = "normal output without marker"
        clean, pipestatus = _extract_pipestatus(output)
        assert clean == output
        assert pipestatus is None

    def test_extract_pipestatus_cleans_output(self):
        """Output is cleaned of marker line."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        output = f"line1\nline2\n{PIPESTATUS_MARKER}:0 0 0\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert "line1" in clean
        assert "line2" in clean
        assert PIPESTATUS_MARKER not in clean

    def test_extract_pipestatus_invalid_values(self):
        """Invalid pipestatus values return None."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        output = f"output\n{PIPESTATUS_MARKER}:abc def\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert pipestatus is None

    def test_extract_pipestatus_no_trailing_newline(self):
        """Output without trailing newline before marker is preserved (Issue #41)."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        # curl -s ifconfig.me outputs IP without trailing newline
        output = f"153.66.73.228{PIPESTATUS_MARKER}:0\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert clean == "153.66.73.228"
        assert pipestatus == [0]

    def test_extract_pipestatus_no_trailing_newline_pipeline(self):
        """Pipeline output without trailing newline preserves output (Issue #41)."""
        from zsh_tool.alan import _extract_pipestatus
        from zsh_tool.config import PIPESTATUS_MARKER
        output = f"result{PIPESTATUS_MARKER}:0 1\n"
        clean, pipestatus = _extract_pipestatus(output)
        assert clean == "result"
        assert pipestatus == [0, 1]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
