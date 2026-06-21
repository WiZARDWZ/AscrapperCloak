#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-ascrapper}"
SERVICE_NAME="${SERVICE_NAME:-$APP_NAME}"
BRANCH="${BRANCH:-main}"
PY_BIN="${PY_BIN:-python3.10}"
INSTALL_CHROME="${INSTALL_CHROME:-1}"
REPO_URL="${REPO_URL:-}"

RUN_USER="${ASCRAPPER_USER:-${SUDO_USER:-${USER}}}"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP_DIR="${APP_DIR:-$RUN_HOME/apps/$APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="$APP_DIR/.env"

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

run_as_user() {
  if [[ "$(id -u)" -eq 0 ]]; then
    sudo -u "$RUN_USER" -H bash -lc "$*"
  else
    bash -lc "$*"
  fi
}

require_ubuntu_2204() {
  if [[ ! -r /etc/os-release ]]; then
    echo "[ERROR] /etc/os-release not found. This deploy script targets Ubuntu 22.04."
    exit 1
  fi
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "22.04" ]]; then
    echo "[ERROR] This script targets Ubuntu 22.04. Detected ${PRETTY_NAME:-unknown}."
    exit 1
  fi
}

require_normal_user() {
  if [[ "$RUN_USER" == "root" ]]; then
    echo "[ERROR] Refusing to configure the service to run as root. Set ASCRAPPER_USER to a normal user."
    exit 1
  fi
}

install_base_packages() {
  echo "[apt] Installing Python, build, Chrome runtime, and Xvfb dependencies..."
  $SUDO apt-get update
  $SUDO apt-get install -y \
    git curl ca-certificates gnupg unzip build-essential pkg-config \
    "$PY_BIN" "${PY_BIN}-venv" \
    unixodbc unixodbc-dev \
    xvfb \
    libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxrandr2 libasound2 fonts-liberation
}

install_google_chrome() {
  if [[ "$INSTALL_CHROME" != "1" ]]; then
    echo "[chrome] INSTALL_CHROME=0, skipping Google Chrome install."
    return
  fi
  if command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1; then
    echo "[chrome] Google Chrome already available."
    return
  fi
  echo "[chrome] Installing Google Chrome stable..."
  local tmp_deb="/tmp/google-chrome-stable_current_amd64.deb"
  curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o "$tmp_deb"
  $SUDO dpkg -i "$tmp_deb" || $SUDO apt-get -f install -y
  rm -f "$tmp_deb"
}

install_microsoft_odbc18() {
  if dpkg -s msodbcsql18 >/dev/null 2>&1; then
    echo "[odbc] msodbcsql18 already installed."
    $SUDO apt-get install -y unixodbc unixodbc-dev
    return
  fi

  echo "[odbc] Installing Microsoft ODBC Driver 18 for SQL Server..."
  . /etc/os-release
  if ! dpkg -s packages-microsoft-prod >/dev/null 2>&1; then
    local repo_deb="/tmp/packages-microsoft-prod.deb"
    curl -fsSL "https://packages.microsoft.com/config/ubuntu/${VERSION_ID}/packages-microsoft-prod.deb" -o "$repo_deb"
    $SUDO dpkg -i "$repo_deb"
    rm -f "$repo_deb"
  else
    echo "[odbc] Microsoft package repo already configured."
  fi

  $SUDO apt-get update
  $SUDO ACCEPT_EULA=Y apt-get install -y msodbcsql18 mssql-tools18 unixodbc unixodbc-dev
}

prompt_repo_url() {
  if [[ -n "$REPO_URL" ]]; then
    return
  fi
  read -r -p "Git repository URL (leave blank to use existing APP_DIR): " REPO_URL
}

clone_or_update_repo() {
  run_as_user "mkdir -p '$(dirname "$APP_DIR")'"
  if [[ -d "$APP_DIR/.git" ]]; then
    echo "[repo] Updating existing repository..."
    run_as_user "cd '$APP_DIR' && git fetch origin '$BRANCH' && git checkout '$BRANCH' && git pull --ff-only origin '$BRANCH'"
    return
  fi

  prompt_repo_url
  if [[ -z "$REPO_URL" ]]; then
    if [[ -d "$APP_DIR" ]]; then
      echo "[repo] APP_DIR exists without .git; using current files: $APP_DIR"
      return
    fi
    echo "[ERROR] REPO_URL is required for first install when APP_DIR does not exist."
    exit 1
  fi

  echo "[repo] Cloning $BRANCH into $APP_DIR..."
  run_as_user "git clone --branch '$BRANCH' '$REPO_URL' '$APP_DIR'"
}

