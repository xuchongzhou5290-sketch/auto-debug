from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).absolute().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autodbg.mcp.server import serve_stdio


raise SystemExit(serve_stdio(project_root=PROJECT_ROOT))
