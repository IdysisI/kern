#!/usr/bin/env bash
# enable-pi-tailscale-route.sh
#
# Run THIS on your MAIN PC (the one physically connected to the Pi by
# Ethernet). It advertises the Pi's subnet to Tailscale, so the laptop
# can reach 10.42.0.10 transparently over Tailscale — no SSH needed.
#
# After this, on the LAPTOP, run:
#     sudo tailscale up --accept-routes
#     bash /path/to/setup-pi-tunnel.sh   # the script from earlier
#
# You also need to APPROVE the route once in the Tailscale admin panel:
#     https://login.tailscale.com/admin/machines
#     → click your main PC → "Edit route settings" → enable 10.42.0.0/24
#
# Idempotent: safe to re-run.

set -euo pipefail

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  · %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

# ---- Pre-flight --------------------------------------------------------------
bold "==> Pre-flight checks"
command -v tailscale  >/dev/null || die "tailscale not installed. Install: https://tailscale.com/download/linux"
command -v ip         >/dev/null || die "ip not installed (iproute2 missing?)"

# Verify we can see the Pi on its expected IP.
PI_IP="10.42.0.10"
if timeout 3 bash -c ">/dev/tcp/${PI_IP}/22" 2>/dev/null; then
  info "Pi reachable at ${PI_IP}:22 ✓"
else
  warn "Cannot reach ${PI_IP}:22 from this machine."
  warn "If the Pi is on a different subnet, edit PI_IP in this script and re-run."
fi

# Verify Tailscale is up and we're logged in.
bold "==> Tailscale status"
if ! tailscale status >/dev/null 2>&1; then
  die "Tailscale is not logged in. Run: sudo tailscale up"
fi
TS_IP=$(tailscale ip -4 2>/dev/null | head -n1 || true)
info "Main PC Tailscale IP: ${TS_IP:-unknown}"

# ---- Step 1: enable IP forwarding --------------------------------------------
bold "==> Step 1: enable IPv4 forwarding (required for subnet routing)"
SYSCTL_KEY="net.ipv4.ip_forward"
CURRENT=$(sysctl -n "${SYSCTL_KEY}" 2>/dev/null || echo "0")
if [[ "${CURRENT}" == "1" ]]; then
  info "ip_forward already 1"
else
  info "Setting ip_forward=1"
  sudo sysctl -w "${SYSCTL_KEY}=1" >/dev/null
  if [[ -f /etc/sysctl.d/99-tailscale.conf ]]; then
    info "/etc/sysctl.d/99-tailscale.conf already exists"
  else
    echo "${SYSCTL_KEY}=1" | sudo tee /etc/sysctl.d/99-tailscale.conf >/dev/null
    info "Persisted via /etc/sysctl.d/99-tailscale.conf"
  fi
fi

# ---- Step 2: advertise the Pi's subnet to Tailscale -------------------------
bold "==> Step 2: advertise 10.42.0.0/24 to Tailscale"
PI_SUBNET="10.42.0.0/24"
info "Running: sudo tailscale up --accept-routes --advertise-routes=${PI_SUBNET}"
sudo tailscale up --accept-routes --advertise-routes="${PI_SUBNET}"
info "Subnet advertisement requested."

# ---- Step 3: print next steps ------------------------------------------------
bold "==> Action required (one-time, on the Tailscale admin web panel):"
echo "    1. Open https://login.tailscale.com/admin/machines"
echo "    2. Click your main PC (Tailscale IP: ${TS_IP:-?})"
echo "    3. 'Edit route settings' -> enable '10.42.0.0/24' -> Save"
echo
echo "    Without this approval the route is advertised but not in use."
echo

bold "==> After you approve the route, on the LAPTOP run:"
echo "    sudo tailscale up --accept-routes"
echo "    bash /home/marty/kern/setup-pi-tunnel.sh"
echo
echo "    The laptop will then route 10.42.0.10 transparently through"
echo "    main PC -> Pi, and the existing tunnel script will Just Work."
echo
bold "==> Done on this machine."
info "Verify with: tailscale status | grep -E '10\\.42\\.0\\.0/24|subnets'"
info "(the line will appear once you approve the route in the admin panel)"