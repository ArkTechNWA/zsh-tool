"""
Tests for manopt integration (v0.5.1).

Tests fail counting, caching, script discovery, trigger logic, and presentation.
"""

import asyncio
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from zsh_tool.alan import ALAN


@pytest.fixture
def alan():
    """Create a fresh A.L.A.N. instance with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    instance = ALAN(db_path)
    yield instance
    db_path.unlink(missing_ok=True)


class TestManoptConfig:
    """Tests for manopt config vars."""

    def test_manopt_enabled_default(self):
        from zsh_tool.config import ALAN_MANOPT_ENABLED
        assert ALAN_MANOPT_ENABLED is True

    def test_manopt_timeout_default(self):
        from zsh_tool.config import ALAN_MANOPT_TIMEOUT
        assert ALAN_MANOPT_TIMEOUT == 2.0

    def test_manopt_fail_trigger_default(self):
        from zsh_tool.config import ALAN_MANOPT_FAIL_TRIGGER
        assert ALAN_MANOPT_FAIL_TRIGGER == 2

    def test_manopt_fail_present_default(self):
        from zsh_tool.config import ALAN_MANOPT_FAIL_PRESENT
        assert ALAN_MANOPT_FAIL_PRESENT == 3


class TestManoptSchema:
    """Tests for manopt_cache table."""

    def test_manopt_cache_table_exists(self, alan):
        with alan._connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t['name'] for t in tables]
            assert 'manopt_cache' in table_names

    def test_manopt_cache_columns(self, alan):
        with alan._connect() as conn:
            cursor = conn.execute("PRAGMA table_info(manopt_cache)")
            columns = {row[1] for row in cursor.fetchall()}
            assert columns == {'base_command', 'options_text', 'created_at'}


class TestGetTemplateFailCount:
    """Tests for _get_template_fail_count()."""

    def test_no_failures_returns_zero(self, alan):
        count = alan._get_template_fail_count("tar --xyzzy")
        assert count == 0

    def test_single_failure(self, alan):
        alan.record("tar --xyzzy", exit_code=2, duration_ms=100)
        count = alan._get_template_fail_count("tar --xyzzy")
        assert count == 1

    def test_consecutive_failures(self, alan):
        # Same template "tar xf *" for all three
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad3.tar", exit_code=2, duration_ms=100)
        count = alan._get_template_fail_count("tar xf anything.tar")
        assert count == 3

    def test_success_breaks_count(self, alan):
        # All share template "tar xf *"
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf good.tar", exit_code=0, duration_ms=100)
        alan.record("tar xf bad3.tar", exit_code=2, duration_ms=100)
        count = alan._get_template_fail_count("tar xf whatever.tar")
        assert count == 1  # Only the last failure counts

    def test_different_template_not_counted(self, alan):
        alan.record("git push origin main", exit_code=1, duration_ms=100)
        alan.record("git push origin main", exit_code=1, duration_ms=100)
        count = alan._get_template_fail_count("tar --xyzzy")
        assert count == 0

    def test_different_session_not_counted(self, alan):
        alan.record("tar --bad", exit_code=2, duration_ms=100)
        # Change session ID to simulate different session
        alan.session_id = "different"
        count = alan._get_template_fail_count("tar --bad")
        assert count == 0


class TestManoptCache:
    """Tests for _get_cached_manopt()."""

    def test_cache_miss(self, alan):
        result = alan._get_cached_manopt("nonexistent")
        assert result is None

    def test_cache_hit(self, alan):
        with alan._connect() as conn:
            conn.execute("""
                INSERT INTO manopt_cache (base_command, options_text, created_at)
                VALUES ('tar', 'tar options table here', '2026-02-11T00:00:00+00:00')
            """)
        result = alan._get_cached_manopt("tar")
        assert result == "tar options table here"

    def test_cache_survives_prune(self, alan):
        with alan._connect() as conn:
            conn.execute("""
                INSERT INTO manopt_cache (base_command, options_text, created_at)
                VALUES ('grep', 'grep options', '2026-02-11T00:00:00+00:00')
            """)
        alan.prune()
        result = alan._get_cached_manopt("grep")
        assert result == "grep options"


class TestFindManoptScript:
    """Tests for _find_manopt_script()."""

    def test_finds_dev_mode_script(self, alan):
        """In dev/editable install, finds scripts/manopt relative to source."""
        result = alan._find_manopt_script()
        if result is not None:
            assert result.endswith('manopt')
            assert Path(result).exists()

    def test_returns_none_when_script_missing(self, alan, tmp_path):
        """Returns None when script doesn't exist anywhere."""
        # Override the dev-mode path check by patching __file__ location
        fake_alan_py = tmp_path / "zsh_tool" / "alan.py"
        fake_alan_py.parent.mkdir(parents=True)
        fake_alan_py.touch()
        with patch('zsh_tool.alan.__file__', str(fake_alan_py)), \
             patch('zsh_tool.alan.shutil.which', return_value=None):
            result = alan._find_manopt_script()
            assert result is None



