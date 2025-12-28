#!/usr/bin/env bash
# Proxmox VE host script: create a NEW LXC container and install Docker + Cobalt
# Tested approach for Docker-in-LXC:
# - privileged CT (UNPRIVILEGED=0)
# - features: nesting=1,keyctl=1,fuse=1
# - lxc.apparmor.profile: unconfined
# - lxc.mount.auto: proc:rw sys:rw
#
# Run on Proxmox host as root.

set -euo pipefail

# -----------------------------
# Defaults (override via env)
# -----------------------------
CT_HOSTNAME="${CT_HOSTNAME:-cobalt}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-1024}"   # MiB
SWAP="${SWAP:-512}"        # MiB
DISK_GB="${DISK_GB:-8}"    # GB
BRIDGE="${BRIDGE:-vmbr0}"
IPV4="${IPV4:-dhcp}"       # dhcp OR static like 192.168.2.204/24
GATEWAY="${GATEWAY:-}"     # required only for static
DNS_SERVERS="${DNS_SERVERS:-1.1.1.1 8.8.8.8}"

# Docker-in-LXC compatibility
UNPRIVILEGED="${UNPRIVILEGED:-0}"  # 0 recommended for Docker in LXC

# Cobalt
COBALT_IMAGE="${COBALT_IMAGE:-ghcr.io/imputnet/cobalt:latest}"
COBALT_PORT="${COBALT_PORT:-9000}"
BIND_IP="${BIND_IP:-0.0.0.0}"      # set to CT_IP (or bot IP) if you want to restrict exposure

# -----------------------------
# Pretty output helpers
# -----------------------------
_color() { local c="$1"; shift; printf "\033[%sm%s\033[0m\n" "$c" "$*"; }
info()  { _color "1;34" "ℹ️  $*"; }
ok()    { _color "1;32" "✅ $*"; }
warn()  { _color "1;33" "⚠️  $*"; }
fail()  { _color "1;31" "❌ $*"; exit 1; }

command -v pveversion >/dev/null 2>&1 || fail "This script must run on a Proxmox VE host."
[[ "$(id -u)" -eq 0 ]] || fail "Run as root on the Proxmox host."

info "Node: $(hostname)"

# Ensure host has FUSE module available (needed for /dev/fuse)
modprobe fuse >/dev/null 2>&1 || true

# Next CTID
CTID="${CTID:-$(pvesh get /cluster/nextid 2>/dev/null || true)}"
[[ -n "${CTID:-}" ]] || fail "Unable to get next CTID."
if pct status "$CTID" &>/dev/null; then
  fail "CTID $CTID already exists. Set CTID=... and retry."
fi

