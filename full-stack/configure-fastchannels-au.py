#!/usr/bin/env python3
"""Conservative AU starter preset for FastChannels' documented HTTP API.

No credentials, SQLite edits, or platform-region guesses. Dry-run by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def base_url(explicit: str | None) -> str:
    if explicit:
        url = explicit.rstrip("/")
    else:
        env = Path(__file__).resolve().parent / ".env"
        ip = "127.0.0.1"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("HOST_BIND_IP="):
                    ip = line.split("=", 1)[1].strip().strip('"').strip("'") or ip
        if ip in ("0.0.0.0", "::"):
            ip = "127.0.0.1"
        url = f"http://{ip}:5523"
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only direct HTTP FastChannels URLs are supported, without credentials.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Use a base URL, e.g. http://192.168.1.23:5523")
    return url


def request_api(base: str, path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(
        base + path,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=12) as resp:
            return json.load(resp)
    except HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"{method} {path}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up a conservative FastChannels starter lineup for Australia."
    )
    parser.add_argument("--url", help="FastChannels base URL (defaults to HOST_BIND_IP from full-stack/.env)")
    parser.add_argument("--apply", action="store_true", help="Actually enable sources and trigger scrapes")
    parser.add_argument(
        "--try-pluto-local", action="store_true",
        help="Also trial Pluto in 'server IP' region mode; not guaranteed to work in Australia.",
    )
    args = parser.parse_args()
    try:
        url = base_url(args.url)
        sources = request_api(url, "/api/sources")
        if not isinstance(sources, list):
            raise RuntimeError("Unexpected /api/sources response; refusing to modify anything.")
        by_name = {s.get("name"): s for s in sources if isinstance(s, dict)}
        selections = [
            ("plex", None, "Anonymous service; AU availability varies"),
            ("free_networks", None, "International independent networks; inspect for AU playback"),
        ]
        if args.try_pluto_local:
            selections.append((
                "pluto", {"auth_mode": "anonymous", "country_codes": "local"},
                "Experimental: server-IP feed; Australia is not a listed regional feed",
            ))

        print(f"FastChannels: {url}")
        print("MODE:", "APPLY" if args.apply else "DRY RUN (no changes)")
        print("Preserving all other sources and configuration, including sign-in details.")
        failed = 0
        for name, settings, note in selections:
            src = by_name.get(name)
            if not src or not isinstance(src.get("id"), int):
                print(f"SKIP {name}: missing source or invalid id")
                failed += 1
                continue
            ident = src["id"]
            current_enabled = bool(src.get("is_enabled"))
            config_patch = None
            if settings is not None:
                cfg = request_api(url, f"/api/sources/{ident}/config")
                schema_keys = {f.get("key") for f in cfg.get("schema", []) if isinstance(f, dict)}
                if not settings.keys() <= schema_keys:
                    print(f"SKIP {name}: expected config fields unavailable on installed image")
                    failed += 1
                    continue
                vals = cfg.get("values", {})
                config_patch = {key: value for key, value in settings.items() if vals.get(key) != value}

            changes = []
            if config_patch:
                changes.append(f"config {config_patch}")
            if not current_enabled:
                changes.append("enable source")
            changes.append("queue scrape")
            print(f"{name} ({note}): {', '.join(changes)}")
            if not args.apply:
                continue

            if config_patch:
                response = request_api(url, f"/api/sources/{ident}/config", "POST", config_patch)
                if response.get("status") != "saved":
                    raise RuntimeError(f"{name} config update was not acknowledged: {response}")
            if not current_enabled:
                result = request_api(url, f"/api/sources/{ident}", "PATCH", {"is_enabled": True})
                if result.get("is_enabled") is not True:
                    raise RuntimeError(f"{name} enable was not acknowledged: {result}")
            request_api(url, f"/api/sources/{ident}/run", "POST")
            print(f"  {name}: scrape queued")

        print()
        if args.apply:
            print("Open /admin/sources to review scrapes, /admin/channels to Inspect streams,")
            print("and /admin/feeds to create a dedicated Jellyfin M3U and XMLTV feed.")
        else:
            print("No changes made. Re-run with --apply after reviewing the list.")
        print("Samsung currently lacks an AU selection; TCL is US/CA-only; LG is US-fixed.")
        return 1 if failed else 0
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
