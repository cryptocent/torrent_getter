#!/usr/bin/env bash
# Deploy the optional, isolated media stack. Run as a regular Linux user.
set -Eeuo pipefail

STACK_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMPOSE_FILE="$STACK_DIR/compose.yml"
ENV_FILE="$STACK_DIR/.env"
INSTALL_DOCKER=0
CHECK_ONLY=0
NO_START=0

usage() {
  cat <<'EOF'
Usage: bash full-stack/setup.sh [--install-docker] [--check] [--no-start]

  --install-docker  Install Docker Engine and Compose v2 with apt if missing.
                    Debian/Ubuntu only; may prompt for sudo.
  --check           Validate settings and Compose syntax without starting apps.
  --no-start        Create persistent directories and validate, but do not start.
  --help            Show this help.

Run as your normal non-root user. Existing .env, configs and media are never
overwritten. Edit full-stack/.env before launching if you need other disk paths
or LAN access. All management web UIs bind to localhost by default.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
notice() { printf '\n==> %s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --install-docker) INSTALL_DOCKER=1 ;;
    --check) CHECK_ONLY=1 ;;
    --no-start) NO_START=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "Unknown option: $arg" ;;
  esac
done

[[ "$EUID" -ne 0 ]] || die "Run as a regular user, not with sudo. The installer will request sudo only to install Docker."
command -v realpath >/dev/null || die "realpath (coreutils) is required."
[[ -f "$COMPOSE_FILE" ]] || die "Missing $COMPOSE_FILE"

if [[ ! -e "$ENV_FILE" ]]; then
  cp "$STACK_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  sed -i \
    -e "s/^PUID=.*/PUID=$(id -u)/" \
    -e "s/^PGID=.*/PGID=$(id -g)/" \
    "$ENV_FILE"
  notice "Created $ENV_FILE with UID $(id -u), GID $(id -g)."
  printf 'Review HOST_BIND_IP and CONFIG_ROOT. Run setup-network-shares.sh before launching.\n'
else
  notice "Keeping existing $ENV_FILE unchanged."
fi

read_setting() {
  local key="$1" value
  value="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  value="${value%$'\r'}"
  printf '%s' "$value"
}

