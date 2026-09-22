#!/usr/bin/env bash
# Installs the Twitch radio service, and optionally Caddy in front of it.
# Non-interactive Caddy setup: CADDY_DOMAIN=radio.example.com [CADDY_EMAIL=you@example.com] deploy/setup.sh
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; RESET=$'\033[0m'
info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

# ---- .env helpers -----------------------------------------------------
# Deliberately NOT sed-based: a pasted Client Secret or Stream Key can
# contain '/', '&', or '\', any of which corrupts (or silently
# mis-substitutes) a sed replacement. Plain string comparison + printf
# sidesteps that entirely — safe with any value that doesn't itself
# contain a literal newline, which a single `read` line never will.

get_env_var() {  # get_env_var KEY FILE — prints current value, "" if unset/missing
  local key="$1" file="$2" line
  [[ -f "${file}" ]] || return 0
  line="$(grep -m1 "^${key}=" "${file}" 2>/dev/null || true)"
  printf '%s' "${line#"${key}"=}"
}

set_env_var() {  # set_env_var KEY VALUE FILE — replaces KEY=... in place, appends if absent
  local key="$1" value="$2" file="$3" tmp line found=0
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key}="* ]]; then
      printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
      found=1
    else
      printf '%s\n' "${line}" >>"${tmp}"
    fi
  done <"${file}"
  if [[ "${found}" -eq 0 ]]; then
    printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
  fi
  mv "${tmp}" "${file}"
}

trim() {  # pure-bash whitespace trim — no external command, safe with any content
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "${s}"
}

detect_cloud_vm() {  # exit 0 if this looks like a cloud/datacenter VM
  # 169.254.169.254 is the link-local cloud-metadata IP used by AWS, GCP,
  # Azure, Oracle Cloud, DigitalOcean, and most others — unreachable on a
  # residential/home connection. Any HTTP response at all (even 401/403,
  # so deliberately no -f here) means something answered there, which is
  # signal enough; a closed connection or timeout means it didn't.
  curl -s -m 2 -o /dev/null http://169.254.169.254/ 2>/dev/null
}

detect_public_ip() {
  local svc ip=""
  for svc in "https://ifconfig.me" "https://api.ipify.org" "https://icanhazip.com"; do
    ip="$(curl -4 -fsS --max-time 4 "${svc}" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "${ip}" ]]; then
      break
    fi
  done
  printf '%s' "${ip}"
}

generate_password() {
  openssl rand -hex 12 2>/dev/null || head -c16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c20
}

hash_password_for_env() {  # prints a scrypt hash of $1, or $1 itself if hashing fails
  local hashed=""
  hashed="$(cd "${APP_DIR}" && PW="$1" "${APP_DIR}/.venv/bin/python" -c 'import os; from twitch_radio.admin.passwords import hash_password; print(hash_password(os.environ["PW"]))' 2>/dev/null)" || hashed=""
  printf '%s' "${hashed:-$1}"
}

prompt_settings_password() {
  local pw=""
  echo ""
  echo "${CYAN}TWITCH_SETTINGS_PASSWORD${RESET}"
  echo "  Sign-in password for /settings. Required once the bot is reachable from the internet."
  read -r -s -p "  Value (input hidden, Enter to auto-generate one): " pw || true
  echo ""
  pw="$(trim "${pw}")"
  if [[ -z "${pw}" ]]; then
    pw="$(generate_password)"
    echo "  Generated: ${pw}"
    echo "  It is stored hashed in .env — write this one down."
  fi
  set_env_var "TWITCH_SETTINGS_PASSWORD" "$(hash_password_for_env "${pw}")" "${ENV_PATH}"
  success "TWITCH_SETTINGS_PASSWORD saved."
}

