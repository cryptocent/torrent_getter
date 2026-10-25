"""Submit saved Torrent Getter links to the existing qBittorrent Web API.

HTTP torrent links are fetched by Torrent Getter first, then uploaded as files
rather than asking qBittorrent to fetch URLs with potentially incorrect MIME types.
Only explicitly permitted public HTTPS origins may be fetched.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests


MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3


class QBitSubmissionError(Exception):
    """A safe, user-facing error from the download submission workflow."""


def _check_download_url(url: str, allowed_hosts: set[str]) -> None:
    if not url or len(url) > 4096:
        raise QBitSubmissionError("Torrent URL is empty or too long.")

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise QBitSubmissionError("Torrent links must use HTTPS.")

    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise QBitSubmissionError("Torrent link contains unsupported credentials or port.")

    host = parsed.hostname.rstrip(".").lower()
    if host not in allowed_hosts:
        raise QBitSubmissionError(
            "Torrent source is not permitted. Add its hostname to QBIT_TORRENT_ALLOWED_HOSTS."
        )

    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise QBitSubmissionError("Could not resolve the torrent source.") from exc

    if not addresses or any(
        not ipaddress.ip_address(result[4][0]).is_global for result in addresses
    ):
        raise QBitSubmissionError("Torrent source resolved to a non-public address.")


def fetch_torrent_file(
    url: str, allowed_hosts: set[str], session: requests.Session | None = None
) -> bytes:
    """Fetch bounded torrent metadata; do not trust source Content-Type headers."""
    if not allowed_hosts:
        raise QBitSubmissionError("No approved torrent-source hosts have been configured.")

    client = session or requests.Session()
    # Do not inherit unrelated HTTP_PROXY settings for untrusted source URLs.
    client.trust_env = False

    for _ in range(MAX_REDIRECTS + 1):
        _check_download_url(url, allowed_hosts)
        try:
            response = client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=(5, 20),
                allow_redirects=False,
                stream=True,
            )
            try:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    if not location:
                        raise QBitSubmissionError("Torrent source redirected without a location.")
                    url = urljoin(url, location)
                    continue

                response.raise_for_status()
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > MAX_TORRENT_BYTES:
                        raise QBitSubmissionError("Torrent metadata exceeds the 10 MB size limit.")
                    chunks.append(chunk)
            finally:
                response.close()
        except requests.RequestException as exc:
            raise QBitSubmissionError("Could not retrieve torrent metadata from the source.") from exc

        payload = b"".join(chunks)
        # The qBittorrent backend performs final parsing and validation.
        if not payload.startswith(b"d") or b"4:info" not in payload:
            raise QBitSubmissionError("Source did not return recognisable torrent metadata.")
        return payload

    raise QBitSubmissionError("Torrent source redirected too many times.")


def submit_to_qbittorrent(
    *,
    link_url: str,
    link_type: str,
    base_url: str,
    username: str,
    password: str,
    category: str,
    allowed_hosts: set[str],
) -> None:
    """Submit a saved magnet or downloaded .torrent directly through qBittorrent."""
    if not base_url or not password:
        raise QBitSubmissionError("qBittorrent connection settings are incomplete.")

    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
        raise QBitSubmissionError("qBittorrent URL must be an HTTP(S) server origin.")

    if link_type == "torrent":
        torrent_data = fetch_torrent_file(link_url, allowed_hosts)
    elif link_type == "magnet" and link_url.startswith("magnet:?"):
        torrent_data = None
    else:
        raise QBitSubmissionError("The selected link is not a supported torrent or magnet.")

    root = base_url.rstrip("/")
    client = requests.Session()
    client.trust_env = False
    headers = {"Origin": root, "Referer": root + "/"}
    try:
        auth = client.post(
            root + "/api/v2/auth/login",
            data={"username": username, "password": password},
            headers=headers,
            timeout=(5, 15),
            allow_redirects=False,
        )
        if auth.status_code != 200 or auth.text.strip() != "Ok.":
            raise QBitSubmissionError("qBittorrent authentication failed; verify its credentials.")

        form = {"category": category, "autoTMM": "true"}
        kwargs = (
            {"files": {"torrents": ("download.torrent", torrent_data, "application/x-bittorrent")}}
            if torrent_data is not None
            else {"data": {**form, "urls": link_url}}
        )
        if torrent_data is not None:
            kwargs["data"] = form

        added = client.post(
            root + "/api/v2/torrents/add",
            headers=headers,
            timeout=(5, 30),
            allow_redirects=False,
            **kwargs,
        )
        if added.status_code not in (200, 202) or added.text.strip() == "Fails.":
            raise QBitSubmissionError(
                f"qBittorrent rejected the torrent (HTTP {added.status_code})."
            )
        # qBittorrent 5.3 may return structured per-torrent results.
        try:
            result = added.json()
        except ValueError:
            result = None
        if isinstance(result, dict):
            failed = result.get("failure_count", 0)
            successful = result.get("success_count", 0)
            pending = result.get("pending_count", 0)
            if isinstance(failed, int) and failed and not successful and not pending:
                raise QBitSubmissionError("qBittorrent reported that the torrent addition failed.")
    except requests.RequestException as exc:
        raise QBitSubmissionError("Could not communicate with qBittorrent.") from exc
    finally:
        client.close()
