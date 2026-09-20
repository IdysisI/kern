#!/usr/bin/env bash
# setup-pi-tunnel.sh  (v2 — tty-aware, detects root login, prints progress)
#
# One-shot setup: persistent SSH tunnel from this laptop to the Pi's
# SearXNG orchestrator. After this, Kern's search/scrape tools route
# through the Pi with no code changes to Kern.
#
# YOU WILL BE PROMPTED ONCE for the Pi's root password. The prompt is
# issued by 'expect' (or fall back to direct tty-read), so it works
# whether you run this from a real terminal, my exec handle, or CI.
#
# Re-running is safe: every step is idempotent.

set -euo pipefail

PI_HOST="10.42.0.10"
PI_PORT="22"
PI_USER="root"
LOCAL_PORT="8000"
REMOTE_HOST="10.42.0.10"
REMOTE_PORT="8000"
KEY_PATH="$HOME/.ssh/pi_tunnel"
SHELL_RC="$HOME/.bashrc"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="pi-tunnel.service"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  · %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

# ---- Pre-flight --------------------------------------------------------------
bold "==> Pre-flight checks"
command -v ssh          >/dev/null || die "ssh not installed"
command -v ssh-copy-id  >/dev/null || die "ssh-copy-id not installed"
command -v expect       >/dev/null || warn "expect not installed — will use /dev/tty read fallback"
command -v systemctl    >/dev/null || warn "systemctl not found — will use @reboot cron fallback"

# Detect current user early so we can warn about privilege mismatches
CURRENT_USER="$(whoami)"
info "Running as user: ${CURRENT_USER}"

# Test reachability FIRST. Distinguishes "Pi is offline" from "auth is wrong".
info "TCP-probing ${PI_HOST}:${PI_PORT}..."
if ! timeout 5 bash -c ">/dev/tcp/${PI_HOST}/${PI_PORT}" 2>/dev/null; then
  die "Cannot reach ${PI_HOST}:${PI_PORT} from this laptop. Check: is the Pi powered? same network? firewall?"
fi
info "TCP reachable."

# Test SSH handshake — fails fast on key/network issues, not password.
info "SSH-handshake probe (ssh -o BatchMode=yes → expect 'Permission denied' or shell)..."
HS_OUT=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
         -p "${PI_PORT}" "${PI_USER}@${PI_HOST}" whoami 2>&1 || true)
case "$HS_OUT" in
  *"$PI_USER"*)
    info "Key-based auth to ${PI_USER}@${PI_HOST} already works."
    SKIP_COPYID=1 ;;
  *"Permission denied"*|*"password"*|*"publickey"*)
    info "Auth required (expected — we'll install the key next)."
    SKIP_COPYID=0 ;;
  *)
    warn "SSH probe returned unexpected output:"
    warn "    ${HS_OUT}"
    SKIP_COPYID=0 ;;
esac

# ---- Step 1: dedicated SSH key ------------------------------------------------
bold "==> Step 1: dedicated SSH key (no passphrase, ed25519)"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [[ ! -f "${KEY_PATH}" ]]; then
  info "Generating ${KEY_PATH}"
  ssh-keygen -t ed25519 -f "${KEY_PATH}" -N '' -C 'kern-laptop-pi-tunnel' >/dev/null
else
  info "Key already exists at ${KEY_PATH} — reusing"
fi
ls -la "${KEY_PATH}" "${KEY_PATH}.pub" | sed 's/^/      /'

# ---- Step 2: install public key on the Pi -----------------------------------
bold "==> Step 2: install public key on the Pi"
if [[ "${SKIP_COPYID}" == "1" ]]; then
  info "Key already authorized on Pi — skipping prompt."
else
  info "About to prompt for ${PI_USER}@${PI_HOST}'s password ONCE."
  info ">>> The prompt will appear NOW. Type the password and press Enter. <<<"

  if command -v expect >/dev/null 2>&1; then
    # Reliable path: expect allocates its own pty and feeds the password in.
    expect -c "
      set timeout 30
      spawn ssh-copy-id -i ${KEY_PATH}.pub -p ${PI_PORT} ${PI_USER}@${PI_HOST}
      expect {
        \"password:\" {
          stty -echo
          send \"\${env(KERN_PI_PASS)}\r\"
        }
        timeout { exit 2 }
        eof
      }
      expect {
        \"password:\" {
          # env var not set — read from /dev/tty interactively
          send_user \"password: \"
          expect_user -re \"(.*)\n\" { set pw \$expect_out(1,string) }
          send \"\$pw\r\"
        }
        eof
      }
      expect eof
      catch wait result
      exit [lindex \$result 3]
    " || die "ssh-copy-id failed (auth rejected or unreachable)."
  else
    # Fallback: open /dev/tty ourselves so ssh-copy-id has somewhere to read.
    info "Using /dev/tty fallback (no expect installed)."
    if [[ ! -e /dev/tty ]]; then
      die "No /dev/tty available. Install 'expect' (sudo dnf install expect) and rerun."
    fi
    ssh-copy-id -i "${KEY_PATH}.pub" -p "${PI_PORT}" "${PI_USER}@${PI_HOST}" \
      < /dev/tty || die "ssh-copy-id failed."
  fi
  info "Key installed on Pi."
fi

# Sanity-check that key-based auth now works.
info "Verifying key-based auth..."
if ! ssh -o BatchMode=yes -i "${KEY_PATH}" -p "${PI_PORT}" "${PI_USER}@${PI_HOST}" true 2>/dev/null; then
  die "Key installed but key-based auth still fails. Check Pi's sshd_config (PermitRootLogin, PubkeyAuthentication)."
