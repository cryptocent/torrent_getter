from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote_plus, urlparse

from scraper import CrawlStats, DetailLink, ScrapeConfig, ScrapedItem

RESOLUTION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(4320p|2160p|1440p|1080p|1080i|720p|576p|540p|480p|360p|240p|8k|4k|uhd)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT,
    stop_requested_at TEXT,
    start_url TEXT NOT NULL,
    pagination_template TEXT,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    max_detail_pages INTEGER NOT NULL,
    delay_seconds REAL NOT NULL,
    worker_count INTEGER NOT NULL DEFAULT 10,
    respect_robots INTEGER NOT NULL,
    status TEXT NOT NULL,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    detail_pages_found INTEGER NOT NULL DEFAULT 0,
    detail_pages_scanned INTEGER NOT NULL DEFAULT 0,
    items_found INTEGER NOT NULL DEFAULT 0,
    skipped_by_robots INTEGER NOT NULL DEFAULT 0,
    last_index_url TEXT NOT NULL DEFAULT '',
    last_detail_url TEXT NOT NULL DEFAULT '',
    errors TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    link_url TEXT NOT NULL UNIQUE,
    link_id TEXT NOT NULL DEFAULT '',
    link_type TEXT NOT NULL,
    name TEXT NOT NULL,
    publication_year TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    detail_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
);
CREATE TABLE IF NOT EXISTS crawl_index_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    page_url TEXT NOT NULL,
    page_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, page_url),
    FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
);

CREATE TABLE IF NOT EXISTS detail_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    detail_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    publication_year TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    synopsis TEXT NOT NULL DEFAULT '',
    poster_url TEXT NOT NULL DEFAULT '',
    poster_path TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, detail_url),
    FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(link_type);
