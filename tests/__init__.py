"""Test package. Guard (P2.7 step C3b): the service's logs default to the host's real
logs folder, so every test run points them at a fresh temporary folder: the environment
for modules imported later, and `config.LOGS` itself if a test module imported dbrain
before this package ran (discovery without `-t .`). No test writes to the real folder."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DBRAIN_LOGS"] = tempfile.mkdtemp(prefix="dbrain-test-logs-")
if "dbrain.config" in sys.modules:
    sys.modules["dbrain.config"].LOGS = Path(os.environ["DBRAIN_LOGS"])