fi
info "Key-based auth verified."

# ---- Step 3: ~/.ssh/config entry --------------------------------------------
bold "==> Step 3: ~/.ssh/config entry"
touch "$HOME/.ssh/config" && chmod 600 "$HOME/.ssh/config"
if ! grep -q "^Host pi-tunnel$" "$HOME/.ssh/config" 2>/dev/null; then
  cat >> "$HOME/.ssh/config" <<'EOF'

Host pi-tunnel
    HostName 10.42.0.10
    Port 22
    User root
    IdentityFile ~/.ssh/pi_tunnel
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
    StrictHostKeyChecking accept-new
EOF
  info "Appended 'pi-tunnel' host alias to ~/.ssh/config"
else
  info "Host alias 'pi-tunnel' already present — leaving as-is"
fi

# ---- Step 4: kill any stale tunnel ------------------------------------------
bold "==> Step 4: kill stale tunnel on local port ${LOCAL_PORT}"
if ss -ltn 2>/dev/null | grep -q ":${LOCAL_PORT}\b"; then
  info "Port ${LOCAL_PORT} in use — killing old forwarders"
  pkill -f "ssh.*-L ${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}" 2>/dev/null || true
  sleep 1
else
  info "Port ${LOCAL_PORT} is free"
fi

# ---- Step 5: start the tunnel ------------------------------------------------
bold "==> Step 5: start the tunnel"
ssh -fN -L "${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}" pi-tunnel
sleep 1
if curl -fsS --max-time 5 "http://127.0.0.1:${LOCAL_PORT}/" >/dev/null 2>&1; then
  info "Tunnel UP — SearXNG responded on http://127.0.0.1:${LOCAL_PORT}/"
else
  warn "Tunnel started but SearXNG did not respond on http://127.0.0.1:${LOCAL_PORT}/"
  warn "Possible causes: SearXNG bound to 127.0.0.1 inside the Pi (not 10.42.0.10), wrong port, container not up."
  warn "Debug from your laptop:"
  warn "    ssh pi-tunnel 'ss -ltn | grep ${REMOTE_PORT}'"
  warn "    ssh pi-tunnel 'docker ps | grep -i searxng'"
fi

# ---- Step 6: KERN_WEB_BASE ----------------------------------------------------
bold "==> Step 6: point Kern at the tunnel via KERN_WEB_BASE"
if ! grep -q '^export KERN_WEB_BASE=' "$SHELL_RC" 2>/dev/null; then
  printf '\n# Kern: route web search/scrape through the SSH tunnel to the Pi\nexport KERN_WEB_BASE="http://127.0.0.1:%s"\n' "${LOCAL_PORT}" >> "$SHELL_RC"
  info "Appended KERN_WEB_BASE to ${SHELL_RC}"
  info "Open a NEW terminal (or: source ${SHELL_RC}) to pick it up."
else
  info "KERN_WEB_BASE already set in ${SHELL_RC} — leaving as-is"
fi

# ---- Step 7: persist across reboots ------------------------------------------
bold "==> Step 7: persist across reboots"
if command -v systemctl >/dev/null && systemctl --user status >/dev/null 2>&1; then
  mkdir -p "${SYSTEMD_DIR}"
  cat > "${SYSTEMD_DIR}/${SERVICE_NAME}" <<EOF
[Unit]
Description=SSH tunnel to Pi for Kern web tools (127.0.0.1:${LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_PORT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -L ${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT} pi-tunnel
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE_NAME}" >/dev/null
  systemctl --user restart "${SERVICE_NAME}"
  info "Enabled ${SYSTEMD_DIR}/${SERVICE_NAME} (auto-restarts on reboot if lingering is enabled)"
  warn "For reboot survival across LOGOUT, run ONCE on the laptop:"
  warn "    sudo loginctl enable-linger ${CURRENT_USER}"
else
  info "No systemd --user — falling back to @reboot cron"
  ( crontab -l 2>/dev/null | grep -v 'pi-tunnel-reboot' ; \
    printf '@reboot /usr/bin/ssh -N -L %s:%s:%s pi-tunnel # pi-tunnel-reboot\n' \
      "${LOCAL_PORT}" "${REMOTE_HOST}" "${REMOTE_PORT}" ) | crontab -
  info "Added @reboot cron entry"
fi

# ---- Step 8: end-to-end smoke test -------------------------------------------
bold "==> Step 8: end-to-end smoke test"
info "POST /v1/search  query=test  limit=1"
RESP=$(curl -sS --max-time 15 -X POST \
  "http://127.0.0.1:${LOCAL_PORT}/v1/search" \
  -H 'Content-Type: application/json' \
  -d '{"query":"test","limit":1}' || echo "<<<curl-failed>>>")
if [[ "$RESP" == "<<<curl-failed>>>" ]]; then
  warn "End-to-end probe failed. Try manually:"
  warn "    curl -v http://127.0.0.1:${LOCAL_PORT}/"
  warn "    ssh pi-tunnel 'ss -ltn | grep ${REMOTE_PORT}'"
else
  echo "$RESP" | head -c 500
  echo
fi

bold "==> Done."
echo "Open a new terminal and run 'kern' — web search/scrape now go through the Pi."
echo "Verify anytime:"
echo "    curl -s http://127.0.0.1:${LOCAL_PORT}/ | head -1"
echo "    systemctl --user status ${SERVICE_NAME}"