CREATE INDEX IF NOT EXISTS idx_items_last_seen ON items(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_items_detail_url_last_seen ON items(detail_url, last_seen_at, id);
CREATE INDEX IF NOT EXISTS idx_crawl_index_pages_run_status ON crawl_index_pages(run_id, status, page_order);
CREATE INDEX IF NOT EXISTS idx_detail_links_run_status ON detail_links(run_id, status, id);
CREATE INDEX IF NOT EXISTS idx_detail_links_detail_url ON detail_links(detail_url);
CREATE INDEX IF NOT EXISTS idx_detail_links_detail_url_updated ON detail_links(detail_url, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_detail_links_publication_year ON detail_links(publication_year);
"""

RUN_COLUMN_MIGRATIONS = {
    "updated_at": "TEXT NOT NULL DEFAULT ''",
    "finished_at": "TEXT",
    "stop_requested_at": "TEXT",
    "worker_count": "INTEGER NOT NULL DEFAULT 10",
    "last_index_url": "TEXT NOT NULL DEFAULT ''",
    "last_detail_url": "TEXT NOT NULL DEFAULT ''",
}

ITEM_COLUMN_MIGRATIONS = {
    "publication_year": "TEXT NOT NULL DEFAULT ''",
    "link_id": "TEXT NOT NULL DEFAULT ''",
}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(database_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(database_path, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(database_path: str) -> None:
    with connect(database_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn, "crawl_runs", RUN_COLUMN_MIGRATIONS)
        _ensure_columns(conn, "items", ITEM_COLUMN_MIGRATIONS)
        _backfill_item_link_ids(conn)
        _delete_duplicate_items(conn)
        _ensure_item_indexes(conn)


def create_run(conn: sqlite3.Connection, config: ScrapeConfig) -> int:
    now = _utc_now()
    cursor = conn.execute(
        """
        INSERT INTO crawl_runs (
            created_at, updated_at, start_url, pagination_template, page_start, page_end,
            max_detail_pages, delay_seconds, worker_count, respect_robots, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            now,
            config.start_url,
            config.pagination_template,
            config.page_start,
            config.page_end,
            config.max_detail_pages,
            config.delay_seconds,
            config.worker_count,
            1 if config.respect_robots else 0,
            "queued",
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def mark_run_started(conn: sqlite3.Connection, run_id: int) -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            updated_at = ?,
            finished_at = NULL,
            stop_requested_at = NULL
        WHERE id = ?
        """,
        ("running", now, run_id),
    )


def update_run_progress(conn: sqlite3.Connection, run_id: int, stats: CrawlStats, errors: str = "") -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_runs
        SET updated_at = ?,
            pages_scanned = ?,
            detail_pages_found = ?,
            detail_pages_scanned = ?,
            items_found = ?,
            skipped_by_robots = ?,
            last_index_url = ?,
            last_detail_url = ?,
            errors = ?
        WHERE id = ?
        """,
        (
            now,
            stats.pages_scanned,
            stats.detail_pages_found,
            stats.detail_pages_scanned,
            stats.items_found,
            stats.skipped_by_robots,
            stats.last_index_url,
            stats.last_detail_url,
            errors,
            run_id,
        ),
    )


def request_run_stop(conn: sqlite3.Connection, run_id: int) -> bool:
    now = _utc_now()
    cursor = conn.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            updated_at = ?,
            stop_requested_at = ?
        WHERE id = ?
          AND status IN ('queued', 'running', 'stopping', 'completed', 'completed_with_errors')
        """,
        ("stopping", now, now, run_id),
    )
    return cursor.rowcount > 0


def request_all_run_stops(conn: sqlite3.Connection) -> int:
    now = _utc_now()
    cursor = conn.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            updated_at = ?,
            stop_requested_at = ?
        WHERE status IN ('queued', 'running', 'stopping')
        """,
        ("stopping", now, now),
    )
    return cursor.rowcount


def is_run_stop_requested(conn: sqlite3.Connection, run_id: int) -> bool:
    row = conn.execute(
        """
        SELECT stop_requested_at, status
        FROM crawl_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return True
    return bool(row["stop_requested_at"]) or row["status"] == "stopping"


def mark_run_cancelled(conn: sqlite3.Connection, run_id: int) -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            updated_at = ?,
            finished_at = COALESCE(finished_at, ?)
        WHERE id = ?
          AND status IN ('queued', 'running', 'stopping')
        """,
        ("cancelled", now, now, run_id),
    )


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats: CrawlStats, errors: str = "") -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            updated_at = ?,
            finished_at = ?,
            pages_scanned = ?,
            detail_pages_found = ?,
            detail_pages_scanned = ?,
            items_found = ?,
            skipped_by_robots = ?,
            last_index_url = ?,
            last_detail_url = ?,
            errors = ?
        WHERE id = ?
        """,
        (
            status,
            now,
            now,
            stats.pages_scanned,
            stats.detail_pages_found,
            stats.detail_pages_scanned,
            stats.items_found,
            stats.skipped_by_robots,
            stats.last_index_url,
            stats.last_detail_url,
            errors,
            run_id,
        ),
    )

def enqueue_index_pages(conn: sqlite3.Connection, run_id: int, page_urls: list[str]) -> None:
    now = _utc_now()
    for page_order, page_url in enumerate(page_urls):
        conn.execute(
            """
            INSERT INTO crawl_index_pages (run_id, page_url, page_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, page_url) DO UPDATE SET
                page_order = excluded.page_order,
                updated_at = excluded.updated_at
            """,
            (run_id, page_url, page_order, now, now),
        )


def reset_processing_work(conn: sqlite3.Connection, run_id: int) -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_index_pages
        SET status = 'pending',
            error = '',
            updated_at = ?,
            started_at = NULL
        WHERE run_id = ?
          AND status IN ('processing', 'failed')
        """,
        (now, run_id),
    )
    conn.execute(
        """
        UPDATE detail_links
        SET status = 'pending',
            error = '',
            updated_at = ?,
            started_at = NULL
        WHERE run_id = ?
          AND status IN ('processing', 'failed')
        """,
        (now, run_id),
    )


