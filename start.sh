#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="Pi-Agenda"
APP_USER="pi-agenda"
APP_GROUP="pi-agenda"
INSTALL_DIR="/opt/pi-agenda"
DATA_DIR="/var/lib/pi-agenda"
ENV_FILE="/etc/pi-agenda.env"
PORT="${PI_AGENDA_PORT:-}"
CACHE_PORT="${PI_AGENDA_CACHE_PORT:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_COMMIT="unknown"
if git -C "$SCRIPT_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  SOURCE_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse --verify HEAD)"
fi
MODE="install"

if [[ -r "$ENV_FILE" ]]; then
  [[ -n "$PORT" ]] || PORT="$(sed -n 's/^PI_AGENDA_PORT=//p' "$ENV_FILE" | head -n 1)"
  [[ -n "$CACHE_PORT" ]] || CACHE_PORT="$(sed -n 's/^PI_AGENDA_CACHE_PORT=//p' "$ENV_FILE" | head -n 1)"
fi
PORT="${PORT:-8000}"
CACHE_PORT="${CACHE_PORT:-8002}"

info() { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*" >&2; }
fail() { printf '\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

for argument in "$@"; do
  case "$argument" in
    --reset-password) MODE="reset-password" ;;
    --repair) MODE="repair" ;;
    --status) MODE="status" ;;
    --help|-h)
      printf '%s\n' "Usage: sudo ./start.sh [--repair|--reset-password|--status]"
      exit 0
      ;;
    *) fail "Unknown option: $argument" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "Run this script with sudo: sudo ./start.sh"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "PI_AGENDA_PORT must be between 1 and 65535."
[[ "$CACHE_PORT" =~ ^[0-9]+$ ]] && (( CACHE_PORT >= 1 && CACHE_PORT <= 65535 )) || fail "PI_AGENDA_CACHE_PORT must be between 1 and 65535."
[[ "$PORT" != "$CACHE_PORT" ]] || fail "Management and cache ports must be different."

management_addresses() {
  printf 'Management: http://pi-agenda.local:%s/admin\n' "$PORT"
  local addresses
  addresses="$(hostname -I 2>/dev/null || true)"
  for address in $addresses; do
    case "$address" in
      127.*|169.254.*|::1) continue ;;
      *:*) continue ;;
    esac
    printf 'Management: http://%s:%s/admin\n' "$address" "$PORT"
  done
}

if [[ "$MODE" == "status" ]]; then
  management_addresses
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --no-pager --full status pi-agenda-web pi-agenda-worker pi-agenda-cache pi-agenda-kiosk || true
  fi
  [[ -x "$INSTALL_DIR/.venv/bin/pi-agenda" ]] && "$INSTALL_DIR/.venv/bin/pi-agenda" status || true
  exit 0
fi

[[ -r /etc/os-release ]] || fail "Cannot identify the operating system."
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
  raspbian|debian) ;;
  *) fail "Raspberry Pi OS/Debian is required; detected ${ID:-unknown}." ;;
esac
case "${VERSION_CODENAME:-}" in
  trixie|bookworm) ;;
  *) warn "${VERSION_CODENAME:-Unknown release} has not been validated. Continuing cautiously." ;;
esac

MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
case "$MODEL" in
  *"Raspberry Pi Zero 2 W"*|*"Raspberry Pi 3 Model B"*) ;;
  *)
    if [[ "${PI_AGENDA_ALLOW_UNSUPPORTED:-0}" != "1" ]]; then
      fail "Supported hardware is Raspberry Pi Zero 2W or Raspberry Pi 3 Model B. Detected: ${MODEL:-unknown}. Set PI_AGENDA_ALLOW_UNSUPPORTED=1 only for development."
    fi
    warn "Continuing on unsupported hardware: ${MODEL:-unknown}"
    ;;
esac

[[ "$(dpkg --print-architecture)" == "armhf" ]] || warn "32-bit armhf is recommended for the supported 1 GB-or-less Raspberry Pi models."