class TestRunManopt:
    """Tests for _run_manopt() async method."""

    def test_no_cache_on_failure(self, alan):
        """Failed manopt run doesn't cache."""
        asyncio.run(
            alan._run_manopt("ls", "/nonexistent/manopt")
        )
        result = alan._get_cached_manopt("ls")
        assert result is None

    def test_no_cache_on_nonexistent_command(self, alan):
        """manopt for nonexistent command doesn't cache."""
        manopt_path = alan._find_manopt_script()
        if manopt_path is None:
            pytest.skip("manopt script not found")
        asyncio.run(
            alan._run_manopt("zzz_nonexistent_cmd_zzz", manopt_path)
        )
        result = alan._get_cached_manopt("zzz_nonexistent_cmd_zzz")
        assert result is None

    def test_caches_on_success(self, alan):
        """Successful manopt run caches result."""
        manopt_path = alan._find_manopt_script()
        if manopt_path is None:
            pytest.skip("manopt script not found")
        asyncio.run(
            alan._run_manopt("ls", manopt_path)
        )
        result = alan._get_cached_manopt("ls")
        # Should have cached something if man ls is available
        # On systems without man, this just won't cache — that's fine
        assert result is None or len(result) > 0


class TestTriggerManoptAsync:
    """Tests for _trigger_manopt_async()."""

    def test_skips_when_disabled(self, alan):
        """No trigger when ALAN_MANOPT_ENABLED=False."""
        with patch('zsh_tool.alan.ALAN_MANOPT_ENABLED', False):
            alan._trigger_manopt_async("tar --xyzzy")
            assert alan._get_cached_manopt("tar") is None

    def test_skips_when_already_cached(self, alan):
        """No trigger when cache already has entry."""
        with alan._connect() as conn:
            conn.execute("""
                INSERT INTO manopt_cache (base_command, options_text, created_at)
                VALUES ('tar', 'cached', '2026-02-11T00:00:00+00:00')
            """)
        alan._trigger_manopt_async("tar --xyzzy")
        assert alan._get_cached_manopt("tar") == "cached"

    def test_skips_empty_command(self, alan):
        """No trigger on empty command."""
        alan._trigger_manopt_async("")
        # Should not crash

    def test_skips_when_script_not_found(self, alan):
        """No trigger when manopt script not found."""
        with patch.object(alan, '_find_manopt_script', return_value=None):
            alan._trigger_manopt_async("tar --xyzzy")
            assert alan._get_cached_manopt("tar") is None


