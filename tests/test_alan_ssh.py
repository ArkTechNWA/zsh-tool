"""
Tests for A.L.A.N. SSH tracking functionality (Issue #2).

Tests SSH command parsing, exit code classification, and dual recording.
"""

import pytest
import tempfile
from pathlib import Path

# Import the ALAN class
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "zsh_tool"))
from server import ALAN


@pytest.fixture
def alan():
    """Create a fresh A.L.A.N. instance with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    instance = ALAN(db_path)
    yield instance
    # Cleanup
    db_path.unlink(missing_ok=True)


class TestParseSSHCommand:
    """Tests for _parse_ssh_command()"""

    def test_simple_ssh(self, alan):
        """ssh hostname"""
        result = alan._parse_ssh_command("ssh myhost")
        assert result is not None
        assert result['host'] == 'myhost'
        assert result['remote_command'] is None
        assert result['user'] is None
        assert result['port'] is None

    def test_ssh_with_user_at(self, alan):
        """ssh user@hostname"""
        result = alan._parse_ssh_command("ssh admin@server.example.com")
        assert result is not None
        assert result['host'] == 'server.example.com'
        assert result['user'] == 'admin'
        assert result['remote_command'] is None

    def test_ssh_with_remote_command(self, alan):
        """ssh hostname 'command'"""
        result = alan._parse_ssh_command("ssh myhost 'git pull'")
        assert result is not None
        assert result['host'] == 'myhost'
        assert result['remote_command'] == "'git pull'"

    def test_ssh_with_remote_command_multi_word(self, alan):
        """ssh hostname command with multiple words"""
        result = alan._parse_ssh_command("ssh myhost ls -la /var/log")
        assert result is not None
        assert result['host'] == 'myhost'
        assert result['remote_command'] == "ls -la /var/log"

    def test_ssh_with_port_flag(self, alan):
        """ssh -p port hostname"""
        result = alan._parse_ssh_command("ssh -p 2222 myhost")
        assert result is not None
        assert result['host'] == 'myhost'
        assert result['port'] == '2222'

    def test_ssh_with_user_flag(self, alan):
        """ssh -l user hostname"""
        result = alan._parse_ssh_command("ssh -l root server")
        assert result is not None
        assert result['host'] == 'server'
        assert result['user'] == 'root'

    def test_ssh_with_identity_flag(self, alan):
        """ssh -i keyfile hostname"""
        result = alan._parse_ssh_command("ssh -i ~/.ssh/id_rsa myhost")
        assert result is not None
        assert result['host'] == 'myhost'

    def test_ssh_verbose_flags(self, alan):
        """ssh -vvv hostname"""
        result = alan._parse_ssh_command("ssh -vvv myhost")
        assert result is not None
        assert result['host'] == 'myhost'

    def test_ssh_complex_command(self, alan):
        """ssh -p 22022 -i key user@host 'systemctl status nginx'"""
        result = alan._parse_ssh_command("ssh -p 22022 -i ~/.ssh/key admin@vps 'systemctl status nginx'")
        assert result is not None
        assert result['host'] == 'vps'
        assert result['user'] == 'admin'
        assert result['port'] == '22022'
        assert result['remote_command'] == "'systemctl status nginx'"

    def test_not_ssh_command(self, alan):
        """Non-SSH commands should return None"""
        assert alan._parse_ssh_command("ls -la") is None
        assert alan._parse_ssh_command("git push") is None
        assert alan._parse_ssh_command("echo ssh") is None
        assert alan._parse_ssh_command("sshd -t") is None

    def test_ssh_without_host(self, alan):
        """ssh with no host should return None"""
        result = alan._parse_ssh_command("ssh")
        assert result is None

    def test_ssh_only_flags_no_host(self, alan):
        """ssh -v -v should return None (no host)"""
        result = alan._parse_ssh_command("ssh -v -v")
        assert result is None


class TestClassifySSHExit:
    """Tests for _classify_ssh_exit()"""

    def test_success(self, alan):
        """Exit code 0 = success"""
        assert alan._classify_ssh_exit(0) == 'success'

    def test_connection_failed(self, alan):
        """Exit code 255 = connection failed"""
        assert alan._classify_ssh_exit(255) == 'connection_failed'

    def test_command_failed_various(self, alan):
        """Exit codes 1-254 = command failed"""
        assert alan._classify_ssh_exit(1) == 'command_failed'
        assert alan._classify_ssh_exit(2) == 'command_failed'
        assert alan._classify_ssh_exit(127) == 'command_failed'
        assert alan._classify_ssh_exit(254) == 'command_failed'

    def test_unknown_negative(self, alan):
        """Negative exit codes = unknown"""
        assert alan._classify_ssh_exit(-1) == 'unknown'

    def test_unknown_high(self, alan):
        """Exit codes > 255 = unknown"""
        assert alan._classify_ssh_exit(256) == 'unknown'


class TestSSHRecording:
    """Tests for SSH dual recording in record()"""

    def test_ssh_command_creates_ssh_observation(self, alan):
        """SSH commands should create entries in ssh_observations table."""
        alan.record("ssh myhost 'ls -la'", 0, 1500)

        with alan._connect() as conn:
            # Check main observations
            obs_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            assert obs_count == 1

            # Check SSH observations
            ssh_count = conn.execute("SELECT COUNT(*) FROM ssh_observations").fetchone()[0]
            assert ssh_count == 1

            ssh_row = conn.execute("SELECT * FROM ssh_observations").fetchone()
            assert ssh_row['host'] == 'myhost'
            assert ssh_row['remote_command'] == "'ls -la'"
            assert ssh_row['exit_code'] == 0
            assert ssh_row['exit_type'] == 'success'

    def test_non_ssh_command_no_ssh_observation(self, alan):
        """Non-SSH commands should not create SSH observations."""
        alan.record("ls -la", 0, 100)

        with alan._connect() as conn:
            obs_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            assert obs_count == 1

            ssh_count = conn.execute("SELECT COUNT(*) FROM ssh_observations").fetchone()[0]
            assert ssh_count == 0

    def test_ssh_connection_failure(self, alan):
        """SSH connection failure (exit 255) should be recorded correctly."""
        alan.record("ssh unreachable-host 'ls'", 255, 3000)

        with alan._connect() as conn:
            ssh_row = conn.execute("SELECT * FROM ssh_observations").fetchone()
            assert ssh_row['host'] == 'unreachable-host'
            assert ssh_row['exit_type'] == 'connection_failed'

    def test_ssh_command_failure(self, alan):
        """SSH command failure (exit 1-254) should be recorded correctly."""
        alan.record("ssh myhost 'false'", 1, 500)

        with alan._connect() as conn:
            ssh_row = conn.execute("SELECT * FROM ssh_observations").fetchone()
            assert ssh_row['exit_type'] == 'command_failed'

    def test_ssh_without_remote_command(self, alan):
        """SSH without remote command should still be recorded."""
        alan.record("ssh myhost", 0, 10000)

        with alan._connect() as conn:
            ssh_row = conn.execute("SELECT * FROM ssh_observations").fetchone()
            assert ssh_row['host'] == 'myhost'
            assert ssh_row['remote_command'] is None
            assert ssh_row['remote_command_template'] is None


class TestSSHStats:
    """Tests for SSH-specific statistics methods."""

    def test_get_ssh_host_stats_unknown(self, alan):
        """Unknown host should return known=False."""
        stats = alan.get_ssh_host_stats("unknown-host")
        assert stats['known'] is False
        assert stats['host'] == 'unknown-host'

    def test_get_ssh_host_stats_with_data(self, alan):
        """Host with data should return proper stats."""
        # Record several SSH commands to same host
        alan.record("ssh myhost 'ls'", 0, 100)
        alan.record("ssh myhost 'pwd'", 0, 50)
        alan.record("ssh myhost 'fail'", 1, 200)  # command failure
        alan.record("ssh myhost 'ls'", 255, 3000)  # connection failure

        stats = alan.get_ssh_host_stats("myhost")
        assert stats['known'] is True
        assert stats['total_connections'] == 4
        assert stats['successes'] == 2
        assert stats['connection_failures'] == 1
        assert stats['command_failures'] == 1
        assert stats['connection_success_rate'] == 0.75  # 3/4 connected
        assert stats['overall_success_rate'] == 0.5  # 2/4 fully succeeded

    def test_get_ssh_command_stats_unknown(self, alan):
        """Unknown command should return known=False."""
        stats = alan.get_ssh_command_stats("unknown-command")
        assert stats['known'] is False

    def test_get_ssh_command_stats_with_data(self, alan):
        """Command with data should return proper stats."""
        # Same command across different hosts (quotes become part of the remote_command)
        alan.record("ssh host1 'git pull'", 0, 1000)
        alan.record("ssh host2 'git pull'", 0, 1200)
        alan.record("ssh host3 'git pull'", 1, 800)  # command failed

        # The remote_command includes quotes, so template is based on "'git pull'"
        stats = alan.get_ssh_command_stats("'git pull'")
        assert stats['known'] is True
        assert stats['total_executions'] == 3
        assert stats['host_count'] == 3
        assert set(stats['hosts']) == {'host1', 'host2', 'host3'}
        assert stats['successes'] == 2
        assert stats['command_failures'] == 1


class TestSSHInsights:
    """Tests for SSH-specific insights in get_insights()."""

    def test_no_ssh_insights_for_non_ssh(self, alan):
        """Non-SSH commands shouldn't have SSH insights."""
        insights = alan.get_insights("ls -la")
        # Should have standard insights but nothing SSH-specific
        ssh_insights = [i for i in insights if 'Host' in i or 'Remote command' in i]
        assert len(ssh_insights) == 0

    def test_host_connection_failure_insight(self, alan):
        """High connection failure rate should trigger warning."""
        # Create a flaky host
        alan.record("ssh flaky 'ls'", 255, 3000)
        alan.record("ssh flaky 'ls'", 255, 3000)
        alan.record("ssh flaky 'ls'", 0, 500)

        insights = alan.get_insights("ssh flaky 'pwd'")
        connection_warnings = [i for i in insights if 'connection failure rate' in i]
        assert len(connection_warnings) > 0

    def test_reliable_host_insight(self, alan):
        """Reliable host should get positive insight."""
        for _ in range(4):
            alan.record("ssh reliable 'ls'", 0, 100)

        insights = alan.get_insights("ssh reliable 'pwd'")
        reliable_insights = [i for i in insights if 'reliable' in i.lower() and 'reliable' in i]
        assert len(reliable_insights) > 0

    def test_command_failure_insight(self, alan):
        """Command that fails often should trigger warning."""
        alan.record("ssh host1 'bad-cmd'", 1, 100)
        alan.record("ssh host2 'bad-cmd'", 1, 100)

        insights = alan.get_insights("ssh host3 'bad-cmd'")
        fail_insights = [i for i in insights if 'fails often' in i]
        assert len(fail_insights) > 0

    def test_reliable_remote_command_insight(self, alan):
        """Remote command that succeeds across multiple hosts should get positive insight."""
        # Run same command successfully across 3 different hosts
        alan.record("ssh host1 'git pull'", 0, 100)
        alan.record("ssh host2 'git pull'", 0, 100)
        alan.record("ssh host3 'git pull'", 0, 100)

        insights = alan.get_insights("ssh host4 'git pull'")
        reliable_insights = [i for i in insights if 'reliable across' in i and 'hosts' in i]
        assert len(reliable_insights) > 0


class TestPruneSSH:
    """Tests for SSH observation pruning."""

    def test_orphan_cleanup(self, alan):
        """SSH observations should be cleaned when parent observation is deleted."""
        alan.record("ssh myhost 'ls'", 0, 100)

        with alan._connect() as conn:
            # Verify both exist
            assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM ssh_observations").fetchone()[0] == 1

            # Manually delete the observation (simulating prune)
            conn.execute("DELETE FROM observations")

        # Run prune to clean up orphans
        alan.prune()

        with alan._connect() as conn:
            ssh_count = conn.execute("SELECT COUNT(*) FROM ssh_observations").fetchone()[0]
            assert ssh_count == 0, "Orphaned SSH observations should be cleaned up"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