if [[ "$MODE" != "reset-password" ]]; then
  info "Installing operating-system dependencies…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev build-essential \
    xserver-xorg xinit x11-xserver-utils openbox unclutter chromium \
    dbus-x11 curl avahi-daemon libnss-mdns sudo rsync git \
    libreoffice-impress poppler-utils ffmpeg wget \
    fonts-dejavu-core fonts-liberation2 alsa-utils zram-tools

  if ! getent group "$APP_GROUP" >/dev/null; then
    groupadd --system "$APP_GROUP"
  fi
  if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --gid "$APP_GROUP" --groups video,audio,input \
      --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"
  fi
  for group in video audio input render systemd-journal; do
    getent group "$group" >/dev/null && usermod -a -G "$group" "$APP_USER"
  done

  install -d -o root -g root -m 0755 "$INSTALL_DIR"
  install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 \
    "$DATA_DIR" "$DATA_DIR/secrets" "$DATA_DIR/uploads" "$DATA_DIR/generations" \
    "$DATA_DIR/thumbs" "$DATA_DIR/staging" "$DATA_DIR/backups"

  if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    info "Installing application files…"
    rsync -a --delete \
      --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' \
      --exclude 'data/' "$SCRIPT_DIR/" "$INSTALL_DIR/"
  fi

  FRESH_VENV=0
  if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
    FRESH_VENV=1
  fi
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
  if [[ "$FRESH_VENV" == "1" ]]; then
    "$INSTALL_DIR/.venv/bin/python" -m pip install "$INSTALL_DIR"
  else
    # The services import pi-agenda from the venv's site-packages (src layout),
    # not from /opt/pi-agenda directly. A plain `pip install` is a no-op when
    # the project version is unchanged, so repairs and one-click updates would
    # rsync new files yet keep running the old code. Force a reinstall of the
    # app package itself while leaving third-party dependencies untouched.
    "$INSTALL_DIR/.venv/bin/python" -m pip install \
      --force-reinstall --no-deps --no-build-isolation "$INSTALL_DIR"
  fi

  PLAYER_TOKEN=""
  if [[ -r "$ENV_FILE" ]]; then
    PLAYER_TOKEN="$(sed -n 's/^PI_AGENDA_PLAYER_TOKEN=//p' "$ENV_FILE" | head -n 1)"
  fi
  if [[ -z "$PLAYER_TOKEN" ]]; then
    PLAYER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  install -m 0640 -o root -g "$APP_GROUP" /dev/null "$ENV_FILE"
  cat >"$ENV_FILE" <<EOF
PI_AGENDA_DATA_DIR=$DATA_DIR
PI_AGENDA_DB=$DATA_DIR/db.sqlite
PI_AGENDA_SECRET_FILE=$DATA_DIR/secrets/flask-secret
PI_AGENDA_HOST=0.0.0.0
PI_AGENDA_PORT=$PORT
PI_AGENDA_CACHE_PORT=$CACHE_PORT
PI_AGENDA_PLAYER_TOKEN=$PLAYER_TOKEN
PI_AGENDA_WEB_THREADS=4
PI_AGENDA_LOG_LEVEL=INFO
PI_AGENDA_VERSION_COMMIT=$SOURCE_COMMIT
EOF

  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  runuser -u "$APP_USER" -- "$INSTALL_DIR/.venv/bin/pi-agenda" init-db

  hostnamectl set-hostname pi-agenda || warn "Could not set hostname; IP address access will still work."

  info "Installing kiosk and privileged helpers…"
  install -d -o root -g root -m 0755 "$INSTALL_DIR/runtime" /usr/local/libexec
  install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$DATA_DIR/.config" "$DATA_DIR/.config/openbox"
  cat >"$DATA_DIR/.config/openbox/rc.xml" <<'OPENBOX'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <keyboard>
    <keybind key="Escape">
      <action name="Execute"><command>/usr/bin/sudo /usr/local/libexec/pi-agenda-kiosk-control exit</command></action>
    </keybind>
  </keyboard>
  <applications><application class="Chromium"><decor>no</decor><maximized>true</maximized></application></applications>
</openbox_config>
OPENBOX
  chown "$APP_USER:$APP_GROUP" "$DATA_DIR/.config/openbox/rc.xml"
  cat >"$INSTALL_DIR/runtime/kiosk-launch.sh" <<'KIOSK'
#!/usr/bin/env bash
set -Eeuo pipefail
export DISPLAY=:0
export XAUTHORITY=/var/lib/pi-agenda/.Xauthority
echo "pi-agenda-kiosk: starting X session"
xset s off
xset s noblank
openbox --config-file /var/lib/pi-agenda/.config/openbox/rc.xml &
# Hide the pointer immediately: this is a non-interactive display and a
# lingering cursor otherwise looks like a hung boot when X is up but the
# browser is still starting.
unclutter -idle 0 -root &
port="${PI_AGENDA_PORT:-8000}"
ready=0
for _ in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${port}/api/ready" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "pi-agenda-kiosk: backend not ready after 60s; launching browser anyway" >&2
fi
echo "pi-agenda-kiosk: launching chromium"
exec chromium \
  --kiosk --noerrdialogs --disable-translate --disable-infobars --no-first-run \
  --disable-dev-shm-usage --autoplay-policy=no-user-gesture-required \
  --window-size=1280,720 "http://127.0.0.1:${port}/startup"