prompt_caddy_site() {
  local domain="" public_ip="" resolved=""
  public_ip="$(detect_public_ip)"
  echo ""
  echo "${CYAN}Domain for Caddy${RESET}"
  echo "  A domain whose DNS A record points at this machine, e.g. radio.example.com."
  if [[ -n "${public_ip}" ]]; then
    echo "  No domain? Press Enter to use ${public_ip//./-}.sslip.io, which already points here."
  fi
  read -r -p "  Domain: " domain || true
  domain="$(trim "${domain}")"
  domain="${domain#https://}"
  domain="${domain#http://}"
  domain="${domain%%/*}"
  if [[ -z "${domain}" && -n "${public_ip}" ]]; then
    domain="${public_ip//./-}.sslip.io"
  fi
  if [[ -z "${domain}" ]]; then
    warn "No domain given and the public IP couldn't be detected — skipping Caddy."
    return
  fi
  resolved="$(getent ahostsv4 "${domain}" 2>/dev/null | awk 'NR==1 {print $1}')"
  if [[ -n "${public_ip}" && "${resolved}" != "${public_ip}" ]]; then
    warn "${domain} resolves to ${resolved:-nothing}, not ${public_ip}. Certificates won't issue until DNS points here."
  fi
  SITE_ADDRESS="${domain}"
  read -r -p "  Email for certificate expiry notices (optional): " CADDY_EMAIL || true
  CADDY_EMAIL="$(trim "${CADDY_EMAIL}")"
}

install_caddy() {
  if command -v caddy >/dev/null 2>&1; then
    info "Caddy already installed ($(caddy version | head -n1)) — skipping."
    return 0
  fi
  sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https gpg || return 1
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg || return 1
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null || return 1
  sudo apt update || return 1
  sudo apt install -y caddy || return 1
}

configure_caddy() {  # configure_caddy PORT
  local port="$1" main="/etc/caddy/Caddyfile" site="/etc/caddy/conf.d/twitch-radio.caddy"
  local import_line="import /etc/caddy/conf.d/*.caddy"
  sudo mkdir -p /etc/caddy/conf.d || return 1
  sed -e "s#__SITE_ADDRESS__#${SITE_ADDRESS}#g" -e "s#__UPSTREAM__#127.0.0.1:${port}#g" \
    "${APP_DIR}/deploy/Caddyfile" | sudo tee "${site}" >/dev/null || return 1

  if ! sudo grep -qsF "${import_line}" "${main}"; then
    if ! sudo test -s "${main}" || sudo grep -q 'root \* /usr/share/caddy' "${main}"; then
      if sudo test -s "${main}"; then
        sudo cp "${main}" "${main}.pre-twitch-radio" || return 1
      fi
      {
        if [[ -n "${CADDY_EMAIL}" ]]; then
          printf '{\n\temail %s\n}\n\n' "${CADDY_EMAIL}"
        fi
        printf '%s\n' "${import_line}"
      } | sudo tee "${main}" >/dev/null || return 1
    else
      printf '\n%s\n' "${import_line}" | sudo tee -a "${main}" >/dev/null || return 1
      warn "Your existing ${main} was kept and the import line appended."
    fi
  fi

  if ! sudo caddy validate --config "${main}" --adapter caddyfile >/dev/null 2>&1; then
    error "Caddy rejected the configuration:"
    sudo caddy validate --config "${main}" --adapter caddyfile || true
    return 1
  fi
  sudo systemctl enable caddy >/dev/null 2>&1 || true
  sudo systemctl reload-or-restart caddy || return 1
}

open_web_ports() {
  if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow 80/tcp >/dev/null && sudo ufw allow 443/tcp >/dev/null && sudo ufw allow 443/udp >/dev/null \
      && success "ufw: allowed ports 80 and 443."
  fi
  echo ""
  echo "─────────────────────────────────────────────────────────────"
  echo " Caddy needs ports 80 and 443 reachable from the internet."
  echo " Port ${1} stays private on 127.0.0.1 — do not open it."
  echo ""
  echo " 1. Cloud firewall: ingress rules for TCP 80 and TCP+UDP 443 from"
  echo "    0.0.0.0/0. On Oracle Cloud: the VCN's Security List or NSG."
  echo ""
  echo " 2. The VM's own firewall. On Oracle's Ubuntu images:"
  echo "      sudo cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak"
  echo "      sudo sed -i '/--dport 22 -j ACCEPT/a -A INPUT -p tcp -m state --state NEW -m multiport --dports 80,443 -j ACCEPT' /etc/iptables/rules.v4"
  echo "      sudo sed -i '/--dport 22 -j ACCEPT/a -A INPUT -p udp --dport 443 -j ACCEPT' /etc/iptables/rules.v4"
  echo "      sudo iptables-restore < /etc/iptables/rules.v4"
  echo "      sudo netfilter-persistent save"
  echo "    Then confirm SSH (port 22) is still allowed:"
  echo "      sudo iptables -L INPUT -n --line-numbers"
  echo "─────────────────────────────────────────────────────────────"
}