def get_next_index_page(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM crawl_index_pages
        WHERE run_id = ?
          AND status = 'pending'
        ORDER BY page_order, id
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()


def mark_index_page_started(conn: sqlite3.Connection, page_id: int) -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_index_pages
        SET status = 'processing',
            updated_at = ?,
            started_at = ?
        WHERE id = ?
        """,
        (now, now, page_id),
    )


def mark_index_page_done(conn: sqlite3.Connection, page_id: int, status: str, error: str = "") -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE crawl_index_pages
        SET status = ?,
            error = ?,
            updated_at = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (status, error, now, now, page_id),
    )


def enqueue_detail_link(conn: sqlite3.Connection, run_id: int, source_url: str, detail_link: DetailLink) -> bool:
    now = _utc_now()
    existing = conn.execute(
        """
        SELECT id
        FROM detail_links
        WHERE run_id = ?
          AND detail_url = ?
        """,
        (run_id, detail_link.url),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO detail_links (
            run_id, source_url, detail_url, title, publication_year, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, detail_url) DO UPDATE SET
            source_url = excluded.source_url,
            title = excluded.title,
            publication_year = excluded.publication_year,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            source_url,
            detail_link.url,
            detail_link.title,
            detail_link.publication_year,
            now,
            now,
        ),
    )
    return existing is None


def count_detail_links(conn: sqlite3.Connection, run_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM detail_links WHERE run_id = ?", (run_id,)).fetchone()[0])


def get_pending_detail_links(conn: sqlite3.Connection, run_id: int, limit: int | None = None) -> list[sqlite3.Row]:
    limit_sql = "" if limit is None else "LIMIT ?"
    params: list[int] = [run_id]
    if limit is not None:
        params.append(limit)
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM detail_links
            WHERE run_id = ?
              AND status = 'pending'
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
    )


def mark_detail_link_started(conn: sqlite3.Connection, detail_link_id: int) -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE detail_links
        SET status = 'processing',
            updated_at = ?,
            started_at = ?
        WHERE id = ?
        """,
        (now, now, detail_link_id),
    )


def mark_detail_link_completed(
    conn: sqlite3.Connection,
    detail_link_id: int,
    synopsis: str = "",
    poster_url: str = "",
    poster_path: str = "",
) -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE detail_links
        SET status = 'completed',
            synopsis = ?,
            poster_url = ?,
            poster_path = ?,
            error = '',
            updated_at = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (synopsis, poster_url, poster_path, now, now, detail_link_id),
    )


def mark_detail_link_done(conn: sqlite3.Connection, detail_link_id: int, status: str, error: str = "") -> None:
    now = _utc_now()
    conn.execute(
        """
        UPDATE detail_links
        SET status = ?,
            error = ?,
            updated_at = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (status, error, now, now, detail_link_id),
    )


def refresh_stats_from_queues(conn: sqlite3.Connection, run_id: int, stats: CrawlStats) -> CrawlStats:
    stats.pages_scanned = int(
        conn.execute(
            "SELECT COUNT(*) FROM crawl_index_pages WHERE run_id = ? AND status = 'completed'",
            (run_id,),
        ).fetchone()[0]
    )
    stats.detail_pages_found = int(
        conn.execute("SELECT COUNT(*) FROM detail_links WHERE run_id = ?", (run_id,)).fetchone()[0]
    )
    stats.detail_pages_scanned = int(
        conn.execute(
            "SELECT COUNT(*) FROM detail_links WHERE run_id = ? AND status = 'completed'",
            (run_id,),
        ).fetchone()[0]
    )
    stats.skipped_by_robots = int(
        conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM crawl_index_pages WHERE run_id = ? AND status = 'skipped_by_robots') +
                (SELECT COUNT(*) FROM detail_links WHERE run_id = ? AND status = 'skipped_by_robots')
            """,
            (run_id, run_id),
        ).fetchone()[0]
    )
    return stats


def get_detail_links_for_run(conn: sqlite3.Connection, run_id: int, limit: int = 100) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM detail_links
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
    )
