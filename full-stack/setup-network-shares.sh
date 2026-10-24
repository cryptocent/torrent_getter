#!/usr/bin/env bash
# Set up optional Windows SMB shares with systemd automount and Docker propagation.
# Run as the normal Linux user; sudo is used only for OS configuration.
set -Eeuo pipefail

ROOT=/mnt/media-shares
SERVER=192.168.1.6
CREDS=/etc/samba/credentials-media-stack
START_MARKER='# BEGIN TORRENT_GETTER_MEDIA_STACK_SHARES'
END_MARKER='# END TORRENT_GETTER_MEDIA_STACK_SHARES'
GUEST=0

usage() {
  cat <<'EOF'
Usage: bash full-stack/setup-network-shares.sh [--guest]

Set up //192.168.1.6/4tb and //192.168.1.6/5tb as optional, on-demand
CIFS mounts at /mnt/media-shares/{4tb,5tb}. Containers can start if the
Windows computer is offline, and no local media directories are substituted.

Prompts once for a Windows/SMB username and password (shared by both shares);
credentials are root-only at /etc/samba/credentials-media-stack.
  --guest    Use guest SMB access instead (only if your server permits it).
EOF
}

for arg in "$@"; do
  case "$arg" in
    --guest) GUEST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
[[ "$EUID" -ne 0 ]] || die "Run without sudo. The script requests sudo when required."
command -v sudo >/dev/null || die "sudo required"
command -v systemctl >/dev/null || die "systemd is required"
command -v mountpoint >/dev/null || die "util-linux mountpoint is required"
command -v systemd-escape >/dev/null || die "systemd-escape is required"
command -v apt-get >/dev/null || die "Debian/Ubuntu apt-get required"
sudo -v

echo "Installing CIFS/SMB support if needed..."
if ! command -v mount.cifs >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y cifs-utils
fi

uid="$(id -u)"
gid="$(id -g)"
if [[ "$GUEST" == 1 ]]; then
  auth_opt=guest
  echo "Using guest access. Only select this if the Windows shares allow guest SMB."
else
  read -r -p "SMB username on $SERVER: " smb_user
  [[ -n "$smb_user" && "$smb_user" != *$'\r'* && "$smb_user" != *'='* ]] || die "Invalid SMB username"
  read -r -s -p "SMB password: " smb_pass
  echo
  [[ -n "$smb_pass" && "$smb_pass" != *$'\r'* ]] || die "Empty/invalid password"
  sudo install -d -m 0700 /etc/samba
  # Password never goes into shell history or process arguments.
  printf 'username=%s\npassword=%s\n' "$smb_user" "$smb_pass" |
    sudo sh -c 'umask 077; cat > /etc/samba/credentials-media-stack'
  sudo chmod 600 "$CREDS"
  unset smb_pass smb_user
  auth_opt="credentials=$CREDS"
fi

# The backing directory is intentionally not user-writable.
# A lost automount must never become a substitute local download directory.
sudo install -d -m 0555 -o root -g root "$ROOT"
for share in 4tb 5tb; do
  target="$ROOT/$share"
  if mountpoint -q "$target"; then
    echo "Existing mount at $target, preserving it."
  else
    sudo install -d -m 0555 -o root -g root "$target"
  fi
done

# Create an always-present, shared mount parent. Docker rslave bind mounts
# then receive later SMB mount/unmount events, including after reboot.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<'EOF'
#!/bin/sh
set -eu
root=/mnt/media-shares
[ -d "$root" ] || exit 1
if ! mountpoint -q "$root"; then
  mount --bind "$root" "$root"
fi
mount --make-rshared "$root"
EOF
sudo install -m 0755 -o root -g root "$tmp" /usr/local/sbin/media-stack-mount-propagation

cat > "$tmp" <<'EOF'
[Unit]
Description=Shared parent mount for on-demand media shares
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/media-stack-mount-propagation
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 -o root -g root "$tmp" /etc/systemd/system/media-stack-propagation.service

# Docker starts after the shared parent is prepared, even after reboot.
sudo install -d -m 0755 /etc/systemd/system/docker.service.d
cat > "$tmp" <<'EOF'
[Unit]
Wants=media-stack-propagation.service
After=media-stack-propagation.service
EOF
sudo install -m 0644 -o root -g root "$tmp" /etc/systemd/system/docker.service.d/20-media-stack-propagation.conf

# Keep unrelated fstab lines unchanged. Refuse to replace someone else's mount.
sudo cat /etc/fstab > "$tmp"
for share in 4tb 5tb; do
  if awk -v start="$START_MARKER" -v end="$END_MARKER" -v t="$ROOT/$share" '
    $0 == start { inside=1; next }
    $0 == end { inside=0; next }
    !inside && $1 !~ /^#/ && $2==t { found=1 }
    END { exit(found ? 0 : 1) }
  ' "$tmp"; then
    die "A non-managed fstab entry already uses $ROOT/$share. Review it manually."
  fi
done
clean="$(mktemp)"
trap 'rm -f "$tmp" "$clean"' EXIT
awk -v start="$START_MARKER" -v end="$END_MARKER" '
  $0==start { ignore=1; next }
  $0==end { ignore=0; next }
  !ignore { print }
' "$tmp" > "$clean"

cat >> "$clean" <<EOF
$START_MARKER
//$SERVER/4tb $ROOT/4tb cifs $auth_opt,uid=$uid,gid=$gid,forceuid,forcegid,iocharset=utf8,file_mode=0664,dir_mode=0775,vers=3.0,soft,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=12s 0 0
//$SERVER/5tb $ROOT/5tb cifs $auth_opt,uid=$uid,gid=$gid,forceuid,forcegid,iocharset=utf8,file_mode=0664,dir_mode=0775,vers=3.0,soft,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=12s 0 0
$END_MARKER
EOF

sudo cp -a /etc/fstab "/etc/fstab.media-stack-backup.$(date +%Y%m%d%H%M%S)"
sudo install -m 0644 -o root -g root "$clean" /etc/fstab
sudo systemctl daemon-reload
sudo systemctl enable --now media-stack-propagation.service

for share in 4tb 5tb; do
  unit="$(systemd-escape --path --suffix=automount "$ROOT/$share")"
  sudo systemctl enable --now "$unit"
  systemctl is-active --quiet "$unit" || die "Automount unit $unit failed"
done

echo
echo "SMB automounts installed. Windows need not be online for Linux or Docker to boot."
echo "Parent propagation: $(findmnt --mountpoint "$ROOT" -n -o PROPAGATION)"
echo "Do not put SMB credentials in the project's .env file."
echo
echo "When the MINI-PC is online, check:"
echo "  ls -la '$ROOT/4tb/MOVIES' '$ROOT/5tb/TV SHOWS'"
echo "To see automount state without waking the remote server:"
echo "  systemctl status '$(systemd-escape --path --suffix=automount "$ROOT/4tb")'"
