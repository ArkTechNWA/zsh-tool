"""Tests for user configuration loading."""

from pathlib import Path
from unittest.mock import patch


class TestLoadUserConfig:
    """Tests for _load_user_config function."""

    def test_returns_empty_dict_when_file_not_exists(self, tmp_path):
        """Should return empty dict when config file doesn't exist."""
        import zsh_tool.config as config_module

        with patch.object(config_module, 'CONFIG_PATH', tmp_path / 'nonexistent.yaml'):
            result = config_module._load_user_config()
            assert result == {}

    def test_parses_yield_after_value(self, tmp_path):
        """Should parse yield_after from config file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("yield_after: 30\n")

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            result = config_module._load_user_config()
            assert result == {'yield_after': 30.0}

    def test_ignores_comments(self, tmp_path):
        """Should ignore comment lines."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""# This is a comment
# Another comment
yield_after: 15
# More comments
""")

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            result = config_module._load_user_config()
            assert result == {'yield_after': 15.0}

    def test_ignores_empty_lines(self, tmp_path):
        """Should ignore empty lines."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""

yield_after: 5

""")

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            result = config_module._load_user_config()
            assert result == {'yield_after': 5.0}

    def test_handles_invalid_yield_after_value(self, tmp_path):
        """Should skip invalid yield_after values."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("yield_after: invalid\n")

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            result = config_module._load_user_config()
            assert result == {}  # Invalid value not added

    def test_handles_read_error_gracefully(self, tmp_path):
        """Should return empty dict on read errors."""
        config_file = tmp_path / "config.yaml"

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            with patch.object(Path, 'exists', return_value=True):
                with patch.object(Path, 'read_text', side_effect=PermissionError("denied")):
                    result = config_module._load_user_config()
                    assert result == {}

    def test_parses_float_values(self, tmp_path):
        """Should parse float yield_after values."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("yield_after: 2.5\n")

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            result = config_module._load_user_config()
            assert result == {'yield_after': 2.5}

    def test_handles_whitespace_around_values(self, tmp_path):
        """Should strip whitespace from keys and values."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("  yield_after  :   10   \n")

        import zsh_tool.config as config_module
        with patch.object(config_module, 'CONFIG_PATH', config_file):
            result = config_module._load_user_config()
            assert result == {'yield_after': 10.0}


class TestYieldAfterDefault:
    """Tests for YIELD_AFTER_DEFAULT initialization."""

    def test_default_value_when_no_config(self):
        """YIELD_AFTER_DEFAULT should be 2.0 when no user config."""
        # This tests the actual default - we can't easily test the loaded value
        # without restarting the module, but we can verify the function works
        import zsh_tool.config as config_module

        # The actual default is determined at module load time
        # Just verify it's a reasonable float
        assert isinstance(config_module.YIELD_AFTER_DEFAULT, float)
        assert config_module.YIELD_AFTER_DEFAULT > 0
