import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

CHECK = """
import connectors.base, connectors.http, connectors.registry, connectors.extract.jsonld
import pipeline.normalize, pipeline.fingerprint, pipeline.diff
connectors.registry.autodiscover()
leaked = sorted(m for m in sys.modules if m == "django" or m.startswith("django."))
assert not leaked, leaked
"""


def test_connectors_and_pipeline_import_without_django():
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import sys\n" + CHECK],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
