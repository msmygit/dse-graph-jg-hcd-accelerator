"""Entry-point tests: ``python -m dse_jg_migrator`` and the installed console script."""

from __future__ import annotations

import subprocess  # noqa: S404  # fixed argv only, no shell, no user input
import sys
from pathlib import Path


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603  # fixed argv


def test_python_dash_m_help() -> None:
    proc = _run([sys.executable, "-m", "dse_jg_migrator", "--help"])
    assert proc.returncode == 0, proc.stderr
    for cmd in ("seed", "assess", "analyse", "translate", "init-target", "migrate", "validate", "report", "run-all"):
        assert cmd in proc.stdout


def test_installed_console_script_help() -> None:
    script = Path(sys.executable).parent / "dse-jg-migrator"
    assert script.exists(), "console script not installed — run `pip install -e .`"
    proc = _run([str(script), "--help"])
    assert proc.returncode == 0, proc.stderr
    assert "Migration Accelerator" in proc.stdout


def test_main_module_invokes_cli_main() -> None:
    """Run __main__ in-process (the subprocess test above cannot be measured by coverage)."""
    import runpy
    from unittest.mock import patch

    with patch("dse_jg_migrator.cli.main") as cli_main:
        runpy.run_module("dse_jg_migrator", run_name="__main__")
    cli_main.assert_called_once_with()