KIOSK
  chmod 0755 "$INSTALL_DIR/runtime/kiosk-launch.sh"

  cat >/usr/local/libexec/pi-agenda-display <<'DISPLAY_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" -eq 1 && ( "$1" == "on" || "$1" == "off" ) ]] || exit 64
state="$1"
display=:0
xauthority=/var/lib/pi-agenda/.Xauthority
query="$(runuser -u pi-agenda -- env DISPLAY="$display" XAUTHORITY="$xauthority" xrandr --query 2>/dev/null || true)"
output="$(printf '%s\n' "$query" | awk '/ connected/{if ($0 ~ /[0-9]+x[0-9]+\+[0-9]+\+[0-9]+/) {print $1; found=1; exit} if (!fallback) fallback=$1} END{if (!found) print fallback}')"
if [[ -n "$output" ]]; then
  if [[ "$state" == "on" ]]; then
    runuser -u pi-agenda -- env DISPLAY="$display" XAUTHORITY="$xauthority" xrandr --output "$output" --auto
    runuser -u pi-agenda -- env DISPLAY="$display" XAUTHORITY="$xauthority" xset dpms force on || true
  else
    runuser -u pi-agenda -- env DISPLAY="$display" XAUTHORITY="$xauthority" xset dpms force off || true
    runuser -u pi-agenda -- env DISPLAY="$display" XAUTHORITY="$xauthority" xrandr --output "$output" --off
  fi
  verified="$(runuser -u pi-agenda -- env DISPLAY="$display" XAUTHORITY="$xauthority" xrandr --query 2>/dev/null | awk -v wanted="$output" '$1 == wanted {print; exit}')"
  if [[ "$state" == "off" && "$verified" =~ [0-9]+x[0-9]+\+[0-9]+\+[0-9]+ ]]; then
    echo "Display output $output still has an active signal after the off request" >&2
    exit 1
  fi
  if [[ "$state" == "on" && ! "$verified" =~ [0-9]+x[0-9]+\+[0-9]+\+[0-9]+ ]]; then
    echo "Display output $output did not regain an active signal" >&2
    exit 1
  fi
elif command -v vcgencmd >/dev/null 2>&1; then
  if [[ "$state" == "on" ]]; then
    vcgencmd display_power 1
    expected=1
  else
    vcgencmd display_power 0
    expected=0
  fi
  actual="$(vcgencmd display_power)"
  [[ "$actual" == *"=$expected"* ]] || { echo "Firmware display power state did not become $expected" >&2; exit 1; }
else
  exit 1
fi
DISPLAY_HELPER
  chmod 0755 /usr/local/libexec/pi-agenda-display

  cat >/usr/local/libexec/pi-agenda-reboot <<'REBOOT_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" -eq 0 ]] || exit 64
exec /usr/bin/systemctl reboot
REBOOT_HELPER
  chmod 0755 /usr/local/libexec/pi-agenda-reboot

  cat >/usr/local/libexec/pi-agenda-kiosk-control <<'KIOSK_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" -eq 1 && ( "$1" == "restart" || "$1" == "exit" ) ]] || exit 64
if [[ "$1" == "restart" ]]; then
  exec /usr/bin/systemctl restart pi-agenda-kiosk.service
fi
exec /usr/bin/systemd-run --quiet --collect --unit=pi-agenda-exit-to-os \
  --on-active=1s /usr/local/libexec/pi-agenda-exit-to-os
KIOSK_HELPER
  chmod 0755 /usr/local/libexec/pi-agenda-kiosk-control

  cat >/usr/local/libexec/pi-agenda-exit-to-os <<'EXIT_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
systemctl stop pi-agenda-kiosk.service
manager="$(cat /etc/pi-agenda-display-manager 2>/dev/null || true)"
if [[ -n "$manager" ]] && systemctl cat "$manager" >/dev/null 2>&1; then
  systemctl start "$manager"
else
  systemctl start getty@tty1.service
