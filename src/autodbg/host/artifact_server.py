from __future__ import annotations

from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from autodbg.utils.hash import sha256_file
from autodbg.utils.process import pid_is_running

DEFAULT_HEALTH_NAME = "__autodbg_health.json"


@dataclass(slots=True)
class ArtifactServerRegistry:
    root: str
    bind: str
    port: int
    base_url: str
    health_url: str
    pid: int
    manifest_path: str
    pull_script_path: str
    log_path: str | None = None
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "bind": self.bind,
            "port": self.port,
            "base_url": self.base_url,
            "health_url": self.health_url,
            "pid": self.pid,
            "manifest_path": self.manifest_path,
            "pull_script_path": self.pull_script_path,
            "log_path": self.log_path,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArtifactServerRegistry":
        return cls(
            root=str(raw["root"]),
            bind=str(raw["bind"]),
            port=int(raw["port"]),
            base_url=str(raw["base_url"]),
            health_url=str(raw["health_url"]),
            pid=int(raw["pid"]),
            manifest_path=str(raw["manifest_path"]),
            pull_script_path=str(raw["pull_script_path"]),
            log_path=str(raw["log_path"]) if raw.get("log_path") else None,
            started_at=str(raw.get("started_at", "")),
        )


@dataclass(slots=True)
class ArtifactEntry:
    relative_path: str
    size_bytes: int
    sha256: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "url": self.url,
        }


def build_manifest(
    root: Path,
    *,
    base_url: str | None = None,
    manifest_name: str = "autodbg-manifest.json",
    exclude_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    root = root.resolve()
    excluded = {manifest_name, *exclude_names}
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in excluded:
            continue
        relative_path = path.relative_to(root).as_posix()
        entry = ArtifactEntry(
            relative_path=relative_path,
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
            url=f"{base_url.rstrip('/')}/{relative_path}" if base_url else None,
        )
        entries.append(entry.to_dict())
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": ".",
        "artifact_count": len(entries),
        "artifacts": entries,
    }


def write_manifest(
    root: Path,
    *,
    base_url: str | None = None,
    manifest_name: str = "autodbg-manifest.json",
    exclude_names: tuple[str, ...] = (),
) -> Path:
    import json

    root.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(
        root,
        base_url=base_url,
        manifest_name=manifest_name,
        exclude_names=exclude_names,
    )
    manifest_path = root / manifest_name
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return manifest_path


