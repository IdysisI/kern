#!/usr/bin/env bash
# Kern installer — one command:
#   curl -fsSL https://raw.githubusercontent.com/IdysisI/kern/main/install.sh | bash
#
# What it does:
#   1. clones the repo into ~/.local/share/kern (or updates it if already there)
#   2. installs the `kern` command with pipx if you have it, else into its own venv
#   3. drops `kern` on your PATH (~/.local/bin) and prints how to run it
#
# Safe to re-run: it just updates. No sudo. Doesn't touch your system Python.
set -euo pipefail

REPO_URL="${KERN_REPO:-https://github.com/IdysisI/kern.git}"
RAW_BASE="https://raw.githubusercontent.com/IdysisI/kern/main"
INSTALL_DIR="${KERN_HOME:-$HOME/.local/share}/kern"
BIN_DIR="$HOME/.local/bin"
GREEN=''; BOLD=''; RESET=''
if [ -t 1 ]; then GREEN='\033[32m'; BOLD='\033[1m'; RESET='\033[0m'; fi
say()  { printf '%b\n' "$*"; }
ok()   { say "${GREEN}✓${RESET} $*"; }
die()  { say "✗ $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

say "${BOLD}🌱 installing kern…${RESET}"

# --- pick a python ------------------------------------------------------------
PYBIN=""
for c in python3 python; do
  if need "$c"; then PYBIN="$c"; break; fi
done
[ -n "$PYBIN" ] || die "python 3 not found. Install Python 3.11+ first: https://python.org"
PYVER="$("$PYBIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PYBIN" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)' \
  || die "kern needs Python 3.11+, you have $PYVER."
ok "found python $PYVER ($PYBIN)"

# --- get the source -----------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  say "updating existing copy in $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only -q || die "git pull failed — is $INSTALL_DIR a clean kern checkout?"
  ok "updated to latest"
else
  need git || die "git not found. Install git, or download https://github.com/IdysisI/kern/archive/refs/heads/main.zip"
  say "cloning into $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone -q "$REPO_URL" "$INSTALL_DIR" || die "git clone failed"
  ok "cloned"
fi

# --- install the command ------------------------------------------------------
# Include the [gui] extra so `kern-gui` works out of the box (pulls mistune,
# PySide6, qasync, pygments). Set KERN_NO_GUI=1 for a headless CLI-only install.
GUI_EXTRA="[gui]"
if [ "${KERN_NO_GUI:-0}" = "1" ]; then GUI_EXTRA=""; fi

# Auto-detect Termux / Android environments where PySide6 has no wheels
if [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux" ] || [ "$(uname -o 2>/dev/null)" = "Android" ]; then
  GUI_EXTRA=""
fi

mkdir -p "$BIN_DIR"
if need pipx; then
  say "installing with pipx (isolated)…"
  if [ -n "$GUI_EXTRA" ] && ! pipx install --force "${INSTALL_DIR}${GUI_EXTRA}" >/dev/null 2>&1; then
    say "  Note: GUI dependencies (PySide6) not available on this platform — installing CLI only."
    GUI_EXTRA=""
  fi
  pipx install --force "${INSTALL_DIR}${GUI_EXTRA}" || die "pipx install failed"
  ok "installed with pipx"
else
  say "no pipx — using a private venv (pipx is nicer; install it anytime and re-run this)"
  VENV="$INSTALL_DIR/.venv"
  "$PYBIN" -m venv "$VENV" || die "could not create venv (python3-venv missing?)"
  "$VENV/bin/pip" -q install --upgrade pip >/dev/null 2>&1 || true
  if [ -n "$GUI_EXTRA" ] && ! "$VENV/bin/pip" -q install "${INSTALL_DIR}${GUI_EXTRA}" >/dev/null 2>&1; then
    say "  Note: GUI dependencies (PySide6) not available on this platform — installing CLI only."
    GUI_EXTRA=""
  fi
  "$VENV/bin/pip" -q install "${INSTALL_DIR}${GUI_EXTRA}" || die "pip install failed"
  ln -sf "$VENV/bin/kern" "$BIN_DIR/kern"
  [ -x "$VENV/bin/kern-gui" ] && ln -sf "$VENV/bin/kern-gui" "$BIN_DIR/kern-gui" || true
  ok "installed into $VENV"
fi

# --- make sure it's on PATH ---------------------------------------------------
on_path() { case ":$PATH:" in *":$BIN_DIR:"*) return 0;; *) return 1;; esac; }
if ! need kern && ! on_path; then
  say ""
  say "${BOLD}one more step:${RESET} put $BIN_DIR on your PATH. Add this to your shell rc:"
  say "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  case "${SHELL:-}" in
    */zsh)  say "  (that's ~/.zshrc)";;
    */fish) say "  (fish:  fish_add_path ~/.local/bin)";;
    *)      say "  (that's ~/.bashrc or ~/.profile)";;
  esac
fi

say ""
ok "${BOLD}kern is installed!${RESET}"
say "  run it:        ${BOLD}kern${RESET}"
say "  sign in:       ${BOLD}kern login github${RESET}   (one click, no SSH keys)"
say "  point at a model:  export KERN_BASE_URL=... KERN_MODEL=..."
say ""
