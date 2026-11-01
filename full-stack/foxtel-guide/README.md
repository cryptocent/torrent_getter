# Foxtel-channel guide bridge for Jellyfin

**Scope:** a functional, self-hosted M3U/XMLTV adapter for authorised clear streams, **not** a Foxtel Now login or DRM playback implementation.

A paid Foxtel Now username/password does not itself supply Jellyfin-compatible video streams. This service does not log in to Foxtel, store Foxtel credentials, scrape protected video, circumvent DRM, or record Foxtel Now. **Never enter a Foxtel password into this service.**

Features:
- Downloads and caches an XMLTV channel guide with a six-hour default refresh, last-good fallback, and retry on failures.
- Publishes token-protected M3U and XMLTV endpoints and lists EPG channel IDs.
- Includes only channels explicitly mapped to independently licensed, directly playable HTTP(S) streams.
- Runs as a non-root isolated container with a read-only configuration mount. It does not alter existing services or media volumes.

The default guide URL is a publicly accessible, third-party source: https://i.mjh.nz/Foxtel/epg.xml.gz. It is **not affiliated with Foxtel**. Availability, licensing, accuracy and compatibility with the Foxtel Now channel lineup are not guaranteed. Only use guide data and stream URLs you are authorised to use.

## Set up (in a checkout of this feature branch)

Run these commands from your full-stack directory, for example /srv/media-ssd/apps/torrent_getter/full-stack. Adapt paths where necessary.

    mkdir -p config/foxtel-guide
    cp foxtel-guide/channels.example.json config/foxtel-guide/channels.json
    python3 -c 'import secrets; print("FOXTEL_GUIDE_FEED_TOKEN=" + secrets.token_urlsafe(36))'
    nano .env

Paste the generated FOXTEL_GUIDE_FEED_TOKEN assignment into the private .env. Never commit the token. Edit config/foxtel-guide/channels.json to specify actual EPG IDs and **authorised clear stream URLs**. To start with no channels, use {"channels":[]}. If CONFIG_ROOT is customised in .env, place channels.json inside that path's foxtel-guide directory instead.

The structure of each entry is:
- tvg_id: guide channel ID shown by the /catalog endpoint
- name: your channel display name
- stream_url: your authorised, non-DRM HTTP(S) stream address
- group (optional): Jellyfin group label
- logo (optional): channel logo URL

The example stream URL is intentionally invalid and must be replaced. Guide data alone cannot produce playable Foxtel Now video.

## Start only the new container

    cd /srv/media-ssd/apps/torrent_getter/full-stack
    docker compose --env-file .env -f compose.yml -f compose.foxtel-guide.yml config --quiet
    docker compose --env-file .env -f compose.yml -f compose.foxtel-guide.yml up -d --no-deps --build foxtel-guide
    docker compose --env-file .env -f compose.yml -f compose.foxtel-guide.yml ps foxtel-guide

This leaves Jellyfin, FastChannels, qBittorrent and the other containers untouched. The bridge joins the same media network as Jellyfin. The default published port is 8765, bound to HOST_BIND_IP (localhost unless your stack specifies otherwise).

Query endpoints using the published IP and token:

    curl -fsS "http://127.0.0.1:8765/catalog?token=YOUR_FEED_TOKEN"
    curl -fsS "http://127.0.0.1:8765/status?token=YOUR_FEED_TOKEN"

The /catalog endpoint displays exact tvg_id values from the XMLTV feed. Configure only guide IDs and independently authorised clear streams you intend to use. The bridge re-reads channels.json on each request, so mappings can be updated without restarting.

## Configure Jellyfin Live TV

In Jellyfin Dashboard > Live TV, add an M3U Tuner at:

    http://foxtel-guide:8765/channels.m3u?token=YOUR_FEED_TOKEN

Then add an XMLTV guide provider at:

    http://foxtel-guide:8765/guide.xml?token=YOUR_FEED_TOKEN

Refresh the guide. Both addresses are Docker-internal and only suitable for Jellyfin on the same media network. For browsers outside Docker, use the host IP and published port. The stream URLs in the M3U must be reachable and playable by Jellyfin.

| Endpoint | Function |
| --- | --- |
| /healthz | Liveness check, no token required |
| /status?token=... | Mappings count, guide cache status, errors and capabilities |
| /catalog?token=... | Guide channel IDs and names |
| /channels.m3u?token=... | M3U for explicitly configured streams |
| /guide.xml?token=... | Filtered XMLTV matching the configured stream IDs |

All endpoints except /healthz require the configured token. The Authorization: Bearer YOUR_FEED_TOKEN header works instead of the URL query token for manual callers.

## Optional .env settings

    FOXTEL_GUIDE_FEED_TOKEN=REPLACE_WITH_LONG_RANDOM_TOKEN
    FOXTEL_GUIDE_SOURCE=https://i.mjh.nz/Foxtel/epg.xml.gz
    FOXTEL_GUIDE_REFRESH_SECONDS=21600
    FOXTEL_GUIDE_PORT=8765

