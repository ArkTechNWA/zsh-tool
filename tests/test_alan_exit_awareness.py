"""
Tests for ALAN post-execution insights (command awareness, pipe masking, silent detection).

Implements Sections 1 and 5 of feedback improvements design.
"""

from zsh_tool.alan import get_post_insights, KNOWN_EXIT_CODES, UNIVERSAL_EXIT_CODES


class TestKnownExitCodes:
    """Tests for the KNOWN_EXIT_CODES dict."""

    def test_grep_exit_1_is_known(self):
        assert "grep" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["grep"]

    def test_diff_exit_1_is_known(self):
        assert "diff" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["diff"]

    def test_test_exit_1_is_known(self):
        assert "test" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["test"]

    def test_bracket_exit_1_is_known(self):
        assert "[" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["["]

    def test_cmp_exit_1_is_known(self):
        assert "cmp" in KNOWN_EXIT_CODES
        assert 1 in KNOWN_EXIT_CODES["cmp"]


class TestUniversalExitCodes:
    """Tests for the UNIVERSAL_EXIT_CODES dict."""

    def test_126_permission_denied(self):
        assert 126 in UNIVERSAL_EXIT_CODES

    def test_127_command_not_found(self):
        assert 127 in UNIVERSAL_EXIT_CODES

    def test_255_ssh_connection_failed(self):
        assert 255 in UNIVERSAL_EXIT_CODES


class TestGetPostInsightsCommandAwareness:
    """Tests for command-specific exit code awareness."""

    def test_grep_exit_1_is_info(self):
        """grep exit 1 = no match, should be info not warning."""
        insights = get_post_insights("grep pattern file.txt", [1])
        assert any(level == "info" and "no match" in msg for level, msg in insights)

    def test_grep_exit_0_no_insight(self):
        """grep exit 0 = match found, no command awareness insight."""
        insights = get_post_insights("grep pattern file.txt", [0])
        awareness = [(lvl, m) for lvl, m in insights if "grep" in m and "exit" in m]
        assert len(awareness) == 0

    def test_grep_exit_2_no_known_insight(self):
        """grep exit 2 = error, not in known codes, no command awareness insight."""
        insights = get_post_insights("grep pattern file.txt", [2])
        awareness = [(lvl, m) for lvl, m in insights if "no match" in m]
        assert len(awareness) == 0

    def test_diff_exit_1_is_info(self):
        """diff exit 1 = files differ, should be info."""
        insights = get_post_insights("diff file1 file2", [1])
        assert any(level == "info" and "files differ" in msg for level, msg in insights)

    def test_test_exit_1_is_info(self):
        """test exit 1 = condition false, should be info."""
        insights = get_post_insights("test -f nofile", [1])
        assert any(level == "info" and "condition false" in msg for level, msg in insights)

    def test_exit_127_is_warning(self):
        """Exit 127 = command not found, always warning."""
        insights = get_post_insights("nonexistent_cmd", [127])
        assert any(level == "warning" and "command not found" in msg for level, msg in insights)

    def test_exit_126_is_warning(self):
        """Exit 126 = permission denied, always warning."""
        insights = get_post_insights("./noperm.sh", [126])
        assert any(level == "warning" and "permission denied" in msg for level, msg in insights)

    def test_exit_255_is_warning(self):
        """Exit 255 = SSH connection failed, always warning."""
        insights = get_post_insights("ssh badhost", [255])
        assert any(level == "warning" and "SSH connection failed" in msg for level, msg in insights)

    def test_universal_takes_precedence(self):
        """Universal exit codes take precedence over command-specific."""
        insights = get_post_insights("grep pattern", [127])
        assert any(level == "warning" and "command not found" in msg for level, msg in insights)
        assert not any("no match" in msg for _, msg in insights)

    def test_full_path_command(self):
        """Command with full path is recognized."""
        insights = get_post_insights("/usr/bin/grep pattern file", [1])
        assert any("no match" in msg for _, msg in insights)

    def test_unknown_command_no_awareness(self):
        """Unknown command with non-zero exit has no command awareness insight."""
        insights = get_post_insights("mycustom_cmd arg1", [1])
        awareness = [(lvl, m) for lvl, m in insights if "normal" in m]
        assert len(awareness) == 0


