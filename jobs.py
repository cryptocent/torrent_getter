from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import requests

from db import (
    connect,
    count_detail_links,
    enqueue_detail_link,
    enqueue_index_pages,
    finish_run,
    get_next_index_page,
    get_pending_detail_links,
    get_run,
    is_run_stop_requested,
    mark_detail_link_completed,
    mark_detail_link_done,
    mark_detail_link_started,
    mark_index_page_done,
    mark_index_page_started,
    mark_run_started,
    record_item,
    refresh_stats_from_queues,
    reset_processing_work,
    update_run_progress,
)
from scraper import (
    CrawlStats,
    DetailResult,
    DetailTask,
    RateLimiter,
    ScrapeConfig,
    build_index_urls,
    crawl_detail_page,
    fetch_url,
    find_detail_links,
)


class CrawlJobManager:
    def __init__(self, database_path: str):
        self.database_path = database_path
        self._active: dict[int, subprocess.Popen] = {}

    def start(self, run_id: int, config: ScrapeConfig) -> None:
        self._remove_finished_processes()
        if run_id in self._active:
            return

        worker_script = Path(__file__).with_name("crawl_worker.py")
        command = [
            sys.executable,
            str(worker_script),
            "--database",
            self.database_path,
            "--run-id",
            str(run_id),
            "--config",
            json.dumps(asdict(config)),
        ]
        kwargs = {
            "cwd": str(Path(__file__).resolve().parent),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._active[run_id] = subprocess.Popen(command, **kwargs)

    def stop(self, run_id: int) -> bool:
        self._remove_finished_processes()
        process = self._active.get(run_id)
        if process is None or process.poll() is not None:
            self._active.pop(run_id, None)
            return False
        self._terminate_process(process)
        self._active.pop(run_id, None)
        return True

    def stop_all(self) -> list[int]:
        self._remove_finished_processes()
        stopped: list[int] = []
        for run_id in list(self._active):
            if self.stop(run_id):
                stopped.append(run_id)
        return stopped

    def is_running(self, run_id: int) -> bool:
        self._remove_finished_processes()
        process = self._active.get(run_id)
        return bool(process and process.poll() is None)

    def _remove_finished_processes(self) -> None:
        for run_id, process in list(self._active.items()):
            if process.poll() is not None:
                self._active.pop(run_id, None)

    def _terminate_process(self, process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def run_crawl_job(database_path: str, run_id: int, config: ScrapeConfig) -> None:
    conn = connect(database_path)
    stats = _stats_from_run(conn, run_id)
    poster_dir = Path(database_path).resolve().parent / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)

    try:
        mark_run_started(conn, run_id)
        reset_processing_work(conn, run_id)
        enqueue_index_pages(
            conn,
            run_id,
            build_index_urls(config.start_url, config.pagination_template, config.page_start, config.page_end),
        )
        _save_progress(conn, run_id, stats)
        conn.commit()

        def should_stop() -> bool:
            with connect(database_path) as stop_conn:
                return is_run_stop_requested(stop_conn, run_id)

        if should_stop():
            stats.cancelled = True
            finish_run(conn, run_id, "cancelled", refresh_stats_from_queues(conn, run_id, stats), _format_errors(stats))
            conn.commit()
            return

        _discover_detail_links(conn, run_id, config, stats, should_stop)
        if not stats.cancelled:
            _process_detail_links(conn, run_id, config, stats, poster_dir, should_stop)

        refresh_stats_from_queues(conn, run_id, stats)
        if stats.cancelled or should_stop():
            status = "cancelled"
        elif stats.errors:
            status = "completed_with_errors"
        else:
            status = "completed"
        finish_run(conn, run_id, status, stats, _format_errors(stats))
        conn.commit()
    except Exception as exc:
        stats.errors.append(str(exc))
        finish_run(conn, run_id, "failed", refresh_stats_from_queues(conn, run_id, stats), _format_errors(stats))
        conn.commit()
    finally:
        conn.close()


def _discover_detail_links(conn, run_id: int, config: ScrapeConfig, stats: CrawlStats, should_stop) -> None:
    index_session = requests.Session()
    index_session.headers.update({"User-Agent": config.user_agent})
    robots_cache = {}
    robots_lock = threading.Lock()
    rate_limiter = RateLimiter(config.delay_seconds)

    while not should_stop():
        if config.max_detail_pages and count_detail_links(conn, run_id) >= config.max_detail_pages:
            _mark_pending_index_pages_skipped_by_limit(conn, run_id)
            _save_progress(conn, run_id, stats)
            conn.commit()
            return

        index_page = get_next_index_page(conn, run_id)
        if index_page is None:
            return

        mark_index_page_started(conn, index_page["id"])
        conn.commit()

        result = fetch_url(
            index_session,
            index_page["page_url"],
            config,
            rate_limiter,
            robots_cache,
            robots_lock,
            should_stop,
        )
        if result.cancelled:
            stats.cancelled = True
            return
        if result.skipped_by_robots:
            mark_index_page_done(conn, index_page["id"], "skipped_by_robots")
        elif result.error:
            stats.errors.append(result.error)
            mark_index_page_done(conn, index_page["id"], "failed", result.error)
        elif result.html:
            for detail_link in find_detail_links(result.html, index_page["page_url"]):
                if config.max_detail_pages and count_detail_links(conn, run_id) >= config.max_detail_pages:
                    break
                enqueue_detail_link(conn, run_id, index_page["page_url"], detail_link)
            stats.last_index_url = index_page["page_url"]
            mark_index_page_done(conn, index_page["id"], "completed")

        _save_progress(conn, run_id, stats)
        conn.commit()

    stats.cancelled = True


def _process_detail_links(conn, run_id: int, config: ScrapeConfig, stats: CrawlStats, poster_dir: Path, should_stop) -> None:
    rate_limiter = RateLimiter(config.delay_seconds)
    robots_cache = {}
    robots_lock = threading.Lock()
    worker_count = max(1, min(config.worker_count, 100))

    while not should_stop():
        pending_details = get_pending_detail_links(conn, run_id, limit=worker_count)
        if not pending_details:
            return

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for detail in pending_details:
                if should_stop():
                    stats.cancelled = True
                    break
                mark_detail_link_started(conn, detail["id"])
                futures[
                    executor.submit(
                        _fetch_detail_link,
                        dict(detail),
                        config,
                        rate_limiter,
                        robots_cache,
                        robots_lock,
                        poster_dir,
                        should_stop,
                    )
                ] = dict(detail)
            conn.commit()

            for future in as_completed(futures):
                if should_stop():
                    stats.cancelled = True
                    for pending in futures:
                        pending.cancel()
                    return

                detail = futures[future]
                try:
                    result, poster_path = future.result()
                except Exception as exc:
                    error = f"{detail['detail_url']}: {exc}"
                    stats.errors.append(error)
                    mark_detail_link_done(conn, detail["id"], "failed", error)
                    _save_progress(conn, run_id, stats)
                    conn.commit()
                    continue

                if result.skipped_by_robots:
                    mark_detail_link_done(conn, detail["id"], "skipped_by_robots")
                elif result.error:
                    stats.errors.append(result.error)
                    mark_detail_link_done(conn, detail["id"], "failed", result.error)
                elif result.fetched:
                    stats.last_detail_url = result.detail_url
                    mark_detail_link_completed(
                        conn,
                        detail["id"],
                        synopsis=result.synopsis,
                        poster_url=result.poster_url,
                        poster_path=poster_path,
                    )
                    for item in result.items:
                        if record_item(conn, run_id, item):
                            stats.items_found += 1
                else:
                    mark_detail_link_done(conn, detail["id"], "failed", "No detail page content was returned.")

                _save_progress(conn, run_id, stats)
                conn.commit()

    stats.cancelled = True


def _fetch_detail_link(
    detail: dict,
    config: ScrapeConfig,
    rate_limiter: RateLimiter,
    robots_cache: dict,
    robots_lock: threading.Lock,
    poster_dir: Path,
    should_stop,
) -> tuple[DetailResult, str]:
    task = DetailTask(
        detail_url=detail["detail_url"],
        source_url=detail["source_url"],
        title=detail["title"],
        publication_year=detail["publication_year"],
    )
    result = crawl_detail_page(task, config, rate_limiter, robots_cache, robots_lock, should_stop)
    poster_path = ""
    if result.fetched and result.poster_url and not should_stop():
        poster_path = _download_poster(result.poster_url, int(detail["id"]), poster_dir, config)
    return result, poster_path


def _download_poster(poster_url: str, detail_id: int, poster_dir: Path, config: ScrapeConfig) -> str:
    try:
        response = requests.get(poster_url, headers={"User-Agent": config.user_agent}, timeout=config.timeout_seconds)
        response.raise_for_status()
    except requests.RequestException:
        return ""

    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type and not content_type.startswith("image/"):
        return ""

    suffix = Path(urlsplit(poster_url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        suffix = ".jpg"
    digest = hashlib.sha256(poster_url.encode("utf-8")).hexdigest()[:12]
    filename = f"detail-{detail_id}-{digest}{suffix}"
    target = poster_dir / filename
    target.write_bytes(response.content)
    return filename


def _mark_pending_index_pages_skipped_by_limit(conn, run_id: int) -> None:
    conn.execute(
        """
        UPDATE crawl_index_pages
        SET status = 'skipped_by_limit',
            updated_at = CURRENT_TIMESTAMP
        WHERE run_id = ?
          AND status = 'pending'
        """,
        (run_id,),
    )


def _save_progress(conn, run_id: int, stats: CrawlStats) -> None:
    refresh_stats_from_queues(conn, run_id, stats)
    update_run_progress(conn, run_id, stats, _format_errors(stats))


def _stats_from_run(conn, run_id: int) -> CrawlStats:
    run = get_run(conn, run_id)
    stats = CrawlStats()
    if run is None:
        return stats
    stats.pages_scanned = run["pages_scanned"]
    stats.detail_pages_found = run["detail_pages_found"]
    stats.detail_pages_scanned = run["detail_pages_scanned"]
    stats.items_found = run["items_found"]
    stats.skipped_by_robots = run["skipped_by_robots"]
    stats.last_index_url = run["last_index_url"]
    stats.last_detail_url = run["last_detail_url"]
    if run["errors"]:
        try:
            parsed_errors = json.loads(run["errors"])
        except json.JSONDecodeError:
            parsed_errors = [run["errors"]]
        if isinstance(parsed_errors, list):
            stats.errors = [str(error) for error in parsed_errors]
    return stats


def config_from_json(raw_config: str) -> ScrapeConfig:
    return ScrapeConfig(**json.loads(raw_config))


def _format_errors(stats: CrawlStats) -> str:
    if not stats.errors:
        return ""
    return json.dumps(stats.errors[:20], indent=2)