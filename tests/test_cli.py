"""Smoke tests for the CLI using typer.testing.CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from anaplan_audit.cli import app

runner = CliRunner()


class TestCLI:
    """CLI smoke tests."""

    def test_version(self) -> None:
        """The version command prints version info."""
        from anaplan_audit import __version__

        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_validate_config_with_valid_file(self, tmp_path: Path) -> None:
        """validate-config succeeds with a valid settings file."""
        config = {
            "authenticationMode": "basic",
            "anaplanTenantName": "Test",
            "database": "test.db",
        }
        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps(config))

        result = runner.invoke(app, ["validate-config", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_validate_config_no_file(self) -> None:
        """validate-config with nonexistent file uses defaults."""
        result = runner.invoke(app, ["validate-config", "--config", "/tmp/nonexistent_config.json"])
        assert result.exit_code == 0

    def test_help(self) -> None:
        """--help shows all subcommands."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "register" in result.output
        assert "validate-config" in result.output
        assert "version" in result.output
