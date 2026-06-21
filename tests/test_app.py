from app import _load_env_file, _server_config_from_env, create_app
from db import (
    connect,
    create_run,
    enqueue_detail_link,
    get_detail_links_for_run,
    get_run,
    init_db,
    mark_detail_link_completed,
    mark_detail_link_done,
    record_item,
)
from scraper import DetailLink, ScrapeConfig, ScrapedItem



def test_server_config_reads_env_file_without_overriding_existing_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "APP_HOST=0.0.0.0\nAPP_PORT=5000\nAPP_DEBUG=true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("APP_DEBUG", raising=False)
    monkeypatch.setenv("APP_PORT", "7000")

    _load_env_file(env_path)
    config = _server_config_from_env()

    assert config == {"host": "0.0.0.0", "port": 7000, "debug": True}
def test_stop_run_marks_non_active_run_stopping(tmp_path):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    with connect(str(database_path)) as conn:
        run_id = create_run(
            conn,
            ScrapeConfig(
                start_url="https://example.org/archive",
                pagination_template="?page={page}",
                page_start=1,
                page_end=2,
            ),
        )

    response = client.post(f"/runs/{run_id}/stop")

    assert response.status_code == 302
    with connect(str(database_path)) as conn:
        run = get_run(conn, run_id)
    assert run["status"] == "stopping"
    assert run["stop_requested_at"]


def test_stop_all_marks_non_active_runs_stopping(tmp_path):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    with connect(str(database_path)) as conn:
        first_run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/one"))
        second_run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/two"))

    response = client.post("/runs/stop-all")

    assert response.status_code == 302
    with connect(str(database_path)) as conn:
        first_run = get_run(conn, first_run_id)
        second_run = get_run(conn, second_run_id)
    assert first_run["status"] == "stopping"
    assert second_run["status"] == "stopping"
    assert first_run["stop_requested_at"]
    assert second_run["stop_requested_at"]


def test_crawl_form_allows_up_to_100_workers(tmp_path, monkeypatch):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    started = []

    def fake_start(run_id, config):
        started.append(config.worker_count)

    monkeypatch.setattr(app.extensions["crawl_jobs"], "start", fake_start)

    response = client.post(
        "/crawl",
        data={
            "start_url": "https://example.org/archive",
            "pagination_template": "",
            "page_start": "1",
            "page_end": "1",
            "max_detail_pages": "100",
            "delay_seconds": "0",
            "timeout_seconds": "20",
            "worker_count": "100",
        },
    )

    assert response.status_code == 302
    assert started == [100]


def test_crawl_form_rejects_more_than_100_workers(tmp_path):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    response = client.post(
        "/crawl",
        data={
            "start_url": "https://example.org/archive",
            "pagination_template": "",
            "page_start": "1",
            "page_end": "1",
            "max_detail_pages": "100",
            "delay_seconds": "0",
            "timeout_seconds": "20",
            "worker_count": "101",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Workers must be between 1 and 100." in response.data


def test_run_detail_page_loads_with_detail_queue(tmp_path):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert b"Discovered detail links" in response.data
    assert b"Run maintenance" in response.data


def test_items_page_displays_catalog_metadata_and_downloads(tmp_path):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))
        detail_url = "https://example.org/detail/one"
        enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive",
            DetailLink(url=detail_url, title="Movie One", publication_year="2024"),
        )
        detail = get_detail_links_for_run(conn, run_id)[0]
        mark_detail_link_completed(
            conn,
            detail["id"],
            synopsis="This synopsis should be visible on the browse page.",
            poster_url="https://example.org/poster.jpg",
            poster_path="poster.jpg",
        )
        record_item(
            conn,
            run_id,
            ScrapedItem(
                source_url="https://example.org/archive",
                detail_url=detail_url,
                link_url="magnet:?xt=urn:btih:abc",
                link_type="magnet",
                name="Movie One",
                publication_year="2024",
                description="Generic details mention 4K availability\n\nLink text: Download 1080p",
            ),
        )
        record_item(
            conn,
            run_id,
            ScrapedItem(
                source_url="https://example.org/archive",
                detail_url=detail_url,
                link_url="https://example.org/movie.torrent",
                link_type="torrent",
                name="Movie One",
                publication_year="2024",
                description="Generic details mention 4K availability\n\nLink text: Download 720p",
            ),
        )
        conn.commit()

    response = client.get("/items?year=2024&sort=title_asc&display=grid")

    assert response.status_code == 200
    assert b"Movie One" in response.data
    assert b"2024" in response.data
    assert b"This synopsis should be visible on the browse page." in response.data
    assert b"/posters/poster.jpg" in response.data
    assert b"catalog-grid" in response.data
    assert b'class="active">Grid' in response.data
    assert b"Open 1080p magnet" in response.data
    assert b"Open 720p torrent" in response.data
    assert b'target="_blank"' in response.data


def test_retry_errors_requeues_failed_work_and_restarts_run(tmp_path, monkeypatch):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()
    started = []

    def fake_start(run_id, config):
        started.append((run_id, config.start_url))

    monkeypatch.setattr(app.extensions["crawl_jobs"], "start", fake_start)

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))
        conn.execute("UPDATE crawl_runs SET status = 'completed_with_errors' WHERE id = ?", (run_id,))
        enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive",
            DetailLink(url="https://example.org/detail/one", title="Failed detail", publication_year="2024"),
        )
        detail = get_detail_links_for_run(conn, run_id)[0]
        mark_detail_link_done(conn, detail["id"], "failed", "Fetch failed")
        conn.commit()

    response = client.post(f"/runs/{run_id}/retry-errors")

    assert response.status_code == 302
    assert started == [(run_id, "https://example.org/archive")]
    with connect(str(database_path)) as conn:
        detail = get_detail_links_for_run(conn, run_id)[0]
    assert detail["status"] == "pending"
    assert detail["error"] == ""


def test_cleanup_marks_pending_work_cancelled(tmp_path):
    database_path = tmp_path / "test.sqlite"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))
        conn.execute("UPDATE crawl_runs SET status = 'stopping' WHERE id = ?", (run_id,))
        enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive",
            DetailLink(url="https://example.org/detail/one", title="Pending detail", publication_year="2024"),
        )
        conn.commit()

    response = client.post(f"/runs/{run_id}/cleanup")

    assert response.status_code == 302
    with connect(str(database_path)) as conn:
        run = get_run(conn, run_id)
        detail = get_detail_links_for_run(conn, run_id)[0]
    assert run["status"] == "cancelled"
    assert detail["status"] == "cancelled"