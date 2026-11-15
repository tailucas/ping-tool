import os
import sys
from pathlib import Path

# `app/__init__.py` reads APP_NAME at import time and the service refuses to
# import without it, so the environment must be primed before `app` is imported
# by any test module. Force the name so a developer's shell cannot change logger
# names or log destinations mid-test.
os.environ['APP_NAME'] = 'ping-tool-test'

# Never let a developer's real IPinfo token influence a test run: the tests that
# exercise `get_ip_org()` set their own value with `monkeypatch`.
os.environ.pop('IPINFO_TOKEN', None)

# The project is installed in editable mode by `uv sync`, but this keeps a bare
# `python -m pytest` working from a checkout that has not been synced yet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
