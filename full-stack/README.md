# Full media stack: two on-demand Windows shares

The complete Docker Compose stack includes Jellyfin, Jellyseerr, Radarr, Sonarr,
Prowlarr, Torrent Getter, iQbit, qBittorrent, Bazarr, Uptime Kuma and
FastChannels. This is separate from Torrent Getter’s original Compose setup.

The films and TV are stored on the MINI-PC, not the Linux Docker host:

| Windows share | Existing content | Path inside containers |
| --- | --- | --- |
| `\\192.168.1.6\4tb` | MOVIES, TV, HER STORY, Pictures | `/network/4tb` |
| `\\192.168.1.6\5tb` | MOVIES, TV SHOWS, RECORDINGS, temp | `/network/5tb` |

## Install on your Jellyfin server

Run as the `jelly` Linux account, not root:

```bash
cd /opt/apps/torrent_getter
sudo chown -R jelly:jelly /opt/apps/torrent_getter
git switch full-stack-setup
git pull --ff-only origin full-stack-setup

# Set up the two SMB shares. Prompts for Windows SMB login.
bash full-stack/setup-network-shares.sh

# Now deploy the complete stack.
bash full-stack/setup.sh --install-docker
```

The SMB password is requested privately and stored only in
`/etc/samba/credentials-media-stack` (root-only, mode 0600), not Git or
the project `.env`. The same Windows user must have permissions on both
shares. If Windows actually supports SMB guest access, use `--guest`
instead. The script uses CIFS v3.0.

The setup installs `cifs-utils` if required, safely adds a managed
block to `/etc/fstab` (with a timestamped backup), creates systemd
automounts for `/mnt/media-shares/4tb` and `/mnt/media-shares/5tb`,
and makes their permanent parent a shared mount before Docker starts.

**The MINI-PC may be offline.** systemd does not require either SMB
connection at boot, and the Docker services bind the always-present
`/mnt/media-shares` parent with Linux `rslave` propagation. They
therefore start without the shares and see mounted shares when available
later. SMB reads/writes will fail or time out while the MINI-PC is offline,
but that does not prevent the operating system or containers starting.

The backing directories are owned by root and made non-writable for the
regular application users. This prevents silent local writes into fake
media directories if a mount unexpectedly disappears. Container
application configurations remain **local**, under
`/opt/apps/torrent_getter/full-stack/config` by default.

## First-run library configuration

Use these **exact paths** in the applications:

| Application | Paths |
| --- | --- |
| Jellyfin Movies | `/network/4tb/MOVIES` and `/network/5tb/MOVIES` |
| Jellyfin TV | `/network/4tb/TV` and `/network/5tb/TV SHOWS` |
| Jellyfin optional library | `/network/4tb/HER STORY` |
| Radarr movie roots | `/network/4tb/MOVIES` and `/network/5tb/MOVIES` |
| Sonarr TV roots | `/network/4tb/TV` and `/network/5tb/TV SHOWS` |
| qBittorrent downloads | `/network/5tb/temp` |
| Bazarr media | Same `/network/...` paths as Radarr and Sonarr |

In Jellyfin add both movie paths to the Movies library and both TV
paths to the TV library. Jellyfin's mount is read-only. In Radarr/Sonarr,
choose which of the two roots each title belongs to.

**qBittorrent requires a first-run change:** configure its *Default Save
Path* and its temporary download directory under `/network/5tb/temp`
before adding downloads. The container's default `/downloads` points
to the non-writable network **parent** deliberately, preventing an
unconfigured qBittorrent from writing downloads to the Docker host disk.
Only write into an actual SMB share subdirectory.

Radarr, Sonarr, qBittorrent and Bazarr all use the same `/network`
mapping, so they do not need remote path mappings. Imports across shares
can require copying instead of hardlinking. If the Windows system
disappears during a library scan, avoid automatic deletion workflows
which interpret an inaccessible share as deleted media.

The scripts do not create folders, move files or perform write tests on
the Windows computer. If you want separate download directories under
`5tb\temp`, create them in Windows first and select them in qBittorrent.

## Link the applications

1. qBittorrent: get its temporary admin password via container logs,
   then change it. Configure downloads on the network share.
