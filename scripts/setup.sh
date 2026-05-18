#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
DNSMASQ_CONF="$ROOT_DIR/dnsmasq/dnsmasq.conf"

APP_PORT="8000"
FILES_PORT="8080"
HOST_IP=""
PXE_NETWORK=""
NETMASK="255.255.255.0"
DHCP_MODE="auto"
DHCP_INTERFACE=""
DHCP_DETECT_TIMEOUT="5"
DHCP_RANGE_START=""
DHCP_RANGE_END=""
DHCP_ROUTER=""
DHCP_DNS=""
DHCP_DOMAIN=""
DHCP_LEASE_TIME="12h"
ADMIN_USER="admin"
ADMIN_PASSWORD=""
SECRET_KEY=""
POSTGRES_DB="pxeapp"
POSTGRES_USER="pxe"
POSTGRES_PASSWORD=""
UNKNOWN_HOST_POLICY="menu"
FORCE="false"
START="false"
PREPARE_MEDIA="false"
MEDIA_DOWNLOAD_BOOTLOADERS="true"
MEDIA_IMPORT_DESKTOPS="true"
MEDIA_IMPORT_SERVERS="true"
MEDIA_UBUNTU_VERSIONS="22 24 26"
MEDIA_REPLACE="true"
WINDOWS_ISO=""
WINDOWS_URL=""
WINDOWS_SHA256=""
WIMBOOT=""

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [options]

Creates .env, dnsmasq/dnsmasq.conf, and required data directories.

Options:
  --host-ip IP              PXE host IP advertised to clients.
  --pxe-network NETWORK    ProxyDHCP network address, e.g. 192.168.10.0.
  --netmask NETMASK        PXE VLAN netmask. Default: 255.255.255.0.
  --dhcp-mode MODE          auto, proxy, or server. Default: auto.
                            auto probes for DHCP; proxy if found, server if none found.
  --dhcp-interface IFACE    Interface used for DHCP probing and dnsmasq binding.
  --dhcp-detect-timeout SEC DHCP probe timeout for auto mode. Default: 5.
  --dhcp-range-start IP     Start IP when running full DHCP server mode.
  --dhcp-range-end IP       End IP when running full DHCP server mode.
  --dhcp-router IP          Router option for full DHCP server mode.
  --dhcp-dns IPS            DNS option for full DHCP server mode, comma-separated.
  --dhcp-domain DOMAIN      Domain-name option for full DHCP server mode.
  --dhcp-lease-time TIME    dnsmasq lease time for server mode. Default: 12h.
  --ui-port PORT           Host/container port for the operator UI and PXE app. Default: 8000.
  --app-port PORT          Alias for --ui-port.
  --files-port PORT        Host port for nginx OS boot files. Default: 8080.
  --admin-user USER        Initial admin username. Default: admin.
  --admin-password PASS    Initial admin password. Generated if omitted.
  --secret-key KEY         Session signing key. Generated if omitted.
  --postgres-db NAME       Postgres database name. Default: pxeapp.
  --postgres-user USER     Postgres username. Default: pxe.
  --postgres-password PASS Postgres password. Generated if omitted.
  --unknown-policy POLICY  menu, register, or localboot. Default: menu.
  --prepare-media          After --start, download/import bootloaders and OS media into tftproot/.
  --media-ubuntu-versions LIST
                           Space-separated Ubuntu aliases for media prep. Default: "22 24 26".
  --no-media-bootloaders   Skip downloading undionly.kpxe, ipxe.efi, and wimboot.
  --no-ubuntu-desktops     Skip Ubuntu Desktop media prep.
  --no-ubuntu-servers      Skip Ubuntu Server media prep.
  --windows-iso PATH       Windows 11 ISO to import during media prep. Use a path under this repo.
  --windows-url URL        Official Microsoft temporary Windows 11 ISO URL to download/import.
  --windows-sha256 HASH    Expected Windows 11 ISO SHA256.
  --wimboot PATH           Existing wimboot path for Windows import. Defaults to tftproot/windows/wimboot.
  --no-media-replace       Do not update existing Image rows during media prep.
  --force                  Overwrite existing .env and dnsmasq/dnsmasq.conf.
  --start                  Run docker compose up -d --build after setup.
  -h, --help               Show this help.

