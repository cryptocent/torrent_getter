#!/usr/bin/env bash
# Optional, idempotent local swapfile provisioning for Ubuntu/ext4 hosts.
# This changes the Linux host, not any Docker container or network share.
set -Eeuo pipefail

SWAPFILE=/swapfile
SIZE_GIB=8
RESERVE_GIB=4
SWAPPINESS=10

usage() {
  cat <<'EOF'
Usage: bash full-stack/setup-swap.sh [size-gib]

Creates a persistent local ext4 /swapfile (default 8 GiB), permissions 0600,
sets vm.swappiness=10 and activates swap. Does not touch remote media shares.

IMPORTANT: Expand the VM disk/filesystem FIRST. Requires requested swap size
plus 4 GiB spare space on the root filesystem. Existing swapfiles are preserved.
Run as a non-root user with sudo access.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
[[ $# -le 1 ]] || { usage >&2; exit 2; }
if [[ $# -eq 1 ]]; then SIZE_GIB="$1"; fi

[[ "$EUID" -ne 0 ]] || die "Run without sudo; the script requests sudo as necessary."
[[ "$SIZE_GIB" =~ ^[1-9][0-9]*$ ]] || die "Swap size must be a positive whole number of GiB."
(( SIZE_GIB <= 64 )) || die "Swap size must be 64 GiB or less."
command -v sudo >/dev/null || die "sudo is required."
command -v findmnt >/dev/null || die "findmnt is required."
command -v swapon >/dev/null || die "swapon is required."
command -v mkswap >/dev/null || die "mkswap is required."
command -v fallocate >/dev/null || die "fallocate is required."
sudo -v

fs_type="$(findmnt --target / -n -o FSTYPE)"
[[ "$fs_type" == ext4 ]] || die "Root filesystem is $fs_type; this helper supports ext4 only."

# Check for an existing swapfile before allocating or altering anything.
if sudo test -e "$SWAPFILE" || sudo test -L "$SWAPFILE"; then
  [[ "$(sudo stat -c '%F' "$SWAPFILE")" == "regular file" ]] ||
    die "$SWAPFILE exists but is not a regular file; refusing to overwrite."
  existing_type="$(sudo blkid -p -s TYPE -o value "$SWAPFILE" 2>/dev/null || true)"
  [[ "$existing_type" == swap ]] ||
    die "$SWAPFILE already exists but is not recognised as swap; refusing to overwrite."
  echo "Existing valid $SWAPFILE found. Leaving its size and contents intact."
else
  available="$(df -B1 --output=avail / | tail -n1 | tr -d ' ')"
  [[ "$available" =~ ^[0-9]+$ ]] || die "Could not determine free space on root filesystem."
  required=$(( (SIZE_GIB + RESERVE_GIB) * 1024 * 1024 * 1024 ))
  if (( available < required )); then
    printf 'Free root disk space: %s GiB; required for new swap + safety reserve: %s GiB.\n' \
      "$(( available / 1024 / 1024 / 1024 ))" "$(( SIZE_GIB + RESERVE_GIB ))" >&2
    die "Expand the VM and its ext4 filesystem before creating the swapfile."
  fi
  echo "Creating $SIZE_GIB GiB local swap at $SWAPFILE."
  sudo install -m 0600 /dev/null "$SWAPFILE"
  if ! sudo fallocate -l "${SIZE_GIB}G" "$SWAPFILE"; then
    sudo rm -f -- "$SWAPFILE"
    die "Could not allocate swapfile."
  fi
  sudo chmod 0600 "$SWAPFILE"
  if ! sudo mkswap "$SWAPFILE"; then
    die "mkswap failed. Inspect $SWAPFILE manually."
  fi
fi

sudo chmod 0600 "$SWAPFILE"
if ! sudo swapon --noheadings --raw --output NAME | grep -Fxq "$SWAPFILE"; then
  sudo swapon "$SWAPFILE"
fi

if ! awk '$1=="/swapfile" && $3=="swap" { found=1 } END{ exit(found ? 0 : 1) }' /etc/fstab; then
  printf '%s\n' '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
printf 'vm.swappiness=%s\n' "$SWAPPINESS" |
  sudo tee /etc/sysctl.d/99-media-stack-swap.conf >/dev/null
sudo sysctl -w "vm.swappiness=$SWAPPINESS" >/dev/null

echo
echo "Swap status:"
swapon --show
free -h
echo "Persisted via /etc/fstab; vm.swappiness=$SWAPPINESS."