def record_item(conn: sqlite3.Connection, run_id: int, item: ScrapedItem) -> bool:
    now = _utc_now()
    link_id = link_id_for_url(item.link_url)
    existing = conn.execute(
        """
        SELECT id
        FROM items
        WHERE link_id = ?
        ORDER BY id
        LIMIT 1
        """,
        (link_id,),
    ).fetchone()
    if existing is None:
        existing = conn.execute(
            """
            SELECT id
            FROM items
            WHERE link_url = ?
            """,
            (item.link_url,),
        ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE items
            SET name = ?,
                publication_year = ?,
                link_id = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (item.name, item.publication_year, link_id, now, existing["id"]),
        )
        return False

    conn.execute(
        """
        INSERT INTO items (
            run_id, link_url, link_id, link_type, name, publication_year, description,
            source_url, detail_url, created_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            item.link_url,
            link_id,
            item.link_type,
            item.name,
            item.publication_year,
            item.description,
            item.source_url,
            item.detail_url,
            now,
            now,
        ),
    )
    return True


def get_downloads_for_detail_url(conn: sqlite3.Connection, detail_url: str) -> list[dict]:
    return _label_download_rows(
        conn.execute(
            """
            SELECT id, link_url, link_id, link_type, name, description
            FROM items
            WHERE detail_url = ?
            ORDER BY CASE link_type WHEN 'magnet' THEN 0 WHEN 'torrent' THEN 1 ELSE 2 END, id
            """,
            (detail_url,),
        ).fetchall()
    )


def search_catalog_items(
    conn: sqlite3.Connection,
    query: str = "",
    year: str = "",
    link_type: str = "",
    sort: str = "last_seen_desc",
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list[str | int] = []

    if query:
        like = f"%{query}%"
        where.append(
            "("
            "i.name LIKE ? OR i.publication_year LIKE ? OR i.description LIKE ? OR "
            "i.link_url LIKE ? OR i.source_url LIKE ? OR i.detail_url LIKE ? OR "
            "i.detail_url IN ("
            "SELECT dq.detail_url FROM detail_links AS dq "
            "WHERE dq.title LIKE ? OR dq.synopsis LIKE ?"
            ")"
            ")"
        )
        params.extend([like, like, like, like, like, like, like, like])

    if year:
        where.append(
            "("
            "i.publication_year = ? OR "
            "i.detail_url IN ("
            "SELECT dy.detail_url FROM detail_links AS dy "
            "WHERE dy.publication_year = ?"
            ")"
            ")"
        )
        params.extend([year, year])

    if link_type in {"magnet", "torrent"}:
        where.append("i.link_type = ?")
        params.append(link_type)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT i.detail_url
                FROM items AS i
                {where_sql}
                GROUP BY i.detail_url
            ) AS grouped_items
            """,
            params,
        ).fetchone()[0]
    )

    order_by = {
        "last_seen_desc": "last_seen_at DESC, max_id DESC",
        "last_seen_asc": "last_seen_at ASC, max_id ASC",
        "title_asc": "sort_title ASC, max_id ASC",
        "title_desc": "sort_title DESC, max_id DESC",
        "year_desc": "sort_year IS NULL ASC, sort_year DESC, sort_year_text DESC, max_id DESC",
        "year_asc": "sort_year IS NULL ASC, sort_year ASC, sort_year_text ASC, max_id ASC",
    }.get(sort, "last_seen_at DESC, max_id DESC")

    page_rows = conn.execute(
        f"""
        SELECT i.detail_url,
               MAX(i.last_seen_at) AS last_seen_at,
               MAX(i.id) AS max_id,
               LOWER(COALESCE(MIN(NULLIF(i.name, '')), 'untitled')) AS sort_title,
               MAX(CAST(NULLIF(i.publication_year, '') AS INTEGER)) AS sort_year,
               MAX(NULLIF(i.publication_year, '')) AS sort_year_text
        FROM items AS i
        {where_sql}
        GROUP BY i.detail_url
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    detail_urls = [row["detail_url"] for row in page_rows]
    if not detail_urls:
        return [], total

    placeholders = ", ".join("?" for _ in detail_urls)
    latest_items: dict[str, dict] = {}
    for row in conn.execute(
        f"""
        SELECT *
        FROM items
        WHERE detail_url IN ({placeholders})
        ORDER BY detail_url, last_seen_at DESC, id DESC
        """,
        detail_urls,
    ).fetchall():
        latest_items.setdefault(row["detail_url"], dict(row))

    latest_details: dict[str, dict] = {}
    for row in conn.execute(
        f"""
        SELECT *
        FROM detail_links
        WHERE detail_url IN ({placeholders})
        ORDER BY detail_url, updated_at DESC, id DESC
        """,
        detail_urls,
    ).fetchall():
        latest_details.setdefault(row["detail_url"], dict(row))

    download_rows_by_detail_url: dict[str, list[sqlite3.Row]] = {detail_url: [] for detail_url in detail_urls}
    for row in conn.execute(
        f"""
        SELECT detail_url, id, link_url, link_id, link_type, name, description
        FROM items
        WHERE detail_url IN ({placeholders})
        ORDER BY detail_url, CASE link_type WHEN 'magnet' THEN 0 WHEN 'torrent' THEN 1 ELSE 2 END, id
        """,
        detail_urls,
    ).fetchall():
        download_rows_by_detail_url.setdefault(row["detail_url"], []).append(row)

    downloads_by_detail_url: dict[str, list[dict]] = {detail_url: [] for detail_url in detail_urls}
    for detail_url, download_rows in download_rows_by_detail_url.items():
        for download in _label_download_rows(download_rows):
            download.pop("detail_url", None)
            downloads_by_detail_url.setdefault(detail_url, []).append(download)

    catalog_items: list[dict] = []
    for page_row in page_rows:
        detail_url = page_row["detail_url"]
        latest = latest_items[detail_url]
        latest_detail = latest_details.get(detail_url, {})
        catalog_items.append(
            {
                "item_id": latest["id"],
                "run_id": latest["run_id"],
                "detail_url": detail_url,
                "source_url": latest["source_url"],
                "name": latest["name"] or latest_detail.get("title") or "Untitled",
                "publication_year": latest["publication_year"] or latest_detail.get("publication_year") or "",
                "description": latest["description"],
                "last_seen_at": latest["last_seen_at"],
                "synopsis": latest_detail.get("synopsis", ""),
                "poster_url": latest_detail.get("poster_url", ""),
                "poster_path": latest_detail.get("poster_path", ""),
                "downloads": downloads_by_detail_url.get(detail_url, []),
            }
        )
    return catalog_items, total

def get_publication_years(conn: sqlite3.Connection, limit: int = 150) -> list[str]:
    return [
        row["publication_year"]
        for row in conn.execute(
            """
            SELECT publication_year
            FROM (
                SELECT DISTINCT publication_year
                FROM items
                WHERE publication_year != ''
            )
            ORDER BY CAST(publication_year AS INTEGER) DESC, publication_year DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def get_run_queue_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    summary = {
        "failed_details": 0,
        "pending_details": 0,
        "processing_details": 0,
        "failed_index_pages": 0,
        "pending_index_pages": 0,
        "processing_index_pages": 0,
    }
    for row in conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM detail_links
        WHERE run_id = ?
        GROUP BY status
        """,
        (run_id,),
    ).fetchall():
        key = f"{row['status']}_details"
        if key in summary:
            summary[key] = int(row["count"])
    for row in conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM crawl_index_pages
        WHERE run_id = ?
        GROUP BY status
        """,
        (run_id,),
    ).fetchall():
        key = f"{row['status']}_index_pages"
        if key in summary:
            summary[key] = int(row["count"])
    return summary


