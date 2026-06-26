from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for

from db import (
    cleanup_incomplete_work,
    connect,
    create_run,
    get_items_for_run,
    get_item,
    get_item_downloads,
    get_latest_run,
    get_detail_links_for_run,
    get_publication_years,
    get_recent_items,
    get_recent_runs,
    get_run,
    get_run_queue_summary,
    init_db,
    mark_run_cancelled,
    request_all_run_stops,
    request_run_stop,
    retry_failed_work,
    search_catalog_items,
)
from jobs import CrawlJobManager
from scraper import ScrapeConfig, validate_http_url


SORT_OPTIONS = [
    ("last_seen_desc", "Newest saved"),
    ("last_seen_asc", "Oldest saved"),
    ("title_asc", "Title A to Z"),
    ("title_desc", "Title Z to A"),
    ("year_desc", "Newest year"),
    ("year_asc", "Oldest year"),
]
SORT_KEYS = {value for value, _label in SORT_OPTIONS}
GRID_COLUMN_COUNT = 3
GRID_MIN_ROWS = 2
ITEMS_PER_PAGE_MIN = 5
ITEMS_PER_PAGE_MAX = 100


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="dev",
        DATABASE=str(Path(app.instance_path) / "torrent_getter.sqlite"),
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    init_db(app.config["DATABASE"])
    app.extensions["crawl_jobs"] = CrawlJobManager(app.config["DATABASE"])

    def db_connect():
        return connect(app.config["DATABASE"])

    @app.get("/")
    def index():
        with db_connect() as conn:
            return render_template(
                "index.html",
                recent_runs=get_recent_runs(conn),
                recent_items=get_recent_items(conn, limit=8),
            )

    @app.post("/crawl")
    def start_crawl():
        form = request.form
        try:
            config = ScrapeConfig(
                start_url=form.get("start_url", "").strip(),
                pagination_template=form.get("pagination_template", "").strip(),
                page_start=_int_from_form(form, "page_start", 1),
                page_end=_int_from_form(form, "page_end", 1),
                max_detail_pages=_int_from_form(form, "max_detail_pages", 100),
                delay_seconds=_float_from_form(form, "delay_seconds", 1.0),
                timeout_seconds=_int_from_form(form, "timeout_seconds", 20),
                worker_count=_int_from_form(form, "worker_count", 10),
                respect_robots=form.get("respect_robots") == "on",
            )
            validate_http_url(config.start_url)
            if config.page_end < config.page_start:
                raise ValueError("Last page number must be greater than or equal to the first page number.")
            if config.max_detail_pages < 0:
                raise ValueError("Max detail pages cannot be negative.")
            if config.delay_seconds < 0:
                raise ValueError("Delay cannot be negative.")
            if config.worker_count < 1 or config.worker_count > 100:
                raise ValueError("Workers must be between 1 and 100.")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        with db_connect() as conn:
            run_id = create_run(conn, config)

        app.extensions["crawl_jobs"].start(run_id, config)
        flash("Crawl started in the background. This page will show progress as it runs.", "success")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.get("/runs/latest")
    def latest_run():
        with db_connect() as conn:
            run = get_latest_run(conn)
        if run is None:
            flash("No crawl runs yet.", "warning")
            return redirect(url_for("index"))
        return redirect(url_for("run_detail", run_id=run["id"]))

    @app.get("/runs/<int:run_id>")
    def run_detail(run_id: int):
        with db_connect() as conn:
            run = get_run(conn, run_id)
            if run is None:
                abort(404)
            run_items = get_items_for_run(conn, run_id, limit=100)
            detail_links = get_detail_links_for_run(conn, run_id, limit=100)
            work_summary = get_run_queue_summary(conn, run_id)

        worker_active = app.extensions["crawl_jobs"].is_running(run_id)
        is_running = run["status"] in {"queued", "running"} or worker_active
        can_resume = not worker_active and run["status"] in {"stopping", "cancelled", "failed", "completed_with_errors"}
        return render_template(
            "run_detail.html",
            run=run,
            items=run_items,
            detail_links=detail_links,
            work_summary=work_summary,
            is_running=is_running,
            can_resume=can_resume,
            worker_active=worker_active,
        )

    @app.post("/runs/<int:run_id>/resume")
    def resume_run(run_id: int):
        manager = app.extensions["crawl_jobs"]
        with db_connect() as conn:
            run = get_run(conn, run_id)
            if run is None:
                abort(404)
            if run["status"] in {"queued", "running", "stopping"} or manager.is_running(run_id):
                flash("That crawl is already running.", "warning")
                return redirect(url_for("run_detail", run_id=run_id))
            config = _config_from_run(run)

        manager.start(run_id, config)
        flash("Crawl resumed. It will continue from saved detail-link progress.", "success")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/runs/<int:run_id>/retry-errors")
    def retry_run_errors(run_id: int):
        manager = app.extensions["crawl_jobs"]
        with db_connect() as conn:
            run = get_run(conn, run_id)
            if run is None:
                abort(404)
            if run["status"] in {"queued", "running", "stopping"} or manager.is_running(run_id):
                flash("That crawl is already active. Let it finish or stop it before retrying errors.", "warning")
                return redirect(url_for("run_detail", run_id=run_id))

            changed = retry_failed_work(conn, run_id)
            config = _config_from_run(run) if changed else None
            conn.commit()

        if changed and config is not None:
            manager.start(run_id, config)
            flash(f"Retry queued {changed} errored page(s). The crawl will continue from saved progress.", "success")
        else:
            flash("No errored detail or index pages were found for that run.", "warning")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/runs/<int:run_id>/cleanup")
    def cleanup_run(run_id: int):
        manager = app.extensions["crawl_jobs"]
        with db_connect() as conn:
            run = get_run(conn, run_id)
            if run is None:
                abort(404)
            if run["status"] in {"queued", "running"} or manager.is_running(run_id):
                flash("Stop the crawl before cleaning up pending work.", "warning")
                return redirect(url_for("run_detail", run_id=run_id))

            changed = cleanup_incomplete_work(conn, run_id)
            conn.commit()

        if changed:
            flash(f"Cleaned up {changed} pending or processing page(s).", "success")
        else:
            flash("No pending or processing work was found for that run.", "warning")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/runs/<int:run_id>/stop")
    def stop_run(run_id: int):
        manager = app.extensions["crawl_jobs"]
        active_thread = manager.is_running(run_id)
        with db_connect() as conn:
            run = get_run(conn, run_id)
            if run is None:
                abort(404)

            if run["status"] not in {"queued", "running", "stopping"} and not active_thread:
                flash("That crawl is not running.", "warning")
                return redirect(url_for("run_detail", run_id=run_id))

            request_run_stop(conn, run_id)
            stopped_active_thread = manager.stop(run_id)
            if stopped_active_thread:
                mark_run_cancelled(conn, run_id)
            conn.commit()

        if stopped_active_thread:
            flash("Worker process stopped and the crawl was marked cancelled.", "success")
        else:
            flash("Stop requested. If the scrape is running in another app process, it will pick this up from the database shortly.", "success")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/runs/stop-all")
    def stop_all_runs():
        manager = app.extensions["crawl_jobs"]
        with db_connect() as conn:
            marked_count = request_all_run_stops(conn)
            stopped_run_ids = manager.stop_all()
            for stopped_run_id in stopped_run_ids:
                mark_run_cancelled(conn, stopped_run_id)
            conn.commit()

        if stopped_run_ids:
            flash(f"Stopped {len(stopped_run_ids)} active worker process(es).", "success")
        elif marked_count:
            flash("Stop requested for all running scrapes. Workers owned by another process will pick it up from the database.", "success")
        else:
            flash("No running scrapes found.", "warning")
        return redirect(request.referrer or url_for("index"))

    @app.get("/items")
    def items():
        query = request.args.get("q", "").strip()
        link_type = request.args.get("type", "").strip()
        year = request.args.get("year", "").strip()
        sort = request.args.get("sort", "last_seen_desc").strip() or "last_seen_desc"
        if sort not in SORT_KEYS:
            sort = "last_seen_desc"
        display_mode = request.args.get("display", "list").strip() or "list"
        if display_mode not in {"list", "grid"}:
            display_mode = "list"
        page = max(_int_from_args("page", 1), 1)
        per_page = _per_page_for_display(_int_from_args("per_page", 25), display_mode)
        offset = (page - 1) * per_page

        with db_connect() as conn:
            rows, total = search_catalog_items(
                conn,
                query=query,
                year=year,
                link_type=link_type,
                sort=sort,
                limit=per_page,
                offset=offset,
            )
            years = get_publication_years(conn)

        next_page = page + 1 if offset + per_page < total else None
        previous_page = page - 1 if page > 1 else None
        return render_template(
            "items.html",
            items=rows,
            total=total,
            query=query,
            link_type=link_type,
            year=year,
            years=years,
            sort=sort,
            sort_options=SORT_OPTIONS,
            display_mode=display_mode,
            page=page,
            per_page=per_page,
            next_page=next_page,
            previous_page=previous_page,
        )

    @app.get("/items/<int:item_id>")
    def item_detail(item_id: int):
        with db_connect() as conn:
            item = get_item(conn, item_id)
            downloads = get_item_downloads(conn, item_id)
        if item is None:
            abort(404)
        return render_template("item_detail.html", item=item, downloads=downloads)

    @app.get("/posters/<path:filename>")
    def poster(filename: str):
        return send_from_directory(Path(app.instance_path) / "posters", filename)

    return app