class TestGetPostInsightsPipeMasking:
    """Tests for pipe segment failure detection."""

    def test_left_failure_masked_by_right(self):
        """Left pipe segment failure masked by right success -> warning."""
        insights = get_post_insights("false | echo ok", [1, 0])
        assert any(level == "warning" and "pipe segment 1" in msg for level, msg in insights)

    def test_sigpipe_excluded(self):
        """Exit 141 (SIGPIPE) in left segment is normal in pipes, no warning."""
        insights = get_post_insights("cat bigfile | head -1", [141, 0])
        pipe_warnings = [(lvl, m) for lvl, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 0

    def test_all_zero_no_pipe_warning(self):
        """All pipe segments exit 0 -> no pipe masking warning."""
        insights = get_post_insights("echo hi | cat", [0, 0])
        pipe_warnings = [(lvl, m) for lvl, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 0

    def test_right_failure_no_masking_warning(self):
        """Right side failure is the overall exit, not masked. No masking warning."""
        insights = get_post_insights("echo hi | grep nope", [0, 1])
        pipe_warnings = [(lvl, m) for lvl, m in insights if "pipe segment" in m and "masked" in m]
        assert len(pipe_warnings) == 0

    def test_multiple_left_failures(self):
        """Multiple left segments failing -> warnings for each."""
        insights = get_post_insights("false | false | echo ok", [1, 1, 0])
        pipe_warnings = [(lvl, m) for lvl, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 2

    def test_single_command_no_pipe_analysis(self):
        """Single command (1-element pipestatus) has no pipe analysis."""
        insights = get_post_insights("false", [1])
        pipe_warnings = [(lvl, m) for lvl, m in insights if "pipe segment" in m]
        assert len(pipe_warnings) == 0


class TestGetPostInsightsSilentDetection:
    """Tests for silent command detection."""

    def test_no_output_exit_0_info(self):
        """Empty output + exit 0 -> info about no output."""
        insights = get_post_insights("true", [0], output="")
        assert any(level == "info" and "No output" in msg for level, msg in insights)

    def test_no_output_exit_nonzero_no_silent_insight(self):
        """Empty output + non-zero exit -> no silent insight (failure is the signal)."""
        insights = get_post_insights("false", [1], output="")
        silent = [(lvl, m) for lvl, m in insights if "No output" in m]
        assert len(silent) == 0

    def test_has_output_no_silent_insight(self):
        """Non-empty output -> no silent insight."""
        insights = get_post_insights("echo hello", [0], output="hello\n")
        silent = [(lvl, m) for lvl, m in insights if "No output" in m]
        assert len(silent) == 0

    def test_whitespace_only_counts_as_empty(self):
        """Whitespace-only output counts as empty."""
        insights = get_post_insights("true", [0], output="   \n\n  ")
        assert any(level == "info" and "No output" in msg for level, msg in insights)


class TestGetPostInsightsReturnType:
    """Tests for return type and edge cases."""

    def test_returns_list_of_tuples(self):
        insights = get_post_insights("echo hello", [0])
        assert isinstance(insights, list)
        for item in insights:
            assert isinstance(item, tuple) and len(item) == 2

    def test_levels_are_valid(self):
        insights = get_post_insights("nonexistent", [127])
        for level, _ in insights:
            assert level in ("info", "warning")

    def test_empty_command(self):
        """Empty command doesn't crash."""
        insights = get_post_insights("", [0])
        assert isinstance(insights, list)

    def test_empty_pipestatus(self):
        """Empty pipestatus list doesn't crash."""
        insights = get_post_insights("echo hi", [])
        assert isinstance(insights, list)

    def test_none_pipestatus(self):
        """None pipestatus doesn't crash."""
        insights = get_post_insights("echo hi", None)
        assert isinstance(insights, list)
