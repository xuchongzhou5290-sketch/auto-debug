from __future__ import annotations

import os
from pathlib import Path
import sys


project_root_text = os.environ.get("AUTO_DBG_PROJECT_ROOT") or os.environ.get("AUTO_DBG_HOME")
PROJECT_ROOT = Path(project_root_text).absolute() if project_root_text else Path(__file__).absolute().parents[3]

if not PROJECT_ROOT.exists():
    raise SystemExit(f"AUTO_DBG_PROJECT_ROOT does not exist: {PROJECT_ROOT}")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autodbg.mcp.server import serve_stdio


raise SystemExit(serve_stdio(project_root=PROJECT_ROOT))
