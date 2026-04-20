from __future__ import annotations

import socket


def detect_host_ipv4(*, probe_host: str = "8.8.8.8", probe_port: int = 80) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
            handle.connect((probe_host, probe_port))
            candidate = handle.getsockname()[0]
        if candidate and not candidate.startswith("127."):
            return candidate
    except OSError:
        pass

    try:
        host_entries = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        host_entries = []
    for candidate in host_entries:
        if candidate and not candidate.startswith("127."):
            return candidate
    return None