REQUIRED_ENV_KEYS=(TWITCH_CLIENT_ID TWITCH_CLIENT_SECRET TWITCH_BOT_ID TWITCH_OWNER_ID)

missing_required_env() {  # prints each still-blank required key, one per line
  local key
  for key in "${REQUIRED_ENV_KEYS[@]}"; do
    if [[ -z "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
      echo "${key}"
    fi
  done
}

prompt_env_field() {
  # prompt_env_field KEY SECRET(0/1) NUMERIC(0/1) instruction-lines...
  local key="$1" secret="$2" numeric="$3"
  shift 3
  if [[ -n "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
    info "${key} is already set — leaving it alone."
    return
  fi
  echo ""
  echo "${CYAN}${key}${RESET}"
  local line
  for line in "$@"; do
    echo "  ${line}"
  done
  local value=""
  # `|| true` on each read: under set -e, Ctrl+D/EOF mid-prompt would
  # otherwise abort the whole install partway through (packages and venv
  # already installed by this point) instead of just skipping this field.
  while true; do
    if [[ "${secret}" == "1" ]]; then
      read -r -s -p "  Paste value (input hidden, Enter to skip): " value || true
      echo ""
    else
      read -r -p "  Paste value (Enter to skip): " value || true
    fi
    value="$(trim "${value}")"
    if [[ -z "${value}" ]]; then
      warn "${key} left blank — set it by hand later in ${ENV_PATH}."
      return
    fi
    if [[ "${numeric}" == "1" && ! "${value}" =~ ^[0-9]+$ ]]; then
      warn "Not a plain numeric ID (digits only — no username, no label like 'Twitch ID:')."
      warn "The service refuses to start with this. Try again, or leave blank to skip."
      continue
    fi
    break
  done
  set_env_var "${key}" "${value}" "${ENV_PATH}"
  success "${key} saved."
}

prompt_optional_field() {
  # prompt_optional_field KEY DEFAULT SECRET(0/1) NUMERIC(0/1) description-line...
  # Unlike prompt_env_field, Enter here means "keep the default" (written
  # explicitly into .env), not "leave blank" — every one of these already
  # has a sane default the service runs fine with.
  local key="$1" default="$2" secret="$3" numeric="$4"
  shift 4
  if [[ -n "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
    info "${key} is already set — leaving it alone."
    return
  fi
  local shown_default="${default}"
  if [[ -z "${shown_default}" ]]; then
    shown_default="disabled"
  fi
  echo ""
  echo "${CYAN}${key}${RESET}"
  local line
  for line in "$@"; do
    echo "  ${line}"
  done
  local value=""
  if [[ "${secret}" == "1" ]]; then
    read -r -s -p "  Value (input hidden, Enter for ${shown_default}): " value || true
    echo ""
  else
    read -r -p "  Value [${shown_default}]: " value || true
  fi
  value="$(trim "${value}")"
  if [[ -z "${value}" ]]; then
    value="${default}"
  elif [[ "${numeric}" == "1" && ! "${value}" =~ ^[0-9]+$ ]]; then
    warn "That doesn't look like a number — using the default (${shown_default}) instead."
    value="${default}"
  fi
  if [[ -z "${value}" ]]; then
    return  # blank default (e.g. no settings password) — nothing to write, absence IS the default
  fi
  set_env_var "${key}" "${value}" "${ENV_PATH}"
  success "${key} = ${value}"
}

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
SERVICE_NAME="twitch-radio"
ENV_PATH="${APP_DIR}/.env"
SITE_ADDRESS="${CADDY_DOMAIN:-}"
CADDY_EMAIL="${CADDY_EMAIL:-}"

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

echo "[1/8] Installing system packages"
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg logrotate curl unzip openssl

echo "[2/8] Installing a JS runtime for yt-dlp (Deno)"
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

# Optional: quickjs-ng, a much lighter JS runtime than Deno (no JIT/V8 to
# start up). twitch_radio/extraction.py tries it first for every resolve
# (falling back to Deno automatically if it's missing or fails), since
# Deno's per-request cost here is dominated by process-spawn + interpreter
# startup, not actual computation — exactly where a JIT buys nothing. Purely
# an optimization: everything works with only Deno installed, just slower.
# Static binary, no package manager needed.
if command -v qjs >/dev/null 2>&1; then
  info "quickjs-ng already installed ($(qjs --help 2>&1 | head -n1)) — skipping."
else
  case "$(uname -m)" in
    x86_64)          qjs_asset="qjs-linux-x86_64" ;;
    aarch64|arm64)   qjs_asset="qjs-linux-aarch64" ;;
    *)                qjs_asset="" ;;
  esac
  if [[ -z "${qjs_asset}" ]]; then
    warn "No prebuilt quickjs-ng binary for this architecture ($(uname -m)) — not required, the bot will keep using Deno." \
         "Manual builds: https://github.com/quickjs-ng/quickjs/releases"
  elif curl -fsSL -o /tmp/qjs "https://github.com/quickjs-ng/quickjs/releases/latest/download/${qjs_asset}" \
     && sudo install -m 755 /tmp/qjs /usr/local/bin/qjs; then
    rm -f /tmp/qjs
    success "quickjs-ng installed to /usr/local/bin ($(qjs --help 2>&1 | head -n1))."
  else
    rm -f /tmp/qjs
    warn "quickjs-ng install failed — not required, the bot will keep using Deno." \
         "Install manually later if you want the speedup: https://github.com/quickjs-ng/quickjs/releases"
  fi
fi

echo "[3/8] Preparing app directories"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs" "${APP_DIR}/data/deno-cache"

echo "[4/8] Creating virtual environment"
if [[ ! -d "${APP_DIR}/.venv" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
fi

echo "[5/8] Installing Python dependencies"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

echo "[6/8] Environment file"
if [[ -f "${ENV_PATH}" ]]; then
  info "Found an existing ${ENV_PATH} — keeping it, only filling in anything still blank below."
  chmod 600 "${ENV_PATH}" 2>/dev/null || true
else
  cp "${APP_DIR}/deploy/.env.example" "${ENV_PATH}"
  chmod 600 "${ENV_PATH}"
  success "Wrote ${ENV_PATH} from the template."
fi

if [[ -z "$(missing_required_env)" ]]; then
  success "All required credentials are already set in ${ENV_PATH}."
elif [[ ! -t 0 || ! -t 1 ]]; then
  warn "Not running interactively (no TTY) — skipping the credential wizard."
  warn "Fill in ${ENV_PATH} by hand before starting the service; see the README for where each value comes from."
elif [[ "${SKIP_WIZARD:-}" == "1" ]]; then
  info "SKIP_WIZARD=1 — skipping the credential wizard."
else
  echo ""
  echo "─────────────────────────────────────────────────────────────"
  echo " Credential setup — Enter to skip any of these and fill it in"
  echo " by hand later (${ENV_PATH}). The service just won't start"
  echo " until all four are set."
  echo "─────────────────────────────────────────────────────────────"
  # Note: if the connection this is running over (e.g. an SSH session) dies
  # entirely mid-prompt — not a plain Ctrl+D, the whole pty going away — bash's
  # own terminal-attribute handling for `read -s` can abort the script outright
  # in a way that isn't a normal command failure `|| true` catches, and isn't
  # a signal a `trap` catches either. If that happens: nothing already
  # installed is harmed, and re-running this script picks up exactly where it
  # left off (already-set values are kept, only blanks get re-prompted).

  prompt_env_field TWITCH_CLIENT_ID 0 0 \
    "1. Go to https://dev.twitch.tv/console/apps and log in." \
    "2. Click 'Register Your Application'." \
    "3. Name: anything unique to your account. Category: 'Chat Bot'." \
    "4. OAuth Redirect URLs — add exactly:  http://localhost:4343/oauth/callback" \
    "5. Client Type: 'Confidential'. Click Create." \
    "6. The Client ID is shown right on the app's page."

  prompt_env_field TWITCH_CLIENT_SECRET 1 0 \
    "On that same app page, click 'New Secret'." \
    "It's shown once — copy it now. (Lost it? Generate a new one any time;" \
    "the old one just stops working.)"

  prompt_env_field TWITCH_BOT_ID 0 1 \
    "The numeric Twitch user ID of the account the BOT chats as — not a" \
    "username. Recommended: a separate account, made a moderator in your" \
    "channel. Look up a username's numeric ID at:" \
    "  https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/"

  prompt_env_field TWITCH_OWNER_ID 0 1 \
    "The numeric Twitch user ID of YOUR (broadcaster) account — not a" \
    "username. Same lookup tool as above, your own username this time."
  echo ""
  echo "─────────────────────────────────────────────────────────────"
  echo " Optional settings — every one below already has a working"
  echo " default. Enter accepts it; only type something if you want"
  echo " to change it. All of these can also be edited by hand later"
  echo " in ${ENV_PATH} (or live, for the four under /settings)."
  echo "─────────────────────────────────────────────────────────────"

  echo ""
  echo "${CYAN}-- Chat & audio --${RESET}"
  prompt_optional_field TWITCH_PREFIX "!" 0 0 \
    "— Command prefix in chat (!sr, !skip, ...)."
  prompt_optional_field AUDIO_BITRATE_KBPS "128" 0 1 \
    "— MP3 bitrate for /stream.mp3 (64-320). Raise it if it sounds thin."
  prompt_optional_field PAUSE_QUEUE_WHEN_NO_LISTENERS "false" 0 0 \
    "— true/false. If true, holds off starting the next track while" \
    "    nobody's connected to /stream.mp3, and resumes on its own once" \
    "    someone (re)connects. A track already playing always finishes."

  echo ""
  echo "${CYAN}-- HTTP surface (/stream.mp3, /overlay, /settings) --${RESET}"
  if [[ -n "$(get_env_var "TWITCH_NOWPLAYING_HOST" "${ENV_PATH}")" ]]; then
    info "TWITCH_NOWPLAYING_HOST is already set — leaving it alone."
    prompt_optional_field TWITCH_NOWPLAYING_PORT "8098" 0 1 \
      "— Port for the HTTP surface (1024-65535)."
    prompt_optional_field TWITCH_SETTINGS_PASSWORD "" 1 0 \
      "— Sign-in password for /settings. Required once the bot is reachable" \
      "    from anywhere but this machine."
  else
    set_env_var "TWITCH_NOWPLAYING_HOST" "127.0.0.1" "${ENV_PATH}"
    success "TWITCH_NOWPLAYING_HOST = 127.0.0.1 (Caddy is the only thing that faces the internet)"
    prompt_optional_field TWITCH_NOWPLAYING_PORT "8098" 0 1 \
      "— Port for the HTTP surface (1024-65535)."

    echo ""
    echo "${CYAN}Caddy (automatic HTTPS)${RESET}"
    echo "  Needed if OBS runs on another machine, or so viewers can open the"
    echo "  /commands page. Skip it if OBS runs on this machine."
    default_caddy="n"
    if detect_cloud_vm; then
      default_caddy="y"
    fi
    want_caddy=""
    read -r -p "  Set up Caddy? [y/n] (${default_caddy}): " want_caddy || true
    want_caddy="$(trim "${want_caddy}")"
    want_caddy="${want_caddy:-${default_caddy}}"
    if [[ "${want_caddy}" =~ ^[Yy] ]]; then
      prompt_caddy_site
      prompt_settings_password
    else
      prompt_optional_field TWITCH_SETTINGS_PASSWORD "" 1 0 \
        "— Sign-in password for /settings. Only needed if you later put" \
        "    the bot behind a reverse proxy."
    fi
  fi

  echo ""
  echo "${CYAN}-- Advanced: state filenames (only for multiple instances) --${RESET}"
  prompt_optional_field TWITCH_TOKEN_FILE "twitch_tokens.json" 0 0 \
    "— Under data/. No reason to change unless running >1 instance from one data/."
  prompt_optional_field TWITCH_TUNABLES_FILE "tunables.json" 0 0 \
    "— Same as above, for the /settings tunables."
  prompt_optional_field TWITCH_BLOCKLIST_FILE "blocklist.json" 0 0 \
    "— Same as above, for the !block/!unblock moderation list."

  echo ""
  echo "${CYAN}-- yt-dlp --${RESET}"
  if detect_cloud_vm; then
    warn "This looks like a cloud/datacenter VM (a metadata service answered"
    warn "at 169.254.169.254). YouTube commonly blocks plain requests from"
    warn "datacenter IPs outright — expect !sr to fail with \"Sign in to"
    warn "confirm you're not a bot\" without cookies set up below."
    cookies_desc=(
      "— Detected as likely needed (see warning above). A REAL cookies.txt"
      "    from a logged-in browser session — export one now if you have a"
      "    browser handy (private/incognito window, log into YouTube,"
      "    export with a browser extension), or leave blank and come back"
      "    once !sr actually fails (empty/placeholder file makes things"
      "    worse, not better). MUST be a path under data/ (e.g."
      "    data/cookies.txt) — anywhere else crashes every !sr (read-only"
      "    fs under this service's sandbox)."
    )
  else
    cookies_desc=(
      "— Leave blank on a residential connection. On a cloud VM, YouTube"
      "    often blocks anonymous requests ('Sign in to confirm you're not a"
      "    bot') and this is the simplest fix — a REAL cookies.txt from a"
      "    logged-in browser session (empty/placeholder makes it worse). MUST"
      "    be a path under data/ (e.g. data/cookies.txt) — anywhere else"
      "    crashes every !sr (read-only fs under this service's sandbox)."
    )
  fi
  prompt_optional_field YTDLP_COOKIES_FILE "" 0 0 "${cookies_desc[@]}"
  prompt_optional_field YTDLP_POT_PROVIDER_URL "" 0 0 \
    "— Advanced, cloud-VM alternative to cookies: URL of a local bgutil-" \
    "    ytdlp-pot-provider instance (see README) if you've set one up." \
    "    Leave blank if you haven't — not required."
  prompt_optional_field YTDLP_JS_RUNTIME_PATH "" 0 0 \
    "— Advanced: pin a specific JS runtime binary. Leave blank to auto-detect" \
    "    the Deno install this script just did." \
    "    Switching YTDLP_JS_RUNTIME_NAME to node instead? It needs Node >=22 —" \
    "    Ubuntu's own apt nodejs package is almost always older than that." \
    "    Use https://github.com/nodesource/distributions or nvm, not apt."
  prompt_optional_field YTDLP_JS_RUNTIME_NAME "deno" 0 0 \
    "— Only matters if YTDLP_JS_RUNTIME_PATH above is set."
  prompt_optional_field YTDLP_PLAYER_CLIENT "" 0 0 \
    "— Advanced: comma-separated yt-dlp YouTube player_client override." \
    "    Leave blank for the built-in default (web_embedded,web when" \
    "    cookies are set, to dodge a currently-broken fallback client —" \
    "    see the README's yt-dlp note — otherwise yt-dlp's own default)." \
    "    YouTube changes what works here often; check" \
    "    https://github.com/yt-dlp/yt-dlp/wiki/EJS if requests start failing."
  prompt_optional_field YTDLP_CACHE_TTL_SECONDS "300" 0 1 \
    "— Seconds a resolved song is reused instead of re-running yt-dlp (0-3600)." \
    "    Cuts the usual double-extraction (once in chat, again right before" \
    "    it plays) for anything near the front of the queue. 0 disables it."
  prompt_optional_field YTDLP_CONCURRENCY "2" 0 1 \
    "— Concurrent yt-dlp extractions (1-4). Raise if !sr gets busy."
  prompt_optional_field YTDLP_EXTRACT_TIMEOUT_SECONDS "45" 0 1 \
    "— Seconds before giving up on a single resolve (10-120)."

  echo ""
  echo "${CYAN}-- Logging --${RESET}"
  prompt_optional_field LOG_LEVEL "INFO" 0 0 \
    "— DEBUG/INFO/WARNING/ERROR/CRITICAL."
  prompt_optional_field LOG_TO_FILE "true" 0 0 \
    "— Also write logs/twitch-radio.log (rotated weekly) alongside journalctl."
  echo ""
fi

echo "[7/8] Installing logrotate config and systemd unit"
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

echo "[8/8] Caddy"
caddy_ok=0
bot_port="$(get_env_var TWITCH_NOWPLAYING_PORT "${ENV_PATH}")"
bot_port="${bot_port:-8098}"
if [[ -z "${SITE_ADDRESS}" ]]; then
  info "Skipping. To add it later: CADDY_DOMAIN=radio.example.com ${BASH_SOURCE[0]}"
elif install_caddy && configure_caddy "${bot_port}"; then
  caddy_ok=1
  if [[ -z "$(get_env_var "TWITCH_PUBLIC_BASE_URL" "${ENV_PATH}")" ]]; then
    set_env_var "TWITCH_PUBLIC_BASE_URL" "https://${SITE_ADDRESS}" "${ENV_PATH}"
  fi
  success "Caddy is serving https://${SITE_ADDRESS} -> 127.0.0.1:${bot_port}"
  if [[ -z "$(get_env_var "TWITCH_SETTINGS_PASSWORD" "${ENV_PATH}")" ]]; then
    warn "No TWITCH_SETTINGS_PASSWORD set — /settings will refuse everyone arriving through Caddy."
    warn "Set one with: ${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py --hash-password"
  fi
  open_web_ports "${bot_port}"
else
  error "Caddy setup failed. The bot itself is installed; fix the error above and re-run this script."
fi

echo ""
echo "─────────────────────────────────────────────────────────────"
echo "Next steps:"
echo ""
still_missing="$(missing_required_env)"
if [[ -n "${still_missing}" ]]; then
  echo "1. Still need to fill in, in ${ENV_PATH}:"
  while IFS= read -r key; do
    echo "     - ${key}"
  done <<<"${still_missing}"
  echo "   (see the comments in the file, or the README, for where each comes from —"
  echo "   or just re-run this script interactively to pick up where you left off.)"
else
  echo "1. All required credentials are set in ${ENV_PATH}."
fi
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
echo "       http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot+moderator:manage:chat_messages+moderator:read:followers+moderator:manage:shoutouts&force_verify=true"
echo "     - as the BROADCASTER account:"
echo "       http://localhost:4343/oauth?scopes=channel:bot+channel:read:subscriptions+bits:read+clips:edit+channel:manage:polls&force_verify=true"
echo "   Use two SEPARATE browser sessions for these two — reusing one"
echo "   logged-in session for both silently authorizes the same account"
echo "   twice. Watch step 5's logs right after starting to catch that."
echo ""
if [[ "${caddy_ok}" -eq 1 ]]; then
  echo "4. In OBS, add a Media Source pointed at https://${SITE_ADDRESS}/stream.mp3 and"
  echo "   (optionally) a Browser Source pointed at https://${SITE_ADDRESS}/overlay."
  echo "   Sign in to settings at https://${SITE_ADDRESS}/login."
else
  echo "4. In OBS, add a Media Source pointed at http://127.0.0.1:${bot_port}/stream.mp3 and"
  echo "   (optionally) a Browser Source pointed at /overlay — see README.md."
fi
echo ""
echo "5. journalctl -u ${SERVICE_NAME} -f -o cat    — watch it come up"
echo "─────────────────────────────────────────────────────────────"
