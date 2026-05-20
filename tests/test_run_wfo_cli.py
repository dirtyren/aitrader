import subprocess
import sys
from pathlib import Path


def test_run_wfo_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_wfo", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--settings" in result.stdout
