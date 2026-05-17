#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

DOWNLOAD_BOOTLOADERS="true"
IMPORT_DESKTOPS="true"
IMPORT_SERVERS="true"
IMPORT_DEBIAN="false"
IMPORT_ROCKY="false"
IMPORT_ALMA="false"
IMPORT_FEDORA="false"
UBUNTU_VERSIONS=("22" "24" "26")
DEBIAN_VERSIONS=("trixie")
ROCKY_VERSIONS=("9" "10")
ALMA_VERSIONS=("9" "10")
FEDORA_VERSIONS=("43")
WINDOWS_ISO=""
WINDOWS_URL=""
WINDOWS_SHA256=""
WIMBOOT=""
REPLACE="true"

usage() {
  cat <<'EOF'
Usage: scripts/prepare_media.sh [options]

Downloads iPXE bootloaders and imports Ubuntu/Windows ISO boot assets into tftproot/.

Defaults:
  - download undionly.kpxe, ipxe.efi, and wimboot
  - import Ubuntu Desktop 22/24/26
  - import Ubuntu Server 22/24/26
  - skip Windows unless --windows-iso or --windows-url is provided

Options:
  --ubuntu-versions LIST   Space-separated Ubuntu aliases. Default: "22 24 26".
  --no-bootloaders         Do not download iPXE/wimboot binaries.
  --no-ubuntu-desktops     Do not import Ubuntu Desktop ISOs.
  --no-ubuntu-servers      Do not import Ubuntu Server ISOs.
  --debian                 Import Debian releases (default: trixie).
  --debian-versions LIST   Codenames/versions for --debian.
  --rocky                  Import Rocky Linux (default: 9 10).
  --rocky-versions LIST    Versions for --rocky.
  --alma                   Import AlmaLinux (default: 9 10).
  --alma-versions LIST     Versions for --alma.
  --fedora                 Import Fedora (default: 43).
  --fedora-versions LIST   Versions for --fedora.
  --windows-iso PATH       Local Windows 11 ISO to import.
  --windows-url URL        Official Microsoft temporary ISO URL to download/import.
  --windows-sha256 HASH    Expected Windows ISO SHA256.
  --wimboot PATH           Existing wimboot path. Defaults to tftproot/wimboot after bootloader download.
  --no-replace             Do not update existing Image rows.
  -h, --help               Show this help.
EOF
}

need_value() {
  [ "$#" -ge 2 ] || { echo "error: $1 requires a value" >&2; exit 1; }
  printf '%s' "$2"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ubuntu-versions)
      read -r -a UBUNTU_VERSIONS <<<"$(need_value "$@")"
      shift 2
      ;;
    --no-bootloaders)
      DOWNLOAD_BOOTLOADERS="false"
      shift
      ;;
    --no-ubuntu-desktops)
      IMPORT_DESKTOPS="false"
      shift
      ;;
    --no-ubuntu-servers)
      IMPORT_SERVERS="false"
      shift
      ;;
    --debian)
      IMPORT_DEBIAN="true"
      shift
      ;;
    --debian-versions)
      read -r -a DEBIAN_VERSIONS <<<"$(need_value "$@")"
      IMPORT_DEBIAN="true"
      shift 2
      ;;
    --rocky)
      IMPORT_ROCKY="true"
      shift
      ;;
    --rocky-versions)
      read -r -a ROCKY_VERSIONS <<<"$(need_value "$@")"
      IMPORT_ROCKY="true"
      shift 2
      ;;
    --alma)
      IMPORT_ALMA="true"
      shift
      ;;
    --alma-versions)
      read -r -a ALMA_VERSIONS <<<"$(need_value "$@")"
      IMPORT_ALMA="true"
      shift 2
      ;;
    --fedora)
      IMPORT_FEDORA="true"
      shift
      ;;
    --fedora-versions)
      read -r -a FEDORA_VERSIONS <<<"$(need_value "$@")"
      IMPORT_FEDORA="true"
      shift 2
      ;;
    --windows-iso)
      WINDOWS_ISO="$(need_value "$@")"
      shift 2
      ;;
    --windows-url)
      WINDOWS_URL="$(need_value "$@")"
      shift 2
      ;;
    --windows-sha256)
      WINDOWS_SHA256="$(need_value "$@")"
      shift 2
      ;;
    --wimboot)
      WIMBOOT="$(need_value "$@")"
      shift 2
      ;;
    --no-replace)
      REPLACE="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      exit 1
      ;;
  esac
done

GLOBAL_ARGS=()
if [ "$REPLACE" = "true" ]; then
  GLOBAL_ARGS+=(--replace)
fi

if [ "$DOWNLOAD_BOOTLOADERS" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" bootloaders
fi

if [ "$IMPORT_DESKTOPS" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" ubuntu-desktops --versions "${UBUNTU_VERSIONS[@]}"
fi

if [ "$IMPORT_SERVERS" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" ubuntu-servers --versions "${UBUNTU_VERSIONS[@]}"
fi

if [ "$IMPORT_DEBIAN" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" debian-set --versions "${DEBIAN_VERSIONS[@]}"
fi

if [ "$IMPORT_ROCKY" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" rocky-set --versions "${ROCKY_VERSIONS[@]}"
fi

if [ "$IMPORT_ALMA" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" almalinux-set --versions "${ALMA_VERSIONS[@]}"
fi

if [ "$IMPORT_FEDORA" = "true" ]; then
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" fedora-set --versions "${FEDORA_VERSIONS[@]}"
fi

if [ -n "$WINDOWS_ISO" ] || [ -n "$WINDOWS_URL" ]; then
  WINDOWS_ARGS=()
  if [ -n "$WINDOWS_ISO" ]; then
    WINDOWS_ARGS+=(--iso "$WINDOWS_ISO")
  fi
  if [ -n "$WINDOWS_URL" ]; then
    WINDOWS_ARGS+=(--url "$WINDOWS_URL")
  fi
  if [ -n "$WINDOWS_SHA256" ]; then
    WINDOWS_ARGS+=(--sha256 "$WINDOWS_SHA256")
  fi
  if [ -n "$WIMBOOT" ]; then
    WINDOWS_ARGS+=(--wimboot "$WIMBOOT")
  fi
  "$PYTHON_BIN" -m app.media_import "${GLOBAL_ARGS[@]}" windows "${WINDOWS_ARGS[@]}"
else
  "$PYTHON_BIN" -m app.media_import windows-download-help
fi
