from __future__ import annotations

from unittest.mock import Mock

import pytest

import qbit_client
from qbit_client import QBitSubmissionError, fetch_torrent_file, submit_to_qbittorrent
from app import _safe_items_return, create_app
from db import connect, create_run, record_item
from scraper import ScrapeConfig, ScrapedItem


VALID_TORRENT = b"d4:infod4:name4:Testee"


class DownloadResponse:
    def __init__(self, body=VALID_TORRENT, status_code=200, location=None):
        self.body = body
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=UTF-8"}
        if location is not None:
            self.headers["Location"] = location

    def raise_for_status(self):
        if self.status_code >= 400:
            raise qbit_client.requests.HTTPError("HTTP error")

    def iter_content(self, chunk_size=65536):
        yield self.body

    def close(self):
        pass


def allow_public_dns(monkeypatch):
    monkeypatch.setattr(
        qbit_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


def test_http_fetch_accepts_mislabelled_bencoded_file(monkeypatch):
    allow_public_dns(monkeypatch)
    fake = Mock()
    fake.get.return_value = DownloadResponse()
    result = fetch_torrent_file("https://files.example.com/movie.torrent", {"files.example.com"}, fake)
    assert result == VALID_TORRENT
    assert fake.get.call_args.kwargs["allow_redirects"] is False


def test_http_fetch_rejects_unapproved_source(monkeypatch):
    allow_public_dns(monkeypatch)
    with pytest.raises(QBitSubmissionError, match="not permitted"):
        fetch_torrent_file("https://internal.example/movie.torrent", {"files.example.com"})


def test_http_fetch_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(
        qbit_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("192.168.1.23", 443))],
    )
    with pytest.raises(QBitSubmissionError, match="non-public"):
        fetch_torrent_file("https://files.example.com/movie.torrent", {"files.example.com"})


def test_http_fetch_checks_redirect_target(monkeypatch):
    allow_public_dns(monkeypatch)
    fake = Mock()
    fake.get.return_value = DownloadResponse(status_code=302, location="https://other.example/file")
    with pytest.raises(QBitSubmissionError, match="not permitted"):
        fetch_torrent_file("https://files.example.com/movie.torrent", {"files.example.com"}, fake)
    assert fake.get.call_count == 1


def test_http_fetch_rejects_large_file(monkeypatch):
    allow_public_dns(monkeypatch)
    fake = Mock()
    fake.get.return_value = DownloadResponse(body=b"d" + b"x" * (10 * 1024 * 1024))
    with pytest.raises(QBitSubmissionError, match="exceeds"):
        fetch_torrent_file("https://files.example.com/movie.torrent", {"files.example.com"}, fake)


def test_submit_torrent_uploads_bytes_with_movies_category(monkeypatch):
    monkeypatch.setattr(qbit_client, "fetch_torrent_file", lambda *args, **kwargs: VALID_TORRENT)

    login = Mock(status_code=200, text="Ok.")
    accepted = Mock(status_code=200, text="Ok.")
    accepted.json.side_effect = ValueError()
    session = Mock()
    session.post.side_effect = [login, accepted]
    monkeypatch.setattr(qbit_client.requests, "Session", lambda: session)

    submit_to_qbittorrent(
        link_url="https://files.example.com/movie.torrent",
        link_type="torrent",
        base_url="http://qbittorrent:8080",
        username="admin",
        password="secret",
        category="MOVIES",
        allowed_hosts={"files.example.com"},
    )
    assert session.post.call_count == 2
    request = session.post.call_args
    assert request.kwargs["data"] == {"category": "MOVIES", "autoTMM": "true"}
    assert request.kwargs["files"]["torrents"][1] == VALID_TORRENT
    assert request.args[0].endswith("/api/v2/torrents/add")


def test_submit_magnet_uses_urls_form_not_file(monkeypatch):
    login = Mock(status_code=200, text="Ok.")
    accepted = Mock(status_code=200, text="Ok.")
    accepted.json.side_effect = ValueError()
    session = Mock()
    session.post.side_effect = [login, accepted]
    monkeypatch.setattr(qbit_client.requests, "Session", lambda: session)

    submit_to_qbittorrent(
        link_url="magnet:?xt=urn:btih:abc123",
        link_type="magnet",
        base_url="http://qbittorrent:8080",
        username="admin",
        password="secret",
        category="MOVIES",
        allowed_hosts=set(),
    )
    assert "files" not in session.post.call_args.kwargs
    assert session.post.call_args.kwargs["data"]["urls"].startswith("magnet:")


