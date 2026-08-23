from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


_TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="refactor-agent-tests-"))
atexit.register(shutil.rmtree, _TEST_RUNTIME_ROOT, True)

# Application modules create the default API store during import. Keep that
# process-wide store isolated from a real product database in .runs.
os.environ["REFACTOR_AGENT_RUN_ROOT"] = str(_TEST_RUNTIME_ROOT / "runs")
os.environ["REFACTOR_AGENT_DATABASE"] = str(_TEST_RUNTIME_ROOT / "refactor_agent.sqlite")
os.environ["REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT"] = str(_TEST_RUNTIME_ROOT / "github-workspaces")
os.environ["NAILONG_DATA_DIR"] = str(_TEST_RUNTIME_ROOT / "nailong")