fi
EXIT_HELPER
  chmod 0755 /usr/local/libexec/pi-agenda-exit-to-os

  cat >/usr/local/libexec/pi-agenda-update <<'UPDATE_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
repo="https://github.com/bkgodwin/Pi-Agenda.git"
[[ "$#" -ge 1 ]] || exit 64
latest="$(git ls-remote --exit-code "$repo" refs/heads/main | awk '{print $1}')"
[[ "$latest" =~ ^[0-9a-f]{40}$ ]] || { echo "Could not identify the latest main commit" >&2; exit 69; }
current="$(sed -n 's/^PI_AGENDA_VERSION_COMMIT=//p' /etc/pi-agenda.env | head -n 1)"
if [[ "$1" == "check" && "$#" -eq 1 ]]; then
  printf 'current=%s\nlatest=%s\n' "${current:-unknown}" "$latest"
  exit 0
fi
[[ "$1" == "update" && "$#" -eq 2 && "$2" =~ ^[0-9a-f]{40}$ && "$2" == "$latest" ]] || exit 64
unit="pi-agenda-update-$(date +%s)"
exec systemd-run --quiet --collect --unit="$unit" /usr/local/libexec/pi-agenda-update-runner "$latest"
UPDATE_HELPER
  chmod 0755 /usr/local/libexec/pi-agenda-update

  cat >/usr/local/libexec/pi-agenda-update-runner <<'UPDATE_RUNNER'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" -eq 1 && "$1" =~ ^[0-9a-f]{40}$ ]] || exit 64
target="$1"
update_dir="$(mktemp -d /var/lib/pi-agenda/staging/update-XXXXXXXX)"
cleanup() { rm -rf -- "$update_dir"; }
trap cleanup EXIT
git clone --quiet --depth=1 --branch=main https://github.com/bkgodwin/Pi-Agenda.git "$update_dir/repo"
[[ "$(git -C "$update_dir/repo" rev-parse HEAD)" == "$target" ]] || { echo "Main changed during update; retry from Settings" >&2; exit 75; }
"$update_dir/repo/start.sh" --repair
systemctl reboot
UPDATE_RUNNER
  chmod 0755 /usr/local/libexec/pi-agenda-update-runner

cat >/usr/local/libexec/pi-agenda-service-control <<'SERVICE_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" -eq 1 && ( "$1" == "restart" || "$1" == "restore" ) ]] || exit 64
if [[ "$1" == "restart" ]]; then
  exec /usr/bin/systemd-run --quiet --collect --unit=pi-agenda-requested-restart \
    --on-active=2s /usr/bin/systemctl restart \
    pi-agenda-web.service pi-agenda-worker.service pi-agenda-cache.service pi-agenda-kiosk.service
fi
cat >/run/pi-agenda-restore.sh <<'RESTORE'
#!/usr/bin/env bash
set -Eeuo pipefail
cleanup() {
  systemctl start pi-agenda-web.service pi-agenda-cache.service pi-agenda-worker.service pi-agenda-kiosk.service || true
  rm -f /run/pi-agenda-restore.sh
}
trap cleanup EXIT
systemctl stop pi-agenda-kiosk.service pi-agenda-worker.service pi-agenda-cache.service pi-agenda-web.service
set -a
source /etc/pi-agenda.env
set +a
runuser -u pi-agenda -- /opt/pi-agenda/.venv/bin/pi-agenda restore-backup /var/lib/pi-agenda/backups/restore-pending.zip
RESTORE
chmod 0700 /run/pi-agenda-restore.sh
exec /usr/bin/systemd-run --quiet --collect --unit=pi-agenda-requested-restore \
  --on-active=2s /run/pi-agenda-restore.sh
SERVICE_HELPER
  chmod 0755 /usr/local/libexec/pi-agenda-service-control

  cat >/etc/sudoers.d/pi-agenda <<'SUDOERS'
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-display *
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-reboot
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-kiosk-control restart
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-kiosk-control exit
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-update check
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-update update *
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-service-control restart
pi-agenda ALL=(root) NOPASSWD: /usr/local/libexec/pi-agenda-service-control restore
SUDOERS
  chmod 0440 /etc/sudoers.d/pi-agenda
  visudo -cf /etc/sudoers.d/pi-agenda >/dev/null

  cat >/etc/X11/Xwrapper.config <<'XWRAPPER'
allowed_users=anybody
needs_root_rights=yes
XWRAPPER

  cat >/etc/systemd/system/pi-agenda-web.service <<EOF
