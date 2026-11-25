import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_settings_load_from_any_working_directory(tmp_path):
    """The MCP server is started by another program, from another folder."""
    result = subprocess.run(
        [sys.executable, "-c", "from app.config import settings; print(bool(settings.database_url))"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "True", result.stderr