def test_send_route_requires_csrf_and_saved_link(tmp_path, monkeypatch):
    database = str(tmp_path / "test.sqlite")
    app = create_app({
        "TESTING": True,
        "DATABASE": database,
        "SECRET_KEY": "secure-test-key",
        "QBIT_URL": "http://qbittorrent:8080",
        "QBIT_USERNAME": "admin",
        "QBIT_PASSWORD": "secret",
        "QBIT_TORRENT_ALLOWED_HOSTS": "files.example.com",
    })
    client = app.test_client()
    with connect(database) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org"))
        record_item(
            conn,
            run_id,
            ScrapedItem(
                source_url="https://example.org",
                detail_url="https://example.org/title",
                link_url="https://files.example.com/movie.torrent",
                link_type="torrent",
                name="Movie",
            ),
        )
        item_id = conn.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
        conn.commit()

    page = client.get(f"/items/{item_id}")
    assert b"Send" in page.data and b"qBittorrent" in page.data
    assert client.post(f"/downloads/{item_id}/send", data={"csrf_token": "fake"}).status_code == 403

    with client.session_transaction() as sess:
        csrf_token = sess["qbit_send_csrf"]

    calls = []
    monkeypatch.setattr(
        "app.submit_to_qbittorrent",
        lambda **kwargs: calls.append(kwargs),
    )
    response = client.post(
        f"/downloads/{item_id}/send",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Torrent submitted to qBittorrent" in response.data
    assert calls[0]["category"] == "MOVIES"
    assert calls[0]["link_type"] == "torrent"


def test_send_disabled_without_configured_secret(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.sqlite"),
        "SECRET_KEY": "dev",
        "QBIT_URL": "http://qbittorrent:8080",
        "QBIT_PASSWORD": "secret",
    })
    client = app.test_client()
    assert client.post("/downloads/1/send").status_code == 503


def test_original_links_and_send_actions_both_render(tmp_path):
    database = str(tmp_path / "test.sqlite")
    app = create_app({
        "TESTING": True,
        "DATABASE": database,
        "SECRET_KEY": "secure-test-key",
        "QBIT_URL": "http://qbittorrent:8080",
        "QBIT_PASSWORD": "secret",
    })
    client = app.test_client()
    with connect(database) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org"))
        for link_type, link_url in (
            ("magnet", "magnet:?xt=urn:btih:abc123"),
            ("torrent", "https://files.example.com/example.torrent"),
        ):
            record_item(conn, run_id, ScrapedItem(
                source_url="https://example.org",
                detail_url="https://example.org/title",
                link_url=link_url,
                link_type=link_type,
                name="Example",
            ))
        item_id = conn.execute("SELECT id FROM items WHERE link_type = 'magnet'").fetchone()[0]
        conn.commit()

    for path in ("/items", f"/items/{item_id}"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Open magnet" in response.data
        assert b"Download torrent" in response.data
        assert b"Send magnet to qBittorrent" in response.data
        assert b"Send torrent to qBittorrent" in response.data
        assert b'href="magnet:?xt=urn:btih:abc123"' in response.data
        assert b'href="https://files.example.com/example.torrent"' in response.data
        assert b'name="return_to"' in response.data


def test_qbittorrent_error_is_shown_on_original_browse_page(tmp_path, monkeypatch):
    database = str(tmp_path / "test.sqlite")
    app = create_app({
        "TESTING": True, "DATABASE": database,
        "SECRET_KEY": "secure-test-key",
        "QBIT_URL": "http://qbittorrent:8080",
        "QBIT_PASSWORD": "secret",
    })
    client = app.test_client()
    with connect(database) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org"))
        record_item(conn, run_id, ScrapedItem(
            source_url="https://example.org",
            detail_url="https://example.org/title",
            link_url="magnet:?xt=urn:btih:abc123",
            link_type="magnet", name="Example",
        ))
        item_id = conn.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
        conn.commit()

    client.get("/items")
    with client.session_transaction() as sess:
        token = sess["qbit_send_csrf"]

    def failed_submission(**_kwargs):
        raise QBitSubmissionError("qBittorrent authentication failed; verify its credentials.")

    monkeypatch.setattr("app.submit_to_qbittorrent", failed_submission)
    response = client.post(
        f"/downloads/{item_id}/send",
        data={"csrf_token": token, "return_to": "/items?display=grid&sort=title_asc"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"qBittorrent authentication failed" in response.data
    assert b"Browse saved links" in response.data


def test_send_redirect_targets_must_be_local_browse_or_detail_pages():
    assert _safe_items_return("/items?page=2") == "/items?page=2"
    assert _safe_items_return("/items/2399") == "/items/2399"
    for unsafe in (
        "https://example.org", "//example.org", "/admin",
        "/items/../../admin", "/items\\\\other", "/items#fragment",
    ):
        assert _safe_items_return(unsafe) is None