setup_venv_deps() {
  echo "[python] Creating/updating virtualenv..."
  run_as_user "cd '$APP_DIR' && '$PY_BIN' -m venv .venv"
  run_as_user "cd '$APP_DIR' && .venv/bin/python -m pip install --upgrade pip"
  run_as_user "cd '$APP_DIR' && .venv/bin/python -m pip install -r requirements.txt"
  run_as_user "cd '$APP_DIR' && .venv/bin/python -m cloakbrowser install"
}

ensure_env_key() {
  local key="$1"
  local value="$2"
  run_as_user "grep -q '^${key}=' '$ENV_FILE' || printf '%s=%s\n' '$key' '$value' >> '$ENV_FILE'"
}

ensure_env_file() {
  echo "[env] Creating .env if missing and appending missing keys only..."
  run_as_user "mkdir -p '$APP_DIR' && touch '$ENV_FILE' && chmod 600 '$ENV_FILE'"
  ensure_env_key "TELEGRAM_BOT_TOKEN" ""
  ensure_env_key "RUNTIME_PROFILE" "ubuntu_prod"
  ensure_env_key "DB_HOST" "127.0.0.1"
  ensure_env_key "DB_PORT" "1433"
  ensure_env_key "DB_NAME" ""
  ensure_env_key "DB_USER" ""
  ensure_env_key "DB_PASSWORD" ""
  ensure_env_key "DB_DRIVER" "ODBC Driver 18 for SQL Server"
  ensure_env_key "DB_ENCRYPT" "yes"
  ensure_env_key "DB_TRUST_SERVER_CERTIFICATE" "yes"
  ensure_env_key "DB_TIMEOUT" "30"
  ensure_env_key "HEADLESS" "0"
  ensure_env_key "PERF_PROFILE" "normal"
  ensure_env_key "OUTPUT_DIR" "output"
  ensure_env_key "LOG_DIR" "logs"
  ensure_env_key "RUNTIME_DIR" "runtime"
  ensure_env_key "PYTHONUNBUFFERED" "1"
  ensure_env_key "APP_ENV" "production"
  ensure_env_key "BROWSER_ENGINE" "cloak"
  ensure_env_key "CLOAK_PROFILE_DIR" "rea_profile"
  ensure_env_key "CLOAK_FINGERPRINT_PLATFORM" "windows"
  ensure_env_key "CLOAK_VIEWPORT" "1365x768"
  ensure_env_key "CLOAK_LOCALE" "en-AU"
  ensure_env_key "CLOAK_TIMEZONE" "Australia/Sydney"
  ensure_env_key "CLOAK_DISABLE_HTTP2" "0"
  ensure_env_key "CLOAK_HTTP2_MODE" "default"
  ensure_env_key "CLOAK_HUMANIZE" "1"
  ensure_env_key "CLOAK_GEOIP" "0"
  ensure_env_key "CLOAK_USE_PERSISTENT_CONTEXT" "1"
  ensure_env_key "CLOAK_HEADLESS" "0"
  ensure_env_key "MODULE2_PROFILE_BASE_DIR" "rea_profile"
  ensure_env_key "MODULE1_INTER_PAGE_DELAY_SECONDS" "10"
  ensure_env_key "MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS" "5"
  ensure_env_key "MODULE2_INTER_PAGE_DELAY_SECONDS" "10"
  ensure_env_key "MODULE2_INTER_PAGE_DELAY_JITTER_SECONDS" "5"
  ensure_env_key "MODULE2_INTER_WINDOW_DELAY_SECONDS" "12"
  ensure_env_key "MODULE2_INTER_WINDOW_DELAY_JITTER_SECONDS" "6"
  ensure_env_key "MODULE3_INTER_DETAIL_DELAY_SECONDS" "10"
  ensure_env_key "MODULE3_INTER_DETAIL_DELAY_JITTER_SECONDS" "5"
  ensure_env_key "LOW_BANDWIDTH_MODE" "0"
  ensure_env_key "BLOCK_HEAVY_RESOURCES" "0"
  ensure_env_key "BLOCK_TRACKERS" "0"
  ensure_env_key "BLOCK_IMAGES" "0"
  ensure_env_key "BLOCK_MEDIA" "0"
  ensure_env_key "BLOCK_FONTS" "0"
  ensure_env_key "BLOCK_MAPS" "0"
  ensure_env_key "BLOCK_ADS" "0"
  ensure_env_key "BLOCK_ANALYTICS" "0"
  ensure_env_key "BROWSER_BLOCK_GRACE_SECONDS" "30"
  ensure_env_key "BROWSER_BLOCK_POLL_SECONDS" "1.0"
  ensure_env_key "BROWSER_NO_RESULTS_STABLE_SECONDS" "1.0"
  ensure_env_key "BROWSER_KPSDK_SAME_SESSION_RECHECKS" "2"
  ensure_env_key "BROWSER_KPSDK_SETTLE_SECONDS" "10"
  ensure_env_key "BROWSER_PAGE_STATE_DEBUG" "1"
  ensure_env_key "BROWSER_USE_RUNTIME_PROFILE_STATE" "1"
  run_as_user "chmod 600 '$ENV_FILE'"
}