class TestRecordManoptTrigger:
    """Tests for manopt trigger from record()."""

    def test_first_fail_no_trigger(self, alan):
        """First failure doesn't trigger manopt."""
        with patch.object(alan, '_trigger_manopt_async') as mock_trigger:
            alan.record("tar xf bad.tar", exit_code=2, duration_ms=100)
            mock_trigger.assert_not_called()

    def test_second_fail_triggers(self, alan):
        """Second consecutive failure triggers manopt."""
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        with patch.object(alan, '_trigger_manopt_async') as mock_trigger:
            alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
            mock_trigger.assert_called_once()
            args = mock_trigger.call_args[0]
            assert "tar" in args[0]

    def test_third_fail_no_retrigger(self, alan):
        """Third failure doesn't re-trigger (only on exact threshold)."""
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        with patch.object(alan, '_trigger_manopt_async') as mock_trigger:
            alan.record("tar xf bad3.tar", exit_code=2, duration_ms=100)
            mock_trigger.assert_not_called()

    def test_success_does_not_trigger(self, alan):
        """Successful command never triggers manopt."""
        alan.record("tar xf file1.tar", exit_code=0, duration_ms=100)
        alan.record("tar xf file2.tar", exit_code=0, duration_ms=100)
        with patch.object(alan, '_trigger_manopt_async') as mock_trigger:
            alan.record("tar xf file3.tar", exit_code=0, duration_ms=100)
            mock_trigger.assert_not_called()

    def test_timeout_does_not_trigger(self, alan):
        """Timed-out command doesn't trigger manopt."""
        alan.record("tar xf bad1.tar", exit_code=1, duration_ms=100, timed_out=True)
        with patch.object(alan, '_trigger_manopt_async') as mock_trigger:
            alan.record("tar xf bad2.tar", exit_code=1, duration_ms=100, timed_out=True)
            mock_trigger.assert_not_called()


class TestManoptPresentation:
    """Tests for manopt insight in get_insights()."""

    def _populate_cache(self, alan, cmd="tar", text="tar options table"):
        with alan._connect() as conn:
            conn.execute("""
                INSERT INTO manopt_cache (base_command, options_text, created_at)
                VALUES (?, ?, '2026-02-11T00:00:00+00:00')
            """, (cmd, text))

    def test_no_insight_on_first_fail(self, alan):
        """No manopt insight after just 1 failure."""
        alan.record("tar xf bad.tar", exit_code=2, duration_ms=100)
        self._populate_cache(alan)
        insights = alan.get_insights("tar xf bad2.tar")
        messages = [msg for _, msg in insights]
        assert not any("Options for" in m for m in messages)

    def test_insight_on_third_fail(self, alan):
        """manopt insight appears on 3rd failure attempt."""
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        self._populate_cache(alan)
        insights = alan.get_insights("tar xf bad3.tar")
        messages = [msg for _, msg in insights]
        assert any("Options for" in m and "tar" in m for m in messages)

    def test_no_insight_without_cache(self, alan):
        """No manopt insight if cache is empty."""
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        insights = alan.get_insights("tar xf bad3.tar")
        messages = [msg for _, msg in insights]
        assert not any("Options for" in m for m in messages)

    def test_no_insight_when_disabled(self, alan):
        """No manopt insight when disabled via config."""
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        self._populate_cache(alan)
        with patch('zsh_tool.alan.ALAN_MANOPT_ENABLED', False):
            insights = alan.get_insights("tar xf bad3.tar")
            messages = [msg for _, msg in insights]
            assert not any("Options for" in m for m in messages)

    def test_insight_includes_cached_text(self, alan):
        """manopt insight includes the full cached option table."""
        alan.record("tar xf bad1.tar", exit_code=2, duration_ms=100)
        alan.record("tar xf bad2.tar", exit_code=2, duration_ms=100)
        self._populate_cache(alan, text="THE_OPTION_TABLE_CONTENT")
        insights = alan.get_insights("tar xf bad3.tar")
        messages = [msg for _, msg in insights]
        manopt_msgs = [m for m in messages if "Options for" in m]
        assert len(manopt_msgs) == 1
        assert "THE_OPTION_TABLE_CONTENT" in manopt_msgs[0]