def retry_failed_work(conn: sqlite3.Connection, run_id: int) -> int:
    now = _utc_now()
    detail_cursor = conn.execute(
        """
        UPDATE detail_links
        SET status = 'pending',
            error = '',
            updated_at = ?,
            started_at = NULL,
            finished_at = NULL
        WHERE run_id = ?
          AND status = 'failed'
        """,
        (now, run_id),
    )
    index_cursor = conn.execute(
        """
        UPDATE crawl_index_pages
        SET status = 'pending',
            error = '',
            updated_at = ?,
            started_at = NULL,
            finished_at = NULL
        WHERE run_id = ?
          AND status = 'failed'
        """,
        (now, run_id),
    )
    return detail_cursor.rowcount + index_cursor.rowcount


def cleanup_incomplete_work(conn: sqlite3.Connection, run_id: int) -> int:
    now = _utc_now()
    detail_cursor = conn.execute(
        """
        UPDATE detail_links
        SET status = 'cancelled',
            updated_at = ?,
            finished_at = COALESCE(finished_at, ?)
        WHERE run_id = ?
          AND status IN ('pending', 'processing')
        """,
        (now, now, run_id),
    )
    index_cursor = conn.execute(
        """
        UPDATE crawl_index_pages
        SET status = 'cancelled',
            updated_at = ?,
            finished_at = COALESCE(finished_at, ?)
        WHERE run_id = ?
          AND status IN ('pending', 'processing')
        """,
        (now, now, run_id),
    )
    changed = detail_cursor.rowcount + index_cursor.rowcount
    if changed:
        conn.execute(
            """
            UPDATE crawl_runs
            SET status = 'cancelled',
                updated_at = ?,
                finished_at = COALESCE(finished_at, ?),
                stop_requested_at = COALESCE(stop_requested_at, ?)
            WHERE id = ?
            """,
            (now, now, now, run_id),
        )
    return changed


