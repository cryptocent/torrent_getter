from db import connect, create_run, enqueue_detail_link, get_detail_links_for_run, get_item_downloads, get_items_for_run, get_run, init_db
import jobs
from scraper import DetailLink, DetailResult, FetchResult, ScrapeConfig, ScrapedItem


def test_run_crawl_job_uses_persistent_detail_queue_and_saves_metadata(monkeypatch, tmp_path):
    database_path = tmp_path / "test.sqlite"
    init_db(str(database_path))
    config = ScrapeConfig(start_url="https://example.org/archive", delay_seconds=0, respect_robots=False)

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, config)

    index_html = """
    <div class="browse-content">
      <div class="container">
        <section>
          <div class="browse-movie-wrap">
            <div class="browse-movie-bottom">
              <a href="/detail/one">Queued title</a>
              <div class="browse-movie-year">2026</div>
            </div>
          </div>
        </section>
      </div>
    </div>
    """

    def fake_fetch_url(session, url, config, rate_limiter, robots_cache, robots_lock, should_stop=None):
        assert url == "https://example.org/archive"
        return FetchResult(html=index_html)

    def fake_crawl_detail_page(task, config, rate_limiter, robots_cache, robots_lock, should_stop=None):
        assert task.title == "Queued title"
        assert task.publication_year == "2026"
        return DetailResult(
            detail_url=task.detail_url,
            source_url=task.source_url,
            items=(
                ScrapedItem(
                    source_url=task.source_url,
                    detail_url=task.detail_url,
                    link_url="magnet:?xt=urn:btih:abc",
                    link_type="magnet",
                    name=task.title,
                    publication_year=task.publication_year,
                ),
                ScrapedItem(
                    source_url=task.source_url,
                    detail_url=task.detail_url,
                    link_url="https://example.org/files/one.torrent",
                    link_type="torrent",
                    name=task.title,
                    publication_year=task.publication_year,
                ),
            ),
            fetched=True,
            synopsis="Saved synopsis",
            poster_url="https://example.org/poster.jpg",
        )

    monkeypatch.setattr(jobs, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(jobs, "crawl_detail_page", fake_crawl_detail_page)
    monkeypatch.setattr(jobs, "_download_poster", lambda poster_url, detail_id, poster_dir, config: "poster.jpg")

    jobs.run_crawl_job(str(database_path), run_id, config)

    with connect(str(database_path)) as conn:
        run = get_run(conn, run_id)
        detail_links = get_detail_links_for_run(conn, run_id)
        items = get_items_for_run(conn, run_id)
        downloads = get_item_downloads(conn, items[0]["id"])

    assert run["status"] == "completed"
    assert run["detail_pages_found"] == 1
    assert run["detail_pages_scanned"] == 1
    assert run["items_found"] == 2
    assert detail_links[0]["status"] == "completed"
    assert detail_links[0]["synopsis"] == "Saved synopsis"
    assert detail_links[0]["poster_path"] == "poster.jpg"
    assert {item["link_type"] for item in items} == {"magnet", "torrent"}
    assert [download["link_type"] for download in downloads] == ["magnet", "torrent"]

def test_process_detail_links_only_claims_worker_sized_batches(monkeypatch, tmp_path):
    database_path = tmp_path / "test.sqlite"
    init_db(str(database_path))
    config = ScrapeConfig(start_url="https://example.org/archive", delay_seconds=0, worker_count=2, respect_robots=False)

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, config)
        for index in range(3):
            enqueue_detail_link(
                conn,
                run_id,
                "https://example.org/archive",
                DetailLink(url=f"https://example.org/detail/{index}", title=f"Title {index}"),
            )
        conn.commit()

    seen_processing_counts = []

    def fake_fetch_detail_link(detail, config, rate_limiter, robots_cache, robots_lock, poster_dir, should_stop):
        with connect(str(database_path)) as check_conn:
            processing_count = check_conn.execute(
                "SELECT COUNT(*) FROM detail_links WHERE run_id = ? AND status = 'processing'",
                (run_id,),
            ).fetchone()[0]
        seen_processing_counts.append(processing_count)
        assert processing_count <= 2
        return (
            DetailResult(
                detail_url=detail["detail_url"],
                source_url=detail["source_url"],
                fetched=True,
            ),
            "",
        )

    monkeypatch.setattr(jobs, "_fetch_detail_link", fake_fetch_detail_link)

    with connect(str(database_path)) as conn:
        jobs._process_detail_links(
            conn,
            run_id,
            config,
            jobs.CrawlStats(),
            tmp_path / "posters",
            lambda: False,
        )
        rows = get_detail_links_for_run(conn, run_id)

    assert seen_processing_counts
    assert all(count <= 2 for count in seen_processing_counts)
    assert {row["status"] for row in rows} == {"completed"}