from __future__ import annotations

from pathlib import Path


__all__ = ["__version__"]

__version__ = "0.1.0"


_PACKAGE_ROOT = Path(__file__).absolute().parent
_SRC_PACKAGE_ROOT = _PACKAGE_ROOT.parent / "src" / "autodbg"

__path__ = [str(_PACKAGE_ROOT)]
if _SRC_PACKAGE_ROOT.is_dir():
    __path__.append(str(_SRC_PACKAGE_ROOT))