normalise_root() {
  local field="$1" input="$2"
  [[ -n "$input" ]] || die "$field cannot be empty"
  [[ "$input" != *:* && "$input" != *$'\n'* ]] || die "$field contains unsupported characters"
  case "$input" in
    /*) realpath -m -- "$input" ;;
    ./*) realpath -m -- "$STACK_DIR/$input" ;;
    *) die "$field must be an absolute path or begin with ./, got: $input" ;;
  esac
}

PUID="$(read_setting PUID)"
PGID="$(read_setting PGID)"
[[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]] || die "PUID/PGID must be numeric"
if [[ "$PUID" != "$(id -u)" || "$PGID" != "$(id -g)" ]]; then
  printf 'WARNING: PUID/PGID (%s:%s) differ from this account (%s:%s). Verify volume ownership.\n' \
    "$PUID" "$PGID" "$(id -u)" "$(id -g)"
fi

RAW_CONFIG="$(read_setting CONFIG_ROOT)"
CONFIG_ROOT="$(normalise_root CONFIG_ROOT "$RAW_CONFIG")"
RAW_NETWORK="$(read_setting NETWORK_ROOT)"
NETWORK_ROOT="$(normalise_root NETWORK_ROOT "${RAW_NETWORK:-/mnt/media-shares}")"
[[ "$CONFIG_ROOT" != "/" ]] || die "CONFIG_ROOT must not be /"
[[ "$NETWORK_ROOT" == "/mnt/media-shares" ]] ||
  die "This SMB setup uses NETWORK_ROOT=/mnt/media-shares. Update .env and run setup-network-shares.sh"

verify_share_setup() {
  command -v findmnt >/dev/null || die "findmnt (util-linux) is required"
  command -v systemd-escape >/dev/null || die "systemd-escape is required"
  command -v systemctl >/dev/null || die "systemctl is required"
  [[ -d "$NETWORK_ROOT" ]] ||
    die "Missing $NETWORK_ROOT: first run bash full-stack/setup-network-shares.sh"

  local prop share target unit load_state unit_state
  prop="$(findmnt --mountpoint "$NETWORK_ROOT" -n -o PROPAGATION || true)"
  [[ "$prop" == shared* ]] ||
    die "$NETWORK_ROOT is not a shared mount (found: ${prop:-none}). Run bash full-stack/setup-network-shares.sh"

  # A systemd automount unit is the correct offline-ready indicator. A CIFS
  # filesystem should NOT have to be connected for this validation to pass.
  # findmnt's FSTYPE alone can be misleading with stacked autofs/CIFS mounts.
  for share in 4tb 5tb; do
    target="$NETWORK_ROOT/$share"
    unit="$(systemd-escape --path --suffix=automount "$target")"
    load_state="$(systemctl show --property=LoadState --value "$unit" 2>/dev/null || true)"
    unit_state="$(systemctl is-active "$unit" 2>/dev/null || true)"

    if [[ "$load_state" == "loaded" && "$unit_state" != "active" ]]; then
      notice "$unit is installed but not active. Starting the on-demand trigger (does not connect to Windows)."
      if command -v sudo >/dev/null 2>&1; then
        sudo systemctl start "$unit" ||
          printf 'WARNING: Could not start %s\n' "$unit" >&2
      fi
      unit_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    fi

    if [[ "$unit_state" != "active" ]]; then
      printf 'SMB automount check failed for %s (unit: %s, loaded: %s, state: %s)\n' \
        "$target" "$unit" "${load_state:-unknown}" "${unit_state:-unknown}" >&2
      printf 'Inspect: systemctl status "%s" --no-pager\n' "$unit" >&2
      printf 'If missing, run: bash full-stack/setup-network-shares.sh\n' >&2
      die "Cannot safely expose $target to Docker. No local-disk fallback will be used."
    fi
    printf '  %s: automount active; remote share may be online or offline.\n' "$target"
  done
}


have_compose() { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }
install_packages() {
  [[ "$INSTALL_DOCKER" -eq 1 ]] || die "Docker Engine and Compose v2 are required. Rerun with --install-docker, or install them first."
  command -v apt-get >/dev/null 2>&1 || die "Automatic Docker installation supports only Debian/Ubuntu (apt)."
  command -v sudo >/dev/null 2>&1 || die "sudo is required to install Docker."
  notice "Installing missing Docker components via apt"
  sudo apt-get update
  if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get install -y docker.io
  fi
  if ! docker compose version >/dev/null 2>&1; then
    sudo apt-get install -y docker-compose-v2 ||
      sudo apt-get install -y docker-compose-plugin ||
      die "Could not install Compose v2. See https://docs.docker.com/compose/install/linux/"
  fi
  sudo systemctl enable --now docker
  have_compose || die "Docker Compose v2 is still unavailable."
}

if ! have_compose; then
  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    die "Docker Compose v2 is needed for --check; install it first."
  fi
  install_packages
fi

# Some hosts require sudo until their Docker group membership takes effect.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
  else
    die "Cannot access the Docker daemon. Start Docker and grant your user permission, or configure sudo."
  fi
fi

dc() {
  "${DOCKER[@]}" compose \
    --project-directory "$STACK_DIR" \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" "$@"
}

notice "Validating Docker Compose configuration"
dc config --quiet

notice "Checking both SMB automount units (no Windows connection required)"
verify_share_setup

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  printf 'Validation passed. No services were started.\n'
  exit 0
fi

notice "Preparing LOCAL persistent configuration directories"
mkdir -p \
  "$CONFIG_ROOT/jellyfin/config" "$CONFIG_ROOT/jellyfin/cache" \
  "$CONFIG_ROOT/jellyseerr" "$CONFIG_ROOT/radarr" \
  "$CONFIG_ROOT/sonarr" "$CONFIG_ROOT/prowlarr" \
  "$CONFIG_ROOT/torrent_getter/posters" "$CONFIG_ROOT/qbittorrent" \
  "$CONFIG_ROOT/bazarr" "$CONFIG_ROOT/uptime-kuma" \
  "$CONFIG_ROOT/fastchannels/data" "$CONFIG_ROOT/fastchannels/adb"

[[ -w "$CONFIG_ROOT/torrent_getter" ]] ||
  die "Configuration directory must be writable by the installing user."
printf 'LOCAL configuration: %s\nSMB network parent: %s\n' "$CONFIG_ROOT" "$NETWORK_ROOT"
printf 'NOTE: SMB shares need not be online, but downloads/imports require them.\n'

if [[ "$NO_START" -eq 1 ]]; then
  printf '\nDirectories are ready. To start:\n'
  printf 'docker compose --project-directory "%s" --env-file "%s" -f "%s" up -d --build\n' \
    "$STACK_DIR" "$ENV_FILE" "$COMPOSE_FILE"
  exit 0
fi

notice "Pulling images"
dc pull jellyfin jellyseerr radarr sonarr prowlarr iqbit qbittorrent bazarr uptime-kuma fastchannels

notice "Building Torrent Getter and launching the stack"
dc up -d --build
dc ps

HOST_BIND_IP="$(read_setting HOST_BIND_IP)"
[[ -n "$HOST_BIND_IP" ]] || HOST_BIND_IP=127.0.0.1
printf '\nWeb interfaces on %s (HTTP):\n' "$HOST_BIND_IP"
printf '  Jellyfin       :8096\n  Jellyseerr     :5055\n  Radarr         :7878\n'
printf '  Sonarr         :8989\n  Prowlarr       :9696\n  Torrent Getter :5010\n'
printf '  iQbit          :8081\n  qBittorrent    :8080\n  Bazarr         :6767\n'
printf '  Uptime Kuma   :3001\n  FastChannels   :5523/admin/\n'
printf '\nFor the qBittorrent temporary admin password, run:\n'
printf '  docker compose --env-file "%s" -f "%s" logs qbittorrent\n' "$ENV_FILE" "$COMPOSE_FILE"
printf '\nSee full-stack/README.md for first-run linking, storage paths and security.\n'
