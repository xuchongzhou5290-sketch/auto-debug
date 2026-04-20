#!/bin/sh
set -eu

BASE_URL="${BASE_URL:-http://127.0.0.1:8765}"
DEFAULT_WORKSPACE='/mnt/sdcard/autodbg'
WORKSPACE="${WORKSPACE:-$DEFAULT_WORKSPACE}"
MANIFEST_NAME="${MANIFEST_NAME:-autodbg-manifest.json}"

fetch_to_file() {
  url="$1"
  dest="$2"
  mkdir -p "$(dirname "$dest")"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
    return 0
  fi
  if command -v wget >/dev/null 2>&1 && wget --help >/dev/null 2>&1; then
    wget -q -O "$dest" "$url"
    return 0
  fi
  if command -v busybox >/dev/null 2>&1 && busybox --list 2>/dev/null | grep -qx 'wget'; then
    busybox wget -q -O "$dest" "$url"
    return 0
  fi
  echo "No downloader available (curl/wget/busybox wget)." >&2
  return 127
}

sha256_of() {
  target="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$target" | awk '{print $1}'
    return 0
  fi
  if command -v busybox >/dev/null 2>&1; then
    busybox sha256sum "$target" | awk '{print $1}'
    return 0
  fi
  return 127
}

verify_file() {
  target="$1"
  expected="$2"
  if actual="$(sha256_of "$target" 2>/dev/null)"; then
    if [ "$actual" != "$expected" ]; then
      echo "SHA256 mismatch: $target" >&2
      return 2
    fi
    return 0
  fi
  echo "WARN: sha256 tool unavailable, skip verify for $target" >&2
  return 0
}

mkdir -p "$WORKSPACE"
fetch_to_file "$BASE_URL/$MANIFEST_NAME" "$WORKSPACE/$MANIFEST_NAME"
fetch_to_file "$BASE_URL/hello.txt" "$WORKSPACE/hello.txt"
verify_file "$WORKSPACE/hello.txt" "266143c8ca32d8684f31dd7a0e953d20a9f90029cc5d2a77c5cb229d87371aac"
printf 'AUTODBG_PULL_OK workspace=%s files=1\n' "$WORKSPACE"
