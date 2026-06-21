from db import (
    cleanup_incomplete_work,
    connect,
    create_run,
    enqueue_detail_link,
    enqueue_index_pages,
    get_detail_links_for_run,
    get_items_for_run,
    get_next_index_page,
    get_run,
    get_run_queue_summary,
    init_db,
    mark_detail_link_completed,
    mark_detail_link_done,
    mark_detail_link_started,
    mark_index_page_done,
    mark_index_page_started,
    record_item,
    retry_failed_work,
    search_catalog_items,
    search_items,
)
from scraper import DetailLink, ScrapeConfig, ScrapedItem


def test_record_item_updates_existing_link_metadata_without_reassigning_run(tmp_path):
    database_path = tmp_path / "test.sqlite"
    init_db(str(database_path))

    with connect(str(database_path)) as conn:
        first_run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))
        second_run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive?page=2"))
        original = ScrapedItem(
            source_url="https://example.org/archive",
            detail_url="https://example.org/detail/one",
            link_url="magnet:?xt=urn:btih:abc",
            link_type="magnet",
            name="Original magnet",
            publication_year="2023",
            description="Original description",
        )
        duplicate = ScrapedItem(
            source_url="https://example.org/archive?page=2",
            detail_url="https://example.org/detail/two",
            link_url="magnet:?xt=urn:btih:abc",
            link_type="magnet",
            name="Updated magnet",
            publication_year="2024",
            description="Duplicate description",
        )

        assert record_item(conn, first_run_id, original) is True
        assert record_item(conn, second_run_id, duplicate) is False
        conn.commit()

        rows, total = search_items(conn)
        first_run_items = get_items_for_run(conn, first_run_id)
        second_run_items = get_items_for_run(conn, second_run_id)

    assert total == 1
    assert rows[0]["run_id"] == first_run_id
    assert rows[0]["name"] == "Updated magnet"
    assert rows[0]["publication_year"] == "2024"
    assert rows[0]["description"] == "Original description"
    assert rows[0]["detail_url"] == "https://example.org/detail/one"
    assert len(first_run_items) == 1
    assert second_run_items == []



def test_enqueue_detail_link_updates_duplicate_without_counting_discovered(tmp_path):
    database_path = tmp_path / "test.sqlite"
    init_db(str(database_path))

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))

        first = enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive?page=1",
            DetailLink(url="https://example.org/detail/one", title="Old title", publication_year="2023"),
        )
        second = enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive?page=2",
            DetailLink(url="https://example.org/detail/one", title="New title", publication_year="2024"),
        )
        rows = get_detail_links_for_run(conn, run_id)

    assert first is True
    assert second is False
    assert len(rows) == 1
    assert rows[0]["source_url"] == "https://example.org/archive?page=2"
    assert rows[0]["title"] == "New title"
    assert rows[0]["publication_year"] == "2024"
def test_search_catalog_items_groups_downloads_with_detail_metadata(tmp_path):
    database_path = tmp_path / "test.sqlite"
    init_db(str(database_path))

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))
        detail_url = "https://example.org/detail/one"
        enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive",
            DetailLink(url=detail_url, title="Browse card title", publication_year="2024"),
        )
        detail = get_detail_links_for_run(conn, run_id)[0]
        mark_detail_link_completed(
            conn,
            detail["id"],
            synopsis="A useful synopsis for filtering.",
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
                name="Browse card title",
                publication_year="2024",
                description="Magnet details",
            ),
        )
        record_item(
            conn,
            run_id,
            ScrapedItem(
                source_url="https://example.org/archive",
                detail_url=detail_url,
                link_url="https://example.org/file.torrent",
                link_type="torrent",
                name="Browse card title",
                publication_year="2024",
                description="Torrent details",
            ),
        )
        conn.commit()

        rows, total = search_catalog_items(conn, query="useful synopsis", year="2024", sort="title_asc")
        missing_rows, missing_total = search_catalog_items(conn, year="1999")

    assert total == 1
    assert rows[0]["name"] == "Browse card title"
    assert rows[0]["publication_year"] == "2024"
    assert rows[0]["synopsis"] == "A useful synopsis for filtering."
    assert rows[0]["poster_path"] == "poster.jpg"
    assert [download["link_type"] for download in rows[0]["downloads"]] == ["magnet", "torrent"]
    assert missing_rows == []
    assert missing_total == 0


def test_retry_and_cleanup_run_queue_work(tmp_path):
    database_path = tmp_path / "test.sqlite"
    init_db(str(database_path))

    with connect(str(database_path)) as conn:
        run_id = create_run(conn, ScrapeConfig(start_url="https://example.org/archive"))
        enqueue_index_pages(conn, run_id, ["https://example.org/archive"])
        index_page = get_next_index_page(conn, run_id)
        mark_index_page_started(conn, index_page["id"])
        mark_index_page_done(conn, index_page["id"], "failed", "Index error")
        enqueue_detail_link(
            conn,
            run_id,
            "https://example.org/archive",
            DetailLink(url="https://example.org/detail/one", title="Errored detail", publication_year="2024"),
        )
        detail = get_detail_links_for_run(conn, run_id)[0]
        mark_detail_link_done(conn, detail["id"], "failed", "Detail error")

        changed = retry_failed_work(conn, run_id)
        summary = get_run_queue_summary(conn, run_id)

        detail = get_detail_links_for_run(conn, run_id)[0]
        index_page = get_next_index_page(conn, run_id)
        mark_detail_link_started(conn, detail["id"])
        mark_index_page_started(conn, index_page["id"])
        cleaned = cleanup_incomplete_work(conn, run_id)
        run = get_run(conn, run_id)
        cleaned_summary = get_run_queue_summary(conn, run_id)

    assert changed == 2
    assert summary["pending_details"] == 1
    assert summary["pending_index_pages"] == 1
    assert cleaned == 2
    assert run["status"] == "cancelled"
    assert cleaned_summary["pending_details"] == 0
    assert cleaned_summary["processing_details"] == 0
    assert cleaned_summary["pending_index_pages"] == 0
    assert cleaned_summary["processing_index_pages"] == 0