2. Radarr/Sonarr: connect the download client `qbittorrent:8080`,
   using qBittorrent credentials and relevant download categories.
3. Prowlarr: add `http://radarr:7878` and `http://sonarr:8989`
   with API keys.
4. Torrent Getter: create a permitted crawl. Add RSS indexer
   `http://torrent_getter:5000/rss/movies.xml` to Radarr.
5. Bazarr: connect to `http://radarr:7878` and `http://sonarr:8989`.
6. Jellyseerr: link to `http://jellyfin:8096`, Radarr and Sonarr.
7. iQbit: accessible on port 8081; proxies to `qbittorrent:8080`.
8. FastChannels: use port 5523 under `/admin/` to configure feeds
   and copy the resulting M3U/XMLTV URLs into Jellyfin Live TV.
9. Uptime Kuma: create your admin and add monitors for the services
   and optionally the networked Windows computer.

## Web ports

| App | Port | App | Port |
| --- | ---: | --- | ---: |
| Jellyfin | 8096 | Jellyseerr | 5055 |
| Radarr | 7878 | Sonarr | 8989 |
| Prowlarr | 9696 | Torrent Getter | 5010 |
| iQbit | 8081 | qBittorrent | 8080 |
| Bazarr | 6767 | Uptime Kuma | 3001 |
| FastChannels | 5523 | | |

Set `HOST_BIND_IP` in `full-stack/.env` to your **Linux server's**
actual LAN IP for LAN access; `192.168.1.6` is the Windows storage
computer, not the Linux server. Default localhost binding is intentional.
Do not expose management web interfaces directly to the Internet.
The torrent peer port is 6881/TCP and UDP unless changed.

## Check and maintain

```bash
# Local mount state without requiring Windows to be online:
findmnt --mountpoint /mnt/media-shares -o TARGET,PROPAGATION
systemctl status "$(systemd-escape --path --suffix=automount /mnt/media-shares/4tb)"
systemctl status "$(systemd-escape --path --suffix=automount /mnt/media-shares/5tb)"

# These two commands trigger an SMB connection; run when Windows is online:
ls -la /mnt/media-shares/4tb/MOVIES
ls -la "/mnt/media-shares/5tb/TV SHOWS"

# Compose validation and container status:
bash full-stack/setup.sh --check
docker compose --env-file full-stack/.env -f full-stack/compose.yml ps
docker compose --env-file full-stack/.env -f full-stack/compose.yml logs qbittorrent

# Recreate after updating mounts, without deleting local app settings:
docker compose --env-file full-stack/.env -f full-stack/compose.yml up -d --force-recreate --build
```

The remote shares are **optional during startup**, not guaranteed to
complete every file operation while disconnected. SMB timeout handling
can pause individual I/O calls; reconnecting allows subsequent attempts.
Do not use `docker compose down -v` unless you intend to remove volumes.
Back up both your local config and the external media independently.
Use only content and sources which you are authorised to access.

## Optional: create a persistent 8 GiB swapfile after resizing the VM

The initial Jellyfin VM only had **1.6 GiB** available on a 29 GiB ext4
root filesystem; Jellyfin itself requires at least 2 GiB free in /config/data.
Do **not** allocate swap before expanding that local filesystem to roughly
100 GiB or otherwise making enough free local space.

Once the virtual disk has been expanded (and Ubuntu's LVM/ext4 filesystem
has been grown), run as the normal Linux user:

```bash
cd /opt/apps/torrent_getter
git pull --ff-only origin full-stack-setup
df -h /
free -h
bash full-stack/setup-swap.sh 8
swapon --show
```

The optional helper creates **/swapfile** on the local ext4 root filesystem,
requires at least **12 GiB free** for the 8 GiB file and a 4 GiB reserve,
restricts its permissions to 0600, enables it immediately, and adds it to
/etc/fstab for reboot persistence. It also configures
`vm.swappiness=10` in /etc/sysctl.d/99-media-stack-swap.conf.

Existing valid /swapfile content is preserved; a suspicious or non-swap
file at this path causes a safe failure instead of overwriting it.
No swap is allocated on either Windows SMB share. The swapfile is optional
and is not created by the normal full-stack installer.