def search_items(
    conn: sqlite3.Connection,
    query: str = "",
    link_type: str = "",
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    where: list[str] = []
    params: list[str | int] = []

    if query:
        like = f"%{query}%"
        where.append("(name LIKE ? OR publication_year LIKE ? OR description LIKE ? OR link_url LIKE ? OR source_url LIKE ? OR detail_url LIKE ?)")
        params.extend([like, like, like, like, like, like])

    if link_type in {"magnet", "torrent"}:
        where.append("link_type = ?")
        params.append(link_type)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM items {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT *
        FROM items
        {where_sql}
        ORDER BY last_seen_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return list(rows), int(total)


def get_recent_items(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM items
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def get_recent_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM crawl_runs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()


def get_latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM crawl_runs
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def get_items_for_run(conn: sqlite3.Connection, run_id: int, limit: int = 100) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM items
            WHERE run_id = ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
    )


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT i.*,
               d.synopsis,
               d.poster_url,
               d.poster_path
        FROM items AS i
        LEFT JOIN detail_links AS d
          ON d.id = (
              SELECT latest.id
              FROM detail_links AS latest
              WHERE latest.detail_url = i.detail_url
              ORDER BY latest.updated_at DESC, latest.id DESC
              LIMIT 1
          )
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()


def get_item_downloads(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    item = conn.execute("SELECT detail_url FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return []
    return get_downloads_for_detail_url(conn, item["detail_url"])


def _label_download_rows(rows) -> list[dict]:
    downloads: list[dict] = []
    seen_link_ids: set[str] = set()
    for row in rows:
        download = dict(row)
        link_id = download.get("link_id") or link_id_for_url(download.get("link_url", ""))
        if link_id in seen_link_ids:
            continue
        seen_link_ids.add(link_id)
        resolution = _download_resolution(
            download.get("description", ""),
            download.get("link_url", ""),
            download.get("name", ""),
        )
        download["label"] = f"{resolution} {download['link_type']}" if resolution else download["link_type"]
        download.pop("name", None)
        download.pop("description", None)
        downloads.append(download)
    return downloads


def get_info_hash(magnet_link: str) -> str | None:
    parsed = urlparse(magnet_link)
    params = parse_qs(parsed.query)
    xt_values = params.get("xt")
    if not xt_values:
        return None

    xt = xt_values[0]
    prefix = "urn:btih:"
    if xt.lower().startswith(prefix):
        return xt[len(prefix):].upper()
    return xt.upper()


def link_id_for_url(link_url: str) -> str:
    if _is_torrent_url(link_url):
        return link_url
    return get_info_hash(link_url) or link_url


def _is_torrent_url(link_url: str) -> bool:
    parsed = urlparse(link_url)
    path = parsed.path or link_url
    return path.lower().endswith(".torrent")


def _ensure_item_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_items_link_id")
    conn.execute("CREATE UNIQUE INDEX idx_items_link_id ON items(link_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_publication_year ON items(publication_year)")

def _backfill_item_link_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, link_url
        FROM items
        WHERE link_id = ''
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE items
            SET link_id = ?
            WHERE id = ?
            """,
            (link_id_for_url(row["link_url"]), row["id"]),
        )

def _delete_duplicate_items(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM items
        WHERE link_id != ''
          AND id NOT IN (
              SELECT MIN(id)
              FROM items
              WHERE link_id != ''
              GROUP BY link_id
          )
        """
    )

def _download_resolution(description: str, link_url: str, name: str) -> str:
    link_text = _extract_link_text(description)
    sources = []
    if link_text:
        sources.append(link_text)
    elif description and "\n" not in description and len(description) <= 160:
        sources.append(description)
    sources.extend([unquote_plus(link_url), name])
    return _extract_resolution(*sources)


def _extract_link_text(description: str) -> str:
    marker = "Link text:"
    if marker not in description:
        return ""
    return description.rsplit(marker, 1)[1].strip().splitlines()[0].strip()


def _extract_resolution(*values: str) -> str:
    for value in values:
        if not value:
            continue
        match = RESOLUTION_PATTERN.search(value)
        if match:
            token = match.group(1).lower()
            if token in {"4k", "8k", "uhd"}:
                return token.upper()
            return token
    return ""

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
