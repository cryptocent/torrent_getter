# Full media-stack installer (optional)

This is an optional Docker Compose deployment for the full stack shown in the
dashboard. It does **not** replace the repository's root compose.yml or modify
an existing installation.

| Service | Local web URL / port | Purpose |
| --- | --- | --- |
| Jellyfin | http://127.0.0.1:8096 | Media server |
| Jellyseerr | http://127.0.0.1:5055 | Media requests |
| Radarr | http://127.0.0.1:7878 | Movie library |
| Sonarr | http://127.0.0.1:8989 | TV library |
| Prowlarr | http://127.0.0.1:9696 | Indexer manager |
| Torrent Getter | http://127.0.0.1:5010 | This repository's catalog and RSS |
| iQbit | http://127.0.0.1:8081 | Alternative qBittorrent web UI |
| qBittorrent | http://127.0.0.1:8080 | Download client |
| Bazarr | http://127.0.0.1:6767 | Subtitles |
| Uptime Kuma | http://127.0.0.1:3001 | Monitoring |
| FastChannels | http://127.0.0.1:5523/admin/ | Live-TV feed administration |

The installer supports modern Debian and Ubuntu Linux, with Docker Engine and
the Docker Compose v2 plugin. Docker is installed with apt **only** when
`--install-docker` is specified and a required component is missing.

## Quick start

```bash
git clone --branch feature/full-stack-setup https://github.com/cryptocent/torrent_getter.git
cd torrent_getter
bash full-stack/setup.sh --install-docker
```

On its first run, the installer creates `full-stack/.env` with the current
user and group IDs, prepares folders, checks Compose syntax, pulls the
published images, builds Torrent Getter from **this checkout** and starts the
stack. It does not overwrite existing settings or media.

**If your media already lives on another disk, configure that mount BEFORE
starting the stack.** For example:

```bash
# After cloning, create your local settings first:
cp full-stack/.env.example full-stack/.env
sed -i "s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" full-stack/.env
nano full-stack/.env
bash full-stack/setup.sh --install-docker
```

The two most important settings are:

```dotenv
MEDIA_ROOT=/mnt/media
CONFIG_ROOT=/srv/media-stack/config
HOST_BIND_IP=127.0.0.1
```

An absolute `MEDIA_ROOT` must already exist, preventing a misspelled or
unmounted disk path from silently redirecting downloads onto the system disk.
By default, `MEDIA_ROOT=./data` and `CONFIG_ROOT=./config` are directories
under `full-stack/`. These directories are gitignored. Be sure the chosen
user has permission to write to both volume roots.

To allow access from another device on the **trusted LAN**, change
`HOST_BIND_IP` to the server's actual LAN IP address. Do not use an
Internet-facing IP. All web UIs are bound to localhost by default; the
separate qBittorrent peer-connection TCP/UDP port (6881) binds to all
interfaces by default. Restrict it with `TORRENT_BIND_IP` if desired.
Protect the server with a firewall and a private network or VPN. Torrent
Getter and FastChannels administration should not be exposed publicly.

## First-run wiring

1. **qBittorrent**: open port 8080. Obtain the initial `admin` password
   with the log command below and set a strong permanent password immediately.
   Configure completed downloads as `/data/torrents/complete` and incomplete
   downloads as `/data/torrents/incomplete`.
2. **Radarr**: set movie root to `/data/media/movies`. Add download client
   qBittorrent with host `qbittorrent`, port `8080` and the qBittorrent
   credentials. Apply the same download category on both sides if using one.
3. **Sonarr**: set TV root to `/data/media/tv` and connect to
   `qbittorrent:8080` with its own category.
4. **Prowlarr**: add Radarr at `http://radarr:7878` and Sonarr at
   `http://sonarr:8989`, supplying their API keys. Configure only lawful
   and permitted indexer sources.
