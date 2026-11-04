#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
if [[ ! -f .env ]]; then
  echo "Missing full-stack/.env; configure the feed token first." >&2
  exit 1
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 1
fi

DC=(docker compose --env-file .env -f compose.yml -f compose.foxtel-guide.yml -f compose.kodi.yml)
"${DC[@]}" config --quiet

PATHS="$("${DC[@]}" config --format json | python3 -c '
import json,sys
data=json.load(sys.stdin)
for service in ("kodi","foxtel-guide"):
    target=next((v for v in data["services"][service]["volumes"]
                 if v.get("type")=="bind" and v.get("target")=="/data"),None)
    if not target:
        raise SystemExit("Missing data mount for "+service)
    path=target.get("source","")
    if not path.startswith("/"):
        raise SystemExit("Expected absolute mount source for "+service)
    print(path)
')"
mapfile -t DIRS <<< "$PATHS"
if [[ "${#DIRS[@]}" -ne 2 ]]; then
  echo "Unable to determine Kodi and bridge storage paths" >&2
  exit 1
fi

KODI_DIR="${DIRS[0]}"
GUIDE_DIR="${DIRS[1]}"
mkdir -p "$KODI_DIR/.kodi/userdata" "$GUIDE_DIR"
if [[ ! -e "$KODI_DIR/.kodi/userdata/advancedsettings.xml" ]]; then
  printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
    '<advancedsettings></advancedsettings>' \
    > "$KODI_DIR/.kodi/userdata/advancedsettings.xml"
  chmod 600 "$KODI_DIR/.kodi/userdata/advancedsettings.xml"
  echo "Initialised Kodi with local SQLite databases."
else
  echo "Preserved existing Kodi configuration."
fi
if [[ ! -e "$GUIDE_DIR/channels.json" ]]; then
  printf '{"channels":[]}\n' > "$GUIDE_DIR/channels.json"
  chmod 600 "$GUIDE_DIR/channels.json"
  echo "Initialised empty stream channel mapping."
else
  echo "Preserved existing channel mappings."
fi
if [[ "${1:-}" == "--prepare-only" ]]; then
  echo "Directories ready. No containers started."
  exit 0
elif [[ "${1:-}" != "" ]]; then
  echo "Usage: bash setup-kodi.sh [--prepare-only]" >&2
  exit 2
fi
"${DC[@]}" up -d --no-deps --build kodi foxtel-guide
"${DC[@]}" ps kodi foxtel-guide
echo "Kodi browser UI defaults to http://127.0.0.1:8000 on the Docker host."
echo "Use an SSH tunnel, not a publicly exposed unauthenticated noVNC port."
echo "Enable Kodi HTTP JSON-RPC and configure its password inside the Kodi GUI."
echo "Kodi installation does not restore the discontinued Foxtel add-on."