The token must contain at least 24 characters or startup fails. Remote guide downloads must use HTTPS. For a local guide instead, mount an XMLTV file in the configuration directory and set FOXTEL_GUIDE_SOURCE=/data/filename.xml or /data/filename.xml.gz. The bridge limits compressed downloads to 8 MiB and uncompressed XML to 32 MiB. Failure triggers a retry after five minutes, and the last good guide stays available.

## Testing and diagnostics

    python3 -m unittest discover -s full-stack/foxtel-guide/tests -v
    docker build -t foxtel-guide-test full-stack/foxtel-guide

Tests use synthetic channels and synthetic XMLTV only. They do not verify Foxtel credentials or streaming.

If /channels.m3u is empty, create mappings in channels.json. HTTP 401 means the feed token is invalid. HTTP 400 signals a bad mapping or invalid source; HTTP 503 means the guide could not load and there is no cached guide. If Jellyfin lists a channel but cannot play it, check the independent upstream URL and encryption: this bridge neither proxies nor decrypts streams, and will not make protected Foxtel Now content playable.

References:
- https://jellyfin.org/docs/general/server/live-tv/
- https://www.foxtel.com.au/now/support/how-it-works/eligible-devices.html
- https://www.foxtel.com.au/now/about/terms.html

## Optional Kodi Foxtel playback companion

This **does not make Foxtel Now playable through Jellyfin**. It is a
controlled way for the Docker service to request playback on a separately
running Kodi device that has a working and authorised Foxtel add-on.

**Prerequisites:** a Kodi installation with HTTP JSON-RPC enabled, the Kodi
Foxtel Go or Foxtel Now add-on already installed, and a working authenticated
subscription within that Kodi add-on. The historical Foxtel add-ons have been
deprecated; no currently working add-on or login is guaranteed. Do not share
your Foxtel credentials with this Docker service.

1. In Kodi, enable *Settings > Services > Control > Allow remote control via
   HTTP* and use a dedicated strong Kodi HTTP password. Restrict it to LAN.
2. From Kodi IPTV Merge, export or locate the M3U generated for your own
   Foxtel Kodi add-on. Copy it to
   `full-stack/config/foxtel-guide/kodi-foxtel.m3u`. Kodi user-data locations
   and IPTV Merge versions differ. This export contains `plugin://` URLs.
3. Set the following in the private `full-stack/.env`, using the Kodi
   machine's actual LAN address, not `127.0.0.1` inside Docker:

       KODI_RPC_URL=http://YOUR_KODI_LAN_IP:8080/jsonrpc
       KODI_RPC_USERNAME=kodi
       KODI_RPC_PASSWORD=YOUR_STRONG_KODI_HTTP_PASSWORD

4. Rebuild only the bridge:

       docker compose --env-file .env -f compose.yml -f compose.foxtel-guide.yml up -d --no-deps --build foxtel-guide

5. Open the optional visual Kodi remote page in a trusted LAN browser:

       http://YOUR_DOCKER_HOST_IP:8765/kodi/ui?token=YOUR_FEED_TOKEN

   The interface reports Kodi availability and provides Play on Kodi buttons.
   Keep this token-bearing URL private.

6. List the imported channel catalogue (the provider add-on URL is not
   returned, only a channel identifier and metadata):

       curl -fsS -H "Authorization: Bearer YOUR_FEED_TOKEN" http://YOUR_DOCKER_HOST_IP:8765/kodi/channels

7. Request playback for a channel ID in the response. The video opens **on
   the Kodi screen**, not in Jellyfin:

       curl -fsS -X POST -H "Authorization: Bearer YOUR_FEED_TOKEN" -H "Content-Type: application/json" -d '{"channel_id":"PASTE_CHANNEL_ID"}' http://YOUR_DOCKER_HOST_IP:8765/kodi/play

8. Check that Kodi is reachable:

       curl -fsS -H "Authorization: Bearer YOUR_FEED_TOKEN" http://YOUR_DOCKER_HOST_IP:8765/kodi/status

Only URLs targeting `plugin.video.foxtel.go` and `plugin.video.foxtel.now`
can be imported and launched. Other Kodi plug-ins, arbitrary files, and
arbitrary remote URLs are excluded. Playback requests use Kodi JSON-RPC
`Player.Open`. No credentials or video are relayed through the bridge.

The `/kodi/play` endpoint requires a POST, JSON and the bridge bearer token.
If the installed Kodi Foxtel add-on is broken or its authentication is blocked,
the bridge cannot repair that upstream provider issue. Kodi JSON-RPC
acknowledgement confirms only that the **playback request was sent**, not that
the stream successfully played. Keep Kodi's RPC interface isolated from the
public Internet.

The existing M3U/XMLTV Jellyfin endpoints remain separate from this feature:
they include only mapped, independently licensed clear streams, not Kodi
`plugin://` entries or Foxtel DRM streams.
