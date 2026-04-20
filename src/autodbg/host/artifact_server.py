from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any


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
            sha256=_sha256_file(path),
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
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    root = root.resolve()
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(root), **kwargs)
    server = ThreadingHTTPServer((bind, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if duration_seconds is not None:
        timer = threading.Timer(duration_seconds, server.shutdown)
        timer.daemon = True
        timer.start()
    return server, thread


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