create_runtime_dirs() {
  echo "[dirs] Creating output/log/runtime directories..."
  run_as_user "mkdir -p '$APP_DIR/output' '$APP_DIR/logs' '$APP_DIR/runtime/temp'"
}

env_get() {
  local key="$1"
  run_as_user "awk -F= '/^${key}=/{print substr(\$0, index(\$0, \"=\")+1); exit}' '$ENV_FILE'"
}

validate_env_before_start() {
  local token db_host db_name db_user db_password
  token="$(env_get TELEGRAM_BOT_TOKEN || true)"
  db_host="$(env_get DB_HOST || true)"
  db_name="$(env_get DB_NAME || true)"
  db_user="$(env_get DB_USER || true)"
  db_password="$(env_get DB_PASSWORD || true)"
  if [[ -z "$token" || -z "$db_host" || -z "$db_name" || -z "$db_user" || -z "$db_password" ]]; then
    echo "[ERROR] .env is incomplete. Fill TELEGRAM_BOT_TOKEN, DB_HOST, DB_NAME, DB_USER, and DB_PASSWORD:"
    echo "        $ENV_FILE"
    exit 1
  fi
}

write_service() {
  echo "[systemd] Writing $SERVICE_FILE..."
  $SUDO tee "$SERVICE_FILE" >/dev/null <<SERVICEEOF
[Unit]
Description=AScrapper / OzHome Monitor Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/env HEADLESS=0 /usr/bin/xvfb-run -a -s "-screen 0 1365x768x24" ${APP_DIR}/.venv/bin/python telegram_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME"
}

restart_service() {
  validate_env_before_start
  echo "[systemd] Restarting $SERVICE_NAME..."
  $SUDO systemctl restart "$SERVICE_NAME"
}

show_status() {
  echo
  echo "Status:"
  $SUDO systemctl status "$SERVICE_NAME" --no-pager || true
  echo
  echo "Useful commands:"
  echo "  sudo systemctl restart $SERVICE_NAME"
  echo "  sudo systemctl status $SERVICE_NAME --no-pager"
  echo "  sudo journalctl -u $SERVICE_NAME -f"
  echo "  cd $APP_DIR && HEADLESS=0 xvfb-run -a -s \"-screen 0 1365x768x24\" .venv/bin/python -m tools.check_chrome_xvfb"
  echo "  cd $APP_DIR && .venv/bin/python -m tools.check_db_connection"
}

main() {
  require_ubuntu_2204
  require_normal_user
  install_base_packages
  install_google_chrome
  install_microsoft_odbc18
  clone_or_update_repo
  setup_venv_deps
  ensure_env_file
  create_runtime_dirs
  write_service
  restart_service
  show_status
}

main "$@"