[Unit]
Description=Pi-Agenda web management service
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
EnvironmentFile=$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/pi-agenda run-web
Restart=on-failure
RestartSec=3
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

  cat >/etc/systemd/system/pi-agenda-worker.service <<EOF
[Unit]
Description=Pi-Agenda background worker and scheduler
After=network.target pi-agenda-web.service
Requires=pi-agenda-web.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
EnvironmentFile=$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/pi-agenda run-worker
Restart=on-failure
RestartSec=3
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
MemoryHigh=75%
MemoryMax=90%
TasksMax=128

[Install]
WantedBy=multi-user.target
EOF

  cat >/etc/systemd/system/pi-agenda-cache.service <<EOF
[Unit]
Description=Pi-Agenda isolated archived-content server
After=local-fs.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
EnvironmentFile=$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/pi-agenda run-cache
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadOnlyPaths=$DATA_DIR/generations

[Install]
WantedBy=multi-user.target
EOF

  XORG_BIN="$(command -v Xorg)"
  [[ -n "$XORG_BIN" ]] || fail "Xorg was installed but its executable could not be found."
  cat >/etc/systemd/system/pi-agenda-kiosk.service <<EOF
[Unit]
Description=Pi-Agenda fullscreen kiosk
After=pi-agenda-web.service pi-agenda-cache.service systemd-user-sessions.service
Requires=pi-agenda-web.service pi-agenda-cache.service
Conflicts=getty@tty1.service display-manager.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
EnvironmentFile=$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/xinit $INSTALL_DIR/runtime/kiosk-launch.sh -- $XORG_BIN :0 vt1 -keeptty -nolisten tcp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  display_manager=""
  if [[ -L /etc/systemd/system/display-manager.service ]]; then
    display_manager="$(basename "$(readlink -f /etc/systemd/system/display-manager.service)")"
    printf '%s\n' "$display_manager" >/etc/pi-agenda-display-manager
    systemctl disable --now "$display_manager" >/dev/null 2>&1 || true
  else
    rm -f /etc/pi-agenda-display-manager
  fi
  systemctl set-default multi-user.target
  systemctl disable --now getty@tty1.service >/dev/null 2>&1 || true
  systemctl enable avahi-daemon pi-agenda-web pi-agenda-worker pi-agenda-cache pi-agenda-kiosk
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

password_is_set() {
  "$INSTALL_DIR/.venv/bin/pi-agenda" status 2>/dev/null | grep -q 'Password configured: yes'
}

if [[ "$MODE" == "reset-password" || "$MODE" == "install" || "$MODE" == "repair" ]] && { [[ "$MODE" == "reset-password" ]] || ! password_is_set; }; then
  if [[ -t 0 ]]; then
    while true; do
      read -r -s -p "Management password (minimum 8 characters): " password
      printf '\n'
      read -r -s -p "Confirm management password: " confirmation
      printf '\n'
      [[ "$password" == "$confirmation" ]] || { warn "Passwords do not match."; continue; }
      [[ "${#password}" -ge 8 ]] || { warn "Password is too short."; continue; }
      printf '%s\n' "$password" | runuser -u "$APP_USER" -- "$INSTALL_DIR/.venv/bin/pi-agenda" set-password --password-stdin
      unset password confirmation
      break
    done
  else
    generated_password="$(runuser -u "$APP_USER" -- "$INSTALL_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(15))')"
    printf '%s\n' "$generated_password" | runuser -u "$APP_USER" -- "$INSTALL_DIR/.venv/bin/pi-agenda" set-password --password-stdin
    warn "Generated one-time management password: $generated_password"
    warn "Change it immediately after signing in."
    unset generated_password
  fi
fi

if [[ "$MODE" == "reset-password" ]]; then
  info "Password reset complete."
  exit 0
fi

info "Starting Pi-Agenda services…"
systemctl restart pi-agenda-web pi-agenda-cache pi-agenda-worker pi-agenda-kiosk

ready=0
for _ in $(seq 1 45); do
  if curl --fail --silent "http://127.0.0.1:${PORT}/api/ready" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" -eq 1 ]] || fail "Web service did not become ready. Run: journalctl -u pi-agenda-web --no-pager -n 100"

info "Pi-Agenda is running on ${MODEL:-Raspberry Pi}."
management_addresses
printf '%s\n' "The kiosk will start automatically on every boot without a sign-in."
printf '%s\n' "The management address will appear on the display for at least five seconds during startup."