5. **Torrent Getter**: in Radarr's *Settings > Indexers* add a Torrent RSS
   Feed at `http://torrent_getter:5000/rss/movies.xml`. Enable RSS sync
   and *Allow Zero Size* as described in the project README. The feed is
   empty until this application has saved permitted releases.
6. **Bazarr**: configure Sonarr `http://sonarr:8989` and Radarr
   `http://radarr:7878` with their API keys. All three share the same
   `/data` mount for consistent path mapping.
7. **Jellyfin**: configure media folders `/media/movies` and
   `/media/tv`, mounted read-only from the shared host library.
8. **Jellyseerr**: link to `http://jellyfin:8096`, and then to
   `http://radarr:7878` and `http://sonarr:8989` with their API keys.
9. **iQbit**: the standalone interface proxies to
   `http://qbittorrent:8080`. Use your normal qBittorrent credentials.
10. **FastChannels**: visit port 5523 at `/admin/`. Set its server URL
    to the LAN address devices can reach, create a feed and add its
    M3U/XMLTV endpoints to Jellyfin's live TV tuner/guide settings.
    Use the actual feed URLs displayed by FastChannels.
11. **Uptime Kuma**: create the administrator account, then add HTTP
    monitors for the other applications.

Docker service names above resolve on the Compose `media` network.
Hostnames such as `radarr` and `torrent_getter` are for **container-to-container**
links only; use the server IP from a desktop or phone.

## Operations

Run from the repository root:

```bash
# Preview installation without starting containers
bash full-stack/setup.sh --check

# Prepare host directories but do not launch
bash full-stack/setup.sh --no-start

# Update images and restart, keeping all persistent volumes
docker compose --env-file full-stack/.env -f full-stack/compose.yml pull \
  jellyfin jellyseerr radarr sonarr prowlarr iqbit qbittorrent bazarr uptime-kuma fastchannels
docker compose --env-file full-stack/.env -f full-stack/compose.yml up -d --build

# Status, qBittorrent initial password and logs
docker compose --env-file full-stack/.env -f full-stack/compose.yml ps
docker compose --env-file full-stack/.env -f full-stack/compose.yml logs qbittorrent
docker compose --env-file full-stack/.env -f full-stack/compose.yml logs -f torrent_getter

# Stop containers without deleting application files
docker compose --env-file full-stack/.env -f full-stack/compose.yml down
```

If Docker requires elevated access, prefix the `docker` commands with
`sudo`. Run setup.sh as a **regular user**, never with sudo. Do not use
`down -v` unless you intentionally want to delete named volumes.

## Storage, permissions and migration

The stack uses one consistent shared data mount in Radarr, Sonarr,
qBittorrent and Bazarr:

```text
MEDIA_ROOT/
  media/
    movies/
    tv/
  torrents/
    complete/
    incomplete/
```

This permits normal hardlinks/atomic moves on a single filesystem. If the
source files span different filesystems or network shares, hardlink
importing might not work; use copies or adapt paths appropriately.
`CONFIG_ROOT` stores persistent application settings, the Torrent Getter
SQLite database and posters, Jellyfin cache, Uptime Kuma data and
FastChannels state/ADB keys. Back up **both** roots before upgrades.

This installer does not move or migrate any existing application data and
does not delete existing containers. If default ports are occupied, Compose
will fail with a binding error; stop/reconfigure the conflicting service
rather than deleting its data.

Images are updated **manually**, not through Watchtower. Avoid unattended
container upgrades on a stack that handles persistent media and databases.
Pinned version tags or image digests are recommended for production.

Use this stack only with media and sources that you are authorised to access
and download. Application installation does not grant usage rights.

## Sources

- [Torrent Getter](https://github.com/cryptocent/torrent_getter)
- [Jellyfin](https://jellyfin.org/docs/general/installation/container/)
- [iQbit standalone WebUI](https://github.com/ntoporcov/iQbit)
- [FastChannels container](https://github.com/kineticman/FastChannels)
- [LinuxServer qBittorrent](https://docs.linuxserver.io/images/docker-qbittorrent/)