## FastChannels geographic region notice (Australia)

FastChannels looks up the server's public IP and may report that it is in AU
rather than the US. This **does not mean Docker or Jellyfin is broken**.
Many upstream channel sources are US-only. Disabling the check only
hides the notice; it does not make geo-restricted streams playable.

To turn off the warning on this host, set `GEO_CHECK_ENABLED=0` in
`full-stack/.env`, then recreate only FastChannels:

```bash
cd /opt/apps/torrent_getter/full-stack
sudo docker compose --env-file .env -f compose.yml up -d --no-deps --force-recreate fastchannels
sudo docker compose --env-file .env -f compose.yml exec fastchannels printenv GEO_CHECK_ENABLED
```

Use the public LAN base URL `http://192.168.1.23:5523` in FastChannels
Admin → Settings. The Docker-internal hostname `fastchannels` is
appropriate when Jellyfin requests a playlist, but stream URLs included
in the M3U need to be resolvable by the device actually playing them.

For Australian sources, use built-in source region settings where offered.
For example, Samsung TV Plus and LG Channels support configurable countries.
Test individual channels with **Inspect** before building a Jellyfin feed.
Some sources require subscriptions, US provider accounts, or DRM-capable
playback and remain unavailable regardless of the geo-check flag.

## Australian FastChannels source preset

A conservative source setup is included. It uses FastChannels' own HTTP API
instead of manipulating its database. It enables **Plex** and **Free Networks**
and queues fresh scrapes. It does not disable other sources or reset credentials.
Availability and clear-stream playback must still be verified per channel.

Run from the Linux host after the stack has started:

```bash
cd /opt/apps/torrent_getter
git pull --ff-only origin full-stack-setup

# Preview source changes, does not write anything:
python3 full-stack/configure-fastchannels-au.py

# Enable the starter sources and queue scrapes:
python3 full-stack/configure-fastchannels-au.py --apply

# Optional trial of Pluto's "Home/Server IP" feed. Australia is not
# one of Pluto's explicit supported regional feeds in this application:
python3 full-stack/configure-fastchannels-au.py --apply --try-pluto-local
```

For an installation whose management UI is not at HOST_BIND_IP:5523, supply
`--url http://192.168.1.23:5523`. The script requires the server's HTTP
admin interface to be reachable; do not expose this API directly to the
Internet. Source scrapes happen asynchronously.

**Provider limits:** The current Samsung scraper offers US, CA, GB, DE,
FR, ES, IT, KR, IN, AT and CH, but **not AU**. The current LG scraper
hardcodes US; TCL only lists US and CA. Pluto's named region feeds
do not include AU, although it has a server-IP option which you may trial.
The preset does not pretend unsupported AU configurations exist.

After scraping, use the channel **Inspect** action for playback checks
and build a clean feed at **Admin → Feeds**. In Jellyfin choose an M3U
tuner and an XMLTV guide using that feed's displayed URLs.

## Optional FastChannels-only VPN

This project does not enable VPN routing by default. For isolation, use a
VPN client container such as Gluetun and share its network namespace only
with FastChannels, leaving Jellyfin, Torrent Getter, *arr services and
qBittorrent on normal Docker networking. Ensure the VPN has a working kill
switch and that Jellyfin can still reach FastChannels on its internal port.

**Urban VPN limitation:** Urban VPN's official platform list includes
Windows, macOS, Android, iOS and browser extensions, but not a native Linux
client. Although Urban states it uses OpenVPN internally, that does **not**
mean the account provides an exportable `.ovpn` profile or credentials for
third-party clients. An Urban login or Chrome extension alone cannot be
entered into a Gluetun container. Ask Urban support whether your account
provides standard OpenVPN/WireGuard client configuration for Linux or
routers before attempting this. Do not send VPN credentials in chat.

With a VPN provider that supplies a valid OpenVPN/WireGuard profile, a
separate VPN gateway container can be configured in a future revision,
with FastChannels' port 5523 exposed through that gateway. A VPN does not
guarantee US-only streams will play, and streaming provider account,
regional and DRM restrictions still apply.
