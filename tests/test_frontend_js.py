import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).parent / "js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node.js is not installed (only needed to test the frontend code).",
)


@pytest.mark.parametrize("script", ["markdown.test.js", "app.test.js"])
def test_frontend_script(script):
    result = subprocess.run(
        ["node", str(JS_DIR / script)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
