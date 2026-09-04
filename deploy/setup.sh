#!/usr/bin/env bash
# Installs the Twitch radio service on the same box as (or separate from) the
# Discord bot. Does NOT touch the Discord bot's files, venv, or service.
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; RESET=$'\033[0m'
info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
SERVICE_NAME="twitch-radio"
ENV_PATH="${APP_DIR}/.env"

echo "Twitch Radio Bot — setup"
echo "App directory: ${APP_DIR}"
echo "Service user:  ${SERVICE_USER}"
echo ""

# SUDO_USER is only set when this was actually invoked via `sudo`. Run as a
# root shell directly (common on minimal VPS/container images) and whoami
# falls back to "root" silently — installing a systemd unit that runs
# ffmpeg and the yt-dlp JS runtime, both consuming untrusted URLs from chat,
# as User=root. Require an explicit opt-in for that instead of guessing.
if [[ "${SERVICE_USER}" == "root" && -z "${SUDO_USER:-}" ]]; then
  if [[ "${ALLOW_ROOT:-}" != "1" ]]; then
    error "Running as root with no SUDO_USER — this would install the service as User=root."
    warn  "Run this via 'sudo' as a normal user instead, so the service runs as that user."
    warn  "If running as root is actually intended (e.g. a container), re-run with:"
    warn  "    ALLOW_ROOT=1 ${BASH_SOURCE[0]}"
    exit 1
  fi
  warn "Proceeding as root (ALLOW_ROOT=1) — the service will run as User=root."
fi

echo "[1/7] Installing system packages"
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg logrotate curl unzip

echo "[2/7] Installing a JS runtime for yt-dlp (Deno)"
# yt-dlp needs an external JS runtime to solve YouTube's JS challenges as of
# the version pinned in requirements.txt. Installed system-wide to
# /usr/local/bin so it's on PATH for the systemd unit too (that unit sets an
# explicit PATH that doesn't include a per-user ~/.deno/bin).
if command -v deno >/dev/null 2>&1; then
  info "Deno already installed ($(deno --version | head -n1)) — skipping."
else
  # install.sh takes zero flags — it's already fully non-interactive. Its
  # only optional argument is a specific version tag; passing anything else
  # gets treated as that tag and 404s.
  if curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh >/dev/null 2>&1; then
    success "Deno installed to /usr/local/bin."
  else
    warn "Deno install failed — yt-dlp will fall back to degraded YouTube support." \
         "Install manually later: https://docs.deno.com/runtime/getting_started/installation/"
  fi
fi

echo "[3/7] Preparing app directories"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"

echo "[4/7] Creating virtual environment"
if [[ ! -d "${APP_DIR}/.venv" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
fi

echo "[5/7] Installing Python dependencies"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

echo "[6/7] Writing environment file"
if [[ -f "${ENV_PATH}" ]]; then
  info "Kept the existing .env unchanged."
else
  cp "${APP_DIR}/deploy/.env.example" "${ENV_PATH}"
  success "Wrote ${ENV_PATH} from the template — every value needs filling in by hand."
fi

echo "[7/7] Installing logrotate config and systemd unit"
# Template the path/user instead of installing verbatim — otherwise a custom
# APP_DIR/SERVICE_USER silently doesn't take effect here even though the
# rest of the install honors it.
sed -e "s#/home/ubuntu/twitch-radio-bot#${APP_DIR}#g" \
    -e "s#su ubuntu ubuntu#su ${SERVICE_USER} ${SERVICE_USER}#" \
    "${APP_DIR}/deploy/${SERVICE_NAME}-logrotate" | sudo tee "/etc/logrotate.d/${SERVICE_NAME}" >/dev/null
sed \
  -e "s#/home/ubuntu/twitch-radio-bot#${APP_DIR}#g" \
  -e "s#User=ubuntu#User=${SERVICE_USER}#" \
  "${APP_DIR}/deploy/${SERVICE_NAME}.service" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null
sudo systemctl daemon-reload
success "Installed /etc/systemd/system/${SERVICE_NAME}.service (not started yet)."

echo ""
echo "─────────────────────────────────────────────────────────────"
echo "Next steps:"
echo ""
echo "1. Edit ${ENV_PATH} — every TWITCH_* value is required (see comments"
echo "   in the file, or the README, for where each one comes from)."
echo ""
echo "2. Start the service:"
echo "     sudo systemctl enable --now ${SERVICE_NAME}"
echo ""
echo "3. Complete the one-time OAuth authorization — the service can't chat"
echo "   or read chat until this is done. Full walkthrough in README.md, or"
echo "   the module docstring in twitch_radio/chatbot.py. Short version:"
echo "     ssh -L 4343:localhost:4343 ${SERVICE_USER}@<this-host>"
echo "   then, in a browser:"
echo "     - as the BOT account:"
echo "       http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot"
echo "     - as the BROADCASTER account:"
echo "       http://localhost:4343/oauth?scopes=channel:bot"
echo ""
echo "4. journalctl -u ${SERVICE_NAME} -f -o cat    — watch it come up"
echo "─────────────────────────────────────────────────────────────"