Example:
  scripts/setup.sh --host-ip 192.168.10.5 --pxe-network 192.168.10.0 --ui-port 9000
EOF
}

fail() {
  echo "error: $*" >&2
  exit 1
}

need_value() {
  [ "$#" -ge 2 ] || fail "$1 requires a value"
  printf '%s' "$2"
}

generate_secret() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
}

generate_password() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(18))'
}

dotenv_quote() {
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

database_url() {
  python3 - "$POSTGRES_USER" "$POSTGRES_PASSWORD" "$POSTGRES_DB" <<'PY'
from urllib.parse import quote
import sys
user, password, database = sys.argv[1:]
print(
    "postgresql+psycopg://"
    f"{quote(user, safe='')}:{quote(password, safe='')}@postgres:5432/{quote(database, safe='')}"
)
PY
}

detect_host_ip() {
  if command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | awk '{print $1}' || true
  fi
}

default_network_for_ip() {
  awk -F. 'NF == 4 {print $1 "." $2 "." $3 ".0"}' <<<"$1"
}

validate_port() {
  local name="$1"
  local port="$2"
  [[ "$port" =~ ^[0-9]+$ ]] || fail "$name must be numeric"
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail "$name must be between 1 and 65535"
}

default_dhcp_range() {
  python3 - "$PXE_NETWORK" "$NETMASK" <<'PY'
from app.dhcp_detect import default_dhcp_range
import sys
start, end = default_dhcp_range(sys.argv[1], sys.argv[2])
print(start)
print(end)
PY
}

detect_dhcp() {
  local args=(python3 -m app.dhcp_detect --timeout "$DHCP_DETECT_TIMEOUT")
  if [ -n "$DHCP_INTERFACE" ]; then
    args+=(--interface "$DHCP_INTERFACE")
  fi
  (cd "$ROOT_DIR" && "${args[@]}" >/tmp/pxe-app-dhcp-detect.out 2>/tmp/pxe-app-dhcp-detect.err)
}

write_dnsmasq_common() {
  cat <<EOF
# dnsmasq for pxe-app.
# Generated by scripts/setup.sh.
port=0
log-dhcp
log-facility=-
enable-tftp
tftp-root=/var/lib/tftpboot
EOF
  if [ -n "$DHCP_INTERFACE" ]; then
    cat <<EOF
interface=$DHCP_INTERFACE
bind-interfaces
EOF
  fi
}

write_dnsmasq_boot_options() {
  cat <<EOF

# PXE client architecture types:
# 0 = BIOS, 7/9 = x86_64 UEFI variants.
dhcp-match=set:bios,option:client-arch,0
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efi64,option:client-arch,9
dhcp-userclass=set:ipxe,iPXE

# First-stage bootloader from TFTP. Use --prepare-media or place these files in tftproot/.
dhcp-boot=tag:!ipxe,tag:efi64,ipxe.efi
dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe

# In proxy mode, pxe-service is required for dnsmasq to actually emit the
# proxy-DHCPOFFER; dhcp-boot alone is silently ignored for UEFI clients.
pxe-service=tag:!ipxe,X86PC,"PXE boot (BIOS)",undionly.kpxe
pxe-service=tag:!ipxe,X86-64_EFI,"PXE boot (UEFI x64)",ipxe.efi
pxe-service=tag:!ipxe,BC_EFI,"PXE boot (UEFI x64 alt)",ipxe.efi

# Once iPXE is running, fetch a script from pxe-app over HTTP.
dhcp-boot=tag:ipxe,http://$HOST_IP:$APP_PORT/boot.ipxe
EOF
}

container_visible_path() {
  local option_name="$1"
  local path="$2"
  if [[ "$path" = /* ]]; then
    case "$path" in
      "$ROOT_DIR"/*)
        printf '%s' "${path#$ROOT_DIR/}"
        ;;
      *)
        fail "$option_name must be inside $ROOT_DIR when used with --prepare-media;" \
          "copy it under data/isos/ or use --windows-url"
        ;;
    esac
  else
    printf '%s' "$path"
  fi
}

build_prepare_media_args() {
  PREPARE_MEDIA_ARGS=()
  PREPARE_MEDIA_ARGS+=(--ubuntu-versions "$MEDIA_UBUNTU_VERSIONS")
  if [ "$MEDIA_DOWNLOAD_BOOTLOADERS" != "true" ]; then
    PREPARE_MEDIA_ARGS+=(--no-bootloaders)
  fi
  if [ "$MEDIA_IMPORT_DESKTOPS" != "true" ]; then
    PREPARE_MEDIA_ARGS+=(--no-ubuntu-desktops)
  fi
  if [ "$MEDIA_IMPORT_SERVERS" != "true" ]; then
    PREPARE_MEDIA_ARGS+=(--no-ubuntu-servers)
  fi
  if [ "$MEDIA_REPLACE" != "true" ]; then
    PREPARE_MEDIA_ARGS+=(--no-replace)
  fi
  if [ -n "$WINDOWS_ISO" ]; then
    PREPARE_MEDIA_ARGS+=(
      --windows-iso "$(container_visible_path "--windows-iso" "$WINDOWS_ISO")"
    )
  fi
  if [ -n "$WINDOWS_URL" ]; then
    PREPARE_MEDIA_ARGS+=(--windows-url "$WINDOWS_URL")
  fi
  if [ -n "$WINDOWS_SHA256" ]; then
    PREPARE_MEDIA_ARGS+=(--windows-sha256 "$WINDOWS_SHA256")
  fi
  if [ -n "$WIMBOOT" ]; then
    PREPARE_MEDIA_ARGS+=(
      --wimboot "$(container_visible_path "--wimboot" "$WIMBOOT")"
    )
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --host-ip)
      HOST_IP="$(need_value "$@")"
      shift 2
      ;;
    --pxe-network)
      PXE_NETWORK="$(need_value "$@")"
      shift 2
      ;;
    --netmask)
      NETMASK="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-mode)
      DHCP_MODE="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-interface)
      DHCP_INTERFACE="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-detect-timeout)
      DHCP_DETECT_TIMEOUT="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-range-start)
      DHCP_RANGE_START="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-range-end)
      DHCP_RANGE_END="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-router)
      DHCP_ROUTER="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-dns)
      DHCP_DNS="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-domain)
      DHCP_DOMAIN="$(need_value "$@")"
      shift 2
      ;;
    --dhcp-lease-time)
      DHCP_LEASE_TIME="$(need_value "$@")"
      shift 2
      ;;
    --ui-port|--app-port)
      APP_PORT="$(need_value "$@")"
      shift 2
      ;;
    --files-port)
      FILES_PORT="$(need_value "$@")"
      shift 2
      ;;
    --admin-user)
      ADMIN_USER="$(need_value "$@")"
      shift 2
      ;;
    --admin-password)
      ADMIN_PASSWORD="$(need_value "$@")"
      shift 2
      ;;
    --secret-key)
      SECRET_KEY="$(need_value "$@")"
      shift 2
      ;;
    --postgres-db)
      POSTGRES_DB="$(need_value "$@")"
      shift 2
      ;;
    --postgres-user)
      POSTGRES_USER="$(need_value "$@")"
      shift 2
      ;;
    --postgres-password)
      POSTGRES_PASSWORD="$(need_value "$@")"
      shift 2
      ;;
    --unknown-policy)
      UNKNOWN_HOST_POLICY="$(need_value "$@")"
      shift 2
      ;;
    --prepare-media)
      PREPARE_MEDIA="true"
      shift
      ;;
    --media-ubuntu-versions)
      MEDIA_UBUNTU_VERSIONS="$(need_value "$@")"
      shift 2
      ;;
    --no-media-bootloaders)
      MEDIA_DOWNLOAD_BOOTLOADERS="false"
      shift
      ;;
    --no-ubuntu-desktops)
      MEDIA_IMPORT_DESKTOPS="false"
      shift
      ;;
    --no-ubuntu-servers)
      MEDIA_IMPORT_SERVERS="false"
      shift
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
    --no-media-replace)
      MEDIA_REPLACE="false"
      shift
      ;;
    --force)
      FORCE="true"
      shift
      ;;
    --start)
      START="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

validate_port "--ui-port" "$APP_PORT"
validate_port "--files-port" "$FILES_PORT"

if [ "$PREPARE_MEDIA" = "true" ] && [ "$START" != "true" ]; then
  fail "--prepare-media requires --start so Postgres is running before Image rows are created"
fi

case "$DHCP_MODE" in
  auto|proxy|server) ;;
  *) fail "--dhcp-mode must be auto, proxy, or server" ;;
esac

case "$UNKNOWN_HOST_POLICY" in
  menu|register|localboot) ;;
  *) fail "--unknown-policy must be menu, register, or localboot" ;;
esac

if [ -z "$HOST_IP" ]; then
  HOST_IP="$(detect_host_ip)"
fi
[ -n "$HOST_IP" ] || fail "could not detect host IP; pass --host-ip"

if [ -z "$PXE_NETWORK" ]; then
  PXE_NETWORK="$(default_network_for_ip "$HOST_IP")"
fi
[ -n "$PXE_NETWORK" ] || fail "could not infer PXE network; pass --pxe-network"

if [ "$DHCP_MODE" = "auto" ]; then
  echo "Probing for an existing DHCP server..."
  if detect_dhcp; then
    DHCP_MODE="proxy"
    echo "Existing DHCP server detected; generating proxyDHCP config."
    cat /tmp/pxe-app-dhcp-detect.out || true
  else
    status=$?
    if [ "$status" -eq 1 ]; then
      DHCP_MODE="server"
      echo "No DHCP server detected; generating full DHCP server config."
    else
      cat /tmp/pxe-app-dhcp-detect.err >&2 || true
      fail "DHCP auto-detection failed. Re-run with sudo, or pass --dhcp-mode proxy/server explicitly."
    fi
  fi
fi

if [ "$DHCP_MODE" = "server" ] && { [ -z "$DHCP_RANGE_START" ] || [ -z "$DHCP_RANGE_END" ]; }; then
  mapfile -t range_values < <(default_dhcp_range)
  DHCP_RANGE_START="${DHCP_RANGE_START:-${range_values[0]}}"
  DHCP_RANGE_END="${DHCP_RANGE_END:-${range_values[1]}}"
fi

if [ "$FORCE" != "true" ] && [ -f "$ENV_FILE" ]; then
  fail "$ENV_FILE already exists; pass --force to overwrite"
fi

SECRET_KEY="${SECRET_KEY:-$(generate_secret)}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(generate_password)}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(generate_secret)}"

mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/tftproot" "$ROOT_DIR/dnsmasq" "$ROOT_DIR/ansible/playbooks"

umask 077
cat >"$ENV_FILE" <<EOF
PXE_ENVIRONMENT=production
PXE_SECRET_KEY=$(dotenv_quote "$SECRET_KEY")
PXE_POSTGRES_DB=$POSTGRES_DB
PXE_POSTGRES_USER=$POSTGRES_USER
PXE_POSTGRES_PASSWORD=$(dotenv_quote "$POSTGRES_PASSWORD")
PXE_DATABASE_URL=$(database_url)
PXE_LISTEN_HOST=0.0.0.0
PXE_LISTEN_PORT=$APP_PORT
PXE_APP_PORT=$APP_PORT
PXE_FILES_PORT=$FILES_PORT
PXE_PUBLIC_BASE_URL=http://$HOST_IP:$APP_PORT
PXE_FILES_BASE_URL=http://$HOST_IP:$FILES_PORT
PXE_ANSIBLE_PLAYBOOKS_DIR=ansible/playbooks
PXE_ANSIBLE_WORK_DIR=data/ansible-runs
PXE_DHCP_MODE=$DHCP_MODE
PXE_UNKNOWN_HOST_POLICY=$UNKNOWN_HOST_POLICY
PXE_INITIAL_ADMIN_USERNAME=$ADMIN_USER
PXE_INITIAL_ADMIN_PASSWORD=$(dotenv_quote "$ADMIN_PASSWORD")
PXE_SESSION_COOKIE_SECURE=false
EOF
chmod 600 "$ENV_FILE"

{
  write_dnsmasq_common
  if [ "$DHCP_MODE" = "proxy" ]; then
    cat <<EOF

# Existing DHCP server detected/configured. dnsmasq runs as proxyDHCP only.
dhcp-range=$PXE_NETWORK,proxy,$NETMASK
EOF
  else
    cat <<EOF

# No existing DHCP server configured. dnsmasq serves addresses for the PXE VLAN.
dhcp-authoritative
dhcp-range=$DHCP_RANGE_START,$DHCP_RANGE_END,$NETMASK,$DHCP_LEASE_TIME
EOF
    if [ -n "$DHCP_ROUTER" ]; then
      echo "dhcp-option=option:router,$DHCP_ROUTER"
    fi
    if [ -n "$DHCP_DNS" ]; then
      echo "dhcp-option=option:dns-server,$DHCP_DNS"
    fi
    if [ -n "$DHCP_DOMAIN" ]; then
      echo "dhcp-option=option:domain-name,$DHCP_DOMAIN"
    fi
  fi
  write_dnsmasq_boot_options
} >"$DNSMASQ_CONF"

cat <<EOF
Setup complete.

UI URL:        http://$HOST_IP:$APP_PORT
Files URL:     http://$HOST_IP:$FILES_PORT
Admin user:    $ADMIN_USER
Admin pass:    $ADMIN_PASSWORD
Postgres DB:   $POSTGRES_DB
Postgres user: $POSTGRES_USER
PXE network:   $PXE_NETWORK/$NETMASK
DHCP mode:     $DHCP_MODE
DHCP range:    ${DHCP_RANGE_START:-proxyDHCP only}${DHCP_RANGE_END:+-$DHCP_RANGE_END}
Media prep:    $PREPARE_MEDIA
Start stack:   $START
EOF

if [ "$PREPARE_MEDIA" = "true" ]; then
  cat <<'EOF'
Next:
  Media preparation will run in Docker after Postgres, pxe-app, and pxe-files start.
EOF
elif [ "$START" = "true" ]; then
  cat <<'EOF'
Next:
  The stack will start now. Add boot files manually under tftproot/, or run:
     docker compose run --rm pxe-app scripts/prepare_media.sh --windows-url '<official-windows-11-url>'
EOF
else
  cat <<'EOF'
Next:
  1. Put undionly.kpxe, ipxe.efi, and OS boot files under tftproot/, or after starting run:
     docker compose run --rm pxe-app scripts/prepare_media.sh --windows-url '<official-windows-11-url>'
  2. Start with: docker compose up --build
EOF
fi

if [ "$START" = "true" ]; then
  cd "$ROOT_DIR"
  if [ "$PREPARE_MEDIA" = "true" ]; then
    build_prepare_media_args
    echo "Starting Postgres, pxe-app, and pxe-files before media preparation..."
    docker compose up -d --build postgres pxe-app pxe-files
    echo "Preparing iPXE bootloaders and OS media under tftproot/..."
    docker compose run --rm pxe-app scripts/prepare_media.sh "${PREPARE_MEDIA_ARGS[@]}"
    echo "Starting dnsmasq after media preparation..."
    docker compose up -d dnsmasq
  else
    docker compose up -d --build
  fi
fi