def write_pull_script(
    root: Path,
    *,
    base_url: str,
    workspace: str,
    script_name: str = "autodbg-pull.sh",
    manifest_name: str = "autodbg-manifest.json",
    exclude_names: tuple[str, ...] = (),
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(
        root,
        base_url=base_url,
        manifest_name=manifest_name,
        exclude_names=(script_name, *exclude_names),
    )
    script_path = root / script_name
    lines = [
        "#!/bin/sh",
        "set -eu",
        "",
        f"BASE_URL={_sh_single_quote(base_url.rstrip('/'))}",
        f"DEFAULT_WORKSPACE={_sh_single_quote(workspace)}",
        'WORKSPACE="${WORKSPACE:-$DEFAULT_WORKSPACE}"',
        f"MANIFEST_NAME={_sh_single_quote(manifest_name)}",
        "",
        "fetch_to_file() {",
        '  url="$1"',
        '  dest="$2"',
        '  mkdir -p "$(dirname "$dest")"',
        "  if command -v curl >/dev/null 2>&1; then",
        '    curl -fsSL "$url" -o "$dest"',
        "    return 0",
        "  fi",
        "  if command -v wget >/dev/null 2>&1 && wget --help >/dev/null 2>&1; then",
        '    wget -q -O "$dest" "$url"',
        "    return 0",
        "  fi",
        "  if command -v busybox >/dev/null 2>&1 && busybox --list 2>/dev/null | grep -qx 'wget'; then",
        '    busybox wget -q -O "$dest" "$url"',
        "    return 0",
        "  fi",
        '  echo "No downloader available (curl/wget/busybox wget)." >&2',
        "  return 127",
        "}",
        "",
        "sha256_of() {",
        '  target="$1"',
        "  if command -v sha256sum >/dev/null 2>&1; then",
        '    sha256sum "$target" | awk \'{print $1}\'',
        "    return 0",
        "  fi",
        "  if command -v busybox >/dev/null 2>&1; then",
        '    busybox sha256sum "$target" | awk \'{print $1}\'',
        "    return 0",
        "  fi",
        "  return 127",
        "}",
        "",
        "verify_file() {",
        '  target="$1"',
        '  expected="$2"',
        '  if actual="$(sha256_of "$target" 2>/dev/null)"; then',
        '    if [ "$actual" != "$expected" ]; then',
        '      echo "SHA256 mismatch: $target" >&2',
        "      return 2",
        "    fi",
        "    return 0",
        "  fi",
        '  echo "WARN: sha256 tool unavailable, skip verify for $target" >&2',
        "  return 0",
        "}",
        "",
        'mkdir -p "$WORKSPACE"',
        'fetch_to_file "$BASE_URL/$MANIFEST_NAME" "$WORKSPACE/$MANIFEST_NAME"',
    ]
    for artifact in manifest["artifacts"]:
        relative_path = artifact["relative_path"]
        lines.extend(
            [
                f'fetch_to_file "$BASE_URL/{relative_path}" "$WORKSPACE/{relative_path}"',
                f'verify_file "$WORKSPACE/{relative_path}" "{artifact["sha256"]}"',
            ]
        )
    lines.extend(
        [
            f'printf \'AUTODBG_PULL_OK workspace=%s files={manifest["artifact_count"]}\\n\' "$WORKSPACE"',
            "",
        ]
    )
    script_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return script_path


def serve_directory(
    root: Path,
    *,
    bind: str = "0.0.0.0",
    port: int = 8765,
    duration_seconds: float | None = None,
    health_name: str = DEFAULT_HEALTH_NAME,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    root = root.resolve()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    class ArtifactRequestHandler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            request_path = self.path.split("?", 1)[0].lstrip("/")
            if request_path == health_name.lstrip("/"):
                payload = {
                    "status": "ok",
                    "root": str(root),
                    "pid": os.getpid(),
                    "bind": bind,
                    "port": int(self.server.server_address[1]),
                    "started_at": started_at,
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    handler = lambda *args, **kwargs: ArtifactRequestHandler(*args, directory=str(root), **kwargs)
    server = ThreadingHTTPServer((bind, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if duration_seconds is not None:
        timer = threading.Timer(duration_seconds, server.shutdown)
        timer.daemon = True
        timer.start()
    return server, thread


def artifact_server_registry_root() -> Path:
    override = os.environ.get("AUTODBG_ARTIFACT_SERVER_REGISTRY_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "autodbg-artifact-servers"


def artifact_server_registry_path(port: int) -> Path:
    return artifact_server_registry_root() / f"artifact-server-{int(port)}.json"


def artifact_server_log_path(port: int) -> Path:
    return artifact_server_registry_root() / f"artifact-server-{int(port)}.log"


def write_artifact_server_registry(registry: ArtifactServerRegistry) -> Path:
    path = artifact_server_registry_path(registry.port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


def load_artifact_server_registry(port: int) -> ArtifactServerRegistry | None:
    path = artifact_server_registry_path(port)
    if not path.is_file():
        return None
    try:
        return ArtifactServerRegistry.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def list_artifact_server_registries() -> list[ArtifactServerRegistry]:
    root = artifact_server_registry_root()
    if not root.is_dir():
        return []
    registries: list[ArtifactServerRegistry] = []
    for path in sorted(root.glob("artifact-server-*.json")):
        try:
            registries.append(ArtifactServerRegistry.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return registries


def remove_artifact_server_registry(port: int) -> None:
    try:
        artifact_server_registry_path(port).unlink()
    except FileNotFoundError:
        return


def is_tcp_port_available(bind: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind, int(port)))
        return True
    except OSError:
        return False


def find_available_port(bind: str, preferred_port: int, *, attempts: int = 50) -> int:
    preferred_port = int(preferred_port)
    if preferred_port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((bind, 0))
            return int(sock.getsockname()[1])
    if preferred_port < 0 or preferred_port > 65535:
        raise ValueError(f"invalid TCP port: {preferred_port}")

    for offset in range(max(attempts, 1)):
        candidate = preferred_port + offset
        if candidate > 65535:
            break
        if is_tcp_port_available(bind, candidate):
            return candidate
    raise OSError(f"no free TCP port found near {preferred_port}")


def probe_http_url(url: str, *, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local tool health probe
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def artifact_server_pid_is_running(pid: int) -> bool:
    return pid_is_running(pid)


def stop_artifact_server(port: int, *, timeout_seconds: float = 3.0) -> dict[str, Any]:
    registry = load_artifact_server_registry(port)
    if registry is None:
        return {"status": "missing", "port": int(port)}

    if artifact_server_pid_is_running(registry.pid):
        try:
            os.kill(registry.pid, signal.SIGTERM)
        except OSError as exc:
            return {"status": "error", "port": int(port), "pid": registry.pid, "error": str(exc)}

        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        while time.monotonic() < deadline:
            if not artifact_server_pid_is_running(registry.pid):
                break
            time.sleep(0.1)

    remove_artifact_server_registry(port)
    return {"status": "stopped", "port": int(port), "pid": registry.pid}

def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