def _config_from_run(run) -> ScrapeConfig:
    return ScrapeConfig(
        start_url=run["start_url"],
        pagination_template=run["pagination_template"] or "",
        page_start=run["page_start"],
        page_end=run["page_end"],
        max_detail_pages=run["max_detail_pages"],
        delay_seconds=run["delay_seconds"],
        timeout_seconds=20,
        worker_count=run["worker_count"],
        respect_robots=bool(run["respect_robots"]),
    )


def _int_from_form(form, name: str, default: int) -> int:
    value = form.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name.replace('_', ' ').title()} must be a whole number.") from exc


def _float_from_form(form, name: str, default: float) -> float:
    value = form.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name.replace('_', ' ').title()} must be a number.") from exc


def _int_from_args(name: str, default: int) -> int:
    value = request.args.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _per_page_for_display(requested: int, display_mode: str) -> int:
    per_page = min(max(requested, ITEMS_PER_PAGE_MIN), ITEMS_PER_PAGE_MAX)
    if display_mode != "grid":
        return per_page

    per_page = max(per_page, GRID_COLUMN_COUNT * GRID_MIN_ROWS)
    return per_page - (per_page % GRID_COLUMN_COUNT)


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _server_config_from_env() -> dict:
    return {
        "host": _env_value(("APP_HOST", "FLASK_RUN_HOST"), "127.0.0.1"),
        "port": _int_from_env(("APP_PORT", "FLASK_RUN_PORT"), 5000),
        "debug": _bool_from_env(("APP_DEBUG", "FLASK_DEBUG"), False),
    }


def _env_value(names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _int_from_env(names: tuple[str, ...], default: int) -> int:
    raw_value = _env_value(names, "")
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{names[0]} must be a whole number.") from exc


def _bool_from_env(names: tuple[str, ...], default: bool) -> bool:
    raw_value = _env_value(names, "")
    if not raw_value:
        return default
    return raw_value.lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    _load_env_file(Path(__file__).with_name(".env"))
    create_app().run(**_server_config_from_env())