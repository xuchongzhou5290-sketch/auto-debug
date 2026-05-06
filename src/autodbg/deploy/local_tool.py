from __future__ import annotations

import ctypes
import os
import shutil
import stat
from pathlib import Path


LOCAL_TOOL_NAME = "auto-debug"
LOCAL_COPY_DIRS = [
    ".agents",
    "autodbg",
    "config",
    "docs",
    "knowledge",
    "payloads",
    "plugins",
    "profiles",
    "src",
    "workflows",
]
LOCAL_COPY_FILES = [
    ".gitattributes",
    ".gitignore",
    "README.md",
    "install-home-plugin.ps1",
    "observe-serial.ps1",
    "pyproject.toml",
]


def install_local_tool(
    *,
    project_root: Path,
    install_root: Path,
    include_venv: bool = True,
    include_local_settings: bool = True,
) -> dict[str, Path]:
    project_root = Path(project_root).absolute()
    install_root = Path(install_root).absolute()
    install_root.mkdir(parents=True, exist_ok=True)

    for relative in LOCAL_COPY_DIRS:
        source = project_root / relative
        if source.is_dir():
            if relative == "config" and not include_local_settings:
                _copy_tree_contents(source, install_root / relative, ignore_names={"user-settings.toml"})
            else:
                _copy_tree(source, install_root / relative)

    for relative in LOCAL_COPY_FILES:
        source = project_root / relative
        if source.is_file():
            _copy_file(source, install_root / relative)

    if include_venv:
        venv_source = project_root / ".venv"
        if venv_source.is_dir():
            _copy_tree(venv_source, install_root / ".venv")

    if include_local_settings:
        local_settings = project_root / "config" / "user-settings.toml"
        if local_settings.is_file():
            _copy_file(local_settings, install_root / "config" / "user-settings.toml")

    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    autodbg_cmd = bin_dir / "autodbg.cmd"
    observe_cmd = bin_dir / "observe-serial.cmd"
    install_plugin_cmd = bin_dir / "install-home-plugin.cmd"

    autodbg_cmd.write_text(_render_autodbg_cmd(), encoding="utf-8", newline="\n")
    observe_cmd.write_text(_render_observe_cmd(), encoding="utf-8", newline="\n")
    install_plugin_cmd.write_text(_render_install_home_plugin_cmd(), encoding="utf-8", newline="\n")

    return {
        "project_root": project_root,
        "install_root": install_root,
        "bin_dir": bin_dir,
        "autodbg_cmd": autodbg_cmd,
        "observe_cmd": observe_cmd,
        "install_home_plugin_cmd": install_plugin_cmd,
    }


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "artifacts", "retrieved", ".pytest_cache"),
    )


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _prepare_existing_file_for_replace(destination)
    shutil.copy2(source, destination)


def _prepare_existing_file_for_replace(path: Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
    path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.unlink()


def _copy_tree_contents(source: Path, destination: Path, *, ignore_names: set[str]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in ignore_names:
            continue
        target = destination / item.name
        if item.is_dir():
            _copy_tree(item, target)
        elif item.is_file():
            _copy_file(item, target)


def _render_autodbg_cmd() -> str:
    return "\n".join(
        [
            "@echo off",
            "setlocal",
            'for %%I in ("%~dp0..") do set "AUTO_DBG_HOME=%%~fI"',
            'set "AUTO_DBG_PROJECT_ROOT=%AUTO_DBG_HOME%"',
            'if defined PYTHONPATH (set "PYTHONPATH=%AUTO_DBG_HOME%;%AUTO_DBG_HOME%\\src;%PYTHONPATH%") else (set "PYTHONPATH=%AUTO_DBG_HOME%;%AUTO_DBG_HOME%\\src")',
            '"%AUTO_DBG_HOME%\\.venv\\Scripts\\python.exe" -m autodbg %*',
            "",
        ]
    )


def _render_observe_cmd() -> str:
    return "\n".join(
        [
            "@echo off",
            "setlocal",
            'for %%I in ("%~dp0..") do set "AUTO_DBG_HOME=%%~fI"',
            'set "AUTO_DBG_PROJECT_ROOT=%AUTO_DBG_HOME%"',
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%AUTO_DBG_HOME%\\observe-serial.ps1" %*',
            "",
        ]
    )


def _render_install_home_plugin_cmd() -> str:
    return "\n".join(
        [
            "@echo off",
            "setlocal",
            'for %%I in ("%~dp0..") do set "AUTO_DBG_HOME=%%~fI"',
            'set "AUTO_DBG_PROJECT_ROOT=%AUTO_DBG_HOME%"',
            'if defined PYTHONPATH (set "PYTHONPATH=%AUTO_DBG_HOME%;%AUTO_DBG_HOME%\\src;%PYTHONPATH%") else (set "PYTHONPATH=%AUTO_DBG_HOME%;%AUTO_DBG_HOME%\\src")',
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%AUTO_DBG_HOME%\\install-home-plugin.ps1" %*',
            "",
        ]
    )