# Storage selection
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-$(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1{print $1}' | head -n1)}"
CT_STORAGE="${CT_STORAGE:-$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1{print $1}' | grep -E 'local-lvm|local' | head -n1)}"
[[ -n "$TEMPLATE_STORAGE" ]] || fail "No storage found with 'vztmpl' content."
[[ -n "$CT_STORAGE" ]] || fail "No storage found with 'rootdir' content (e.g., local-lvm/local)."

info "Template storage: $TEMPLATE_STORAGE"
info "Container storage: $CT_STORAGE"

# Debian 12 template
info "Updating LXC template list..."
pveam update >/dev/null 2>&1 || true

TEMPLATE="$(pveam available -section system 2>/dev/null | awk '{print $2}' | grep -E '^debian-12-standard_.*_amd64\.tar\.zst$' | sort -V | tail -n1)"
[[ -n "$TEMPLATE" ]] || fail "Could not find Debian 12 LXC template via pveam."
ok "Template selected: $TEMPLATE"

info "Downloading template (if not present): $TEMPLATE"
pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
TEMPLATE_REF="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"

# Network config
NET0="name=eth0,bridge=${BRIDGE},ip=${IPV4}"
if [[ "$IPV4" != "dhcp" ]]; then
  [[ -n "$GATEWAY" ]] || fail "Static IP selected but GATEWAY is empty. Set GATEWAY=..."
  NET0="${NET0},gw=${GATEWAY}"
fi

# Create CT (do not start yet)
info "Creating LXC CTID=$CTID hostname=$CT_HOSTNAME (unprivileged=$UNPRIVILEGED)..."
pct create "$CTID" "$TEMPLATE_REF" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" \
  --memory "$MEMORY" \
  --swap "$SWAP" \
  --rootfs "${CT_STORAGE}:${DISK_GB}" \
  --net0 "$NET0" \
  --nameserver "$(echo "$DNS_SERVERS" | tr ' ' ',')" \
  --features "nesting=1,keyctl=1,fuse=1" \
  --unprivileged "$UNPRIVILEGED" \
  --onboot 1 >/dev/null

ok "Container created."

# LXC config tweaks needed for Docker-in-LXC (sysctl permissions + AppArmor)
info "Applying LXC config tweaks for Docker-in-LXC..."
CONF_FILE="/etc/pve/lxc/${CTID}.conf"

if ! grep -q "^lxc.apparmor.profile: unconfined" "$CONF_FILE" 2>/dev/null; then
  echo "lxc.apparmor.profile: unconfined" >> "$CONF_FILE"
fi
# Ensure /proc and /proc/sys are writable (fixes sysctl permission errors from runc)
if ! grep -q "^lxc.mount.auto:.*sys:rw" "$CONF_FILE" 2>/dev/null; then
  echo "lxc.mount.auto: proc:rw sys:rw" >> "$CONF_FILE"
fi

ok "LXC config updated."

# Start CT
info "Starting container..."
pct start "$CTID" >/dev/null
ok "Container started."

# Helper: run inside CT
ct_exec() { pct exec "$CTID" -- bash -lc "$*"; }

info "Waiting for container network..."
sleep 3

# Base packages + locales (avoid noisy locale warnings)
info "Installing base packages..."
ct_exec "apt-get update -y >/dev/null"
ct_exec "DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg lsb-release locales jq file fuse-overlayfs >/dev/null"
ct_exec "sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen || true"
ct_exec "locale-gen >/dev/null || true"
ct_exec "update-locale LANG=en_US.UTF-8 >/dev/null || true"

# Docker
info "Installing Docker..."
ct_exec "curl -fsSL https://get.docker.com | sh >/dev/null"

# Docker daemon config
info "Configuring Docker (fuse-overlayfs, log rotation)..."
ct_exec "mkdir -p /etc/docker"
ct_exec "cat >/etc/docker/daemon.json <<'EOF'
{
  \"storage-driver\": \"fuse-overlayfs\",
  \"log-driver\": \"json-file\",
  \"log-opts\": { \"max-size\": \"10m\", \"max-file\": \"3\" }
}
EOF"
ct_exec "systemctl enable --now docker >/dev/null"
ct_exec "systemctl restart docker >/dev/null"

# Verify /dev/fuse exists
if ! ct_exec "test -e /dev/fuse"; then
  fail "/dev/fuse is missing inside the container. Ensure the CT has feature 'fuse=1' and restart it."
fi

# Verify Docker storage driver
DRIVER="$(pct exec "$CTID" -- bash -lc "docker info --format '{{.Driver}}' 2>/dev/null || true")"
if [[ "$DRIVER" != "fuse-overlayfs" ]]; then
  warn "Docker storage driver is '$DRIVER' (expected fuse-overlayfs). Continuing, but if container start fails, check Docker/LXC settings."
else
  ok "Docker storage driver: $DRIVER"
fi

# Determine CT IP for API_URL
CT_IP="$(pct exec "$CTID" -- bash -lc "hostname -I | awk '{print \$1}'")"
if [[ -z "$CT_IP" ]]; then
  warn "Could not determine container IP (hostname -I empty). If DHCP, wait and re-run: pct exec $CTID -- hostname -I"
  CT_IP="127.0.0.1"
fi
API_URL="http://${CT_IP}:${COBALT_PORT}/"

# Deploy Cobalt
info "Deploying Cobalt (${COBALT_IMAGE})..."
ct_exec "mkdir -p /opt/cobalt"
ct_exec "cat >/opt/cobalt/docker-compose.yml <<EOF
services:
  cobalt:
    image: ${COBALT_IMAGE}
    container_name: cobalt
    restart: unless-stopped
    init: true
    read_only: true
    ports:
      - \"${BIND_IP}:${COBALT_PORT}:${COBALT_PORT}/tcp\"
    environment:
      API_URL: \"${API_URL}\"
      API_NAME: \"${CT_HOSTNAME}\"
EOF"

ct_exec "cd /opt/cobalt && docker compose up -d >/dev/null"

# Show status
ok "Cobalt deployed."

cat <<EOF

============================================================
Cobalt LXC is ready

CTID:           ${CTID}
Hostname:       ${CT_HOSTNAME}
IP:             ${CT_IP}
Cobalt API URL: ${API_URL}

Check:
  pct exec ${CTID} -- bash -lc "docker ps"
  pct exec ${CTID} -- bash -lc "curl -s http://127.0.0.1:${COBALT_PORT}/ | head"

Your bot .env:
  COBALT_API_URL=${API_URL}
============================================================

EOF
