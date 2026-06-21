from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


BROWSE_SECTION_SELECTOR = ".browse-content > .container > section"
DETAIL_LINK_SELECTOR = "div.browse-movie-wrap > .browse-movie-bottom > a[href]"
DOWNLOAD_CONTAINER_SELECTOR = "div#movie-info"
POSTER_IMAGE_SELECTOR = "#movie-poster img"
SYNOPSIS_SELECTOR = "#synopsis"
PUBLICATION_YEAR_SELECTOR = ".browse-movie-bottom > .browse-movie-year, .browse-movie-bottom > browse-movie-year"
DETAILS_SELECTOR = "div.hidden-xs"


@dataclass(frozen=True)
class ScrapeConfig:
    start_url: str
    pagination_template: str = ""
    page_start: int = 1
    page_end: int = 1
    max_detail_pages: int = 0
    delay_seconds: float = 1.0
    timeout_seconds: int = 20
    worker_count: int = 100
    respect_robots: bool = True
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0"


@dataclass(frozen=True)
class ScrapedItem:
    source_url: str
    detail_url: str
    link_url: str
    link_type: str
    name: str
    publication_year: str = ""
    description: str = ""


@dataclass
class CrawlStats:
    pages_scanned: int = 0
    detail_pages_found: int = 0
    detail_pages_scanned: int = 0
    items_found: int = 0
    skipped_by_robots: int = 0
    last_index_url: str = ""
    last_detail_url: str = ""
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DetailLink:
    url: str
    title: str = ""
    publication_year: str = ""


@dataclass(frozen=True)
class DetailMetadata:
    synopsis: str = ""
    poster_url: str = ""


@dataclass(frozen=True)
class DetailTask:
    detail_url: str
    source_url: str
    title: str = ""
    publication_year: str = ""


@dataclass(frozen=True)
class FetchResult:
    html: str | None = None
    error: str = ""
    skipped_by_robots: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class DetailResult:
    detail_url: str
    source_url: str
    items: tuple[ScrapedItem, ...] = ()
    error: str = ""
    skipped_by_robots: bool = False
    fetched: bool = False
    synopsis: str = ""
    poster_url: str = ""


def crawl_site(
    config: ScrapeConfig,
    on_item: Callable[[ScrapedItem], bool],
    on_progress: Callable[[CrawlStats], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> CrawlStats:
    validate_http_url(config.start_url)

    stats = CrawlStats()
    index_session = _make_session(config)
    robots_cache: dict[str, robotparser.RobotFileParser | None] = {}
    robots_lock = threading.Lock()
    rate_limiter = RateLimiter(config.delay_seconds)
    detail_urls_seen: set[str] = set()
    detail_tasks: list[DetailTask] = []

    def progress() -> None:
        if on_progress is not None:
            on_progress(stats)

    def stop_requested() -> bool:
        return should_stop is not None and should_stop()

    for index_url in build_index_urls(config.start_url, config.pagination_template, config.page_start, config.page_end):
        if stop_requested():
            stats.cancelled = True
            progress()
            return stats

        index_result = fetch_url(
            index_session,
            index_url,
            config,
            rate_limiter,
            robots_cache,
            robots_lock,
            stop_requested,
        )
        if index_result.cancelled:
            stats.cancelled = True
            progress()
            return stats
        if index_result.skipped_by_robots:
            stats.skipped_by_robots += 1
            progress()
            continue
        if index_result.error:
            stats.errors.append(index_result.error)
            progress()
            continue
        if not index_result.html:
            continue

        stats.pages_scanned += 1
        stats.last_index_url = index_url
        detail_links = find_detail_links(index_result.html, index_url)
        stats.detail_pages_found += len(detail_links)

        for detail_link in detail_links:
            if detail_link.url in detail_urls_seen:
                continue
            if config.max_detail_pages and len(detail_tasks) >= config.max_detail_pages:
                break

            detail_urls_seen.add(detail_link.url)
            detail_tasks.append(
                DetailTask(
                    detail_url=detail_link.url,
                    source_url=index_url,
                    title=detail_link.title,
                    publication_year=detail_link.publication_year,
                )
            )
        progress()

        if config.max_detail_pages and len(detail_tasks) >= config.max_detail_pages:
            break

    if stop_requested():
        stats.cancelled = True
        progress()
        return stats

    worker_count = max(1, min(config.worker_count, 100))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                crawl_detail_page,
                task,
                config,
                rate_limiter,
                robots_cache,
                robots_lock,
                stop_requested,
            )
            for task in detail_tasks
        ]

        for future in as_completed(futures):
            if stop_requested():
                stats.cancelled = True
                for pending in futures:
                    pending.cancel()

            if future.cancelled():
                progress()
                continue

            try:
                result = future.result()
            except Exception as exc:
                stats.errors.append(str(exc))
                progress()
                continue

            if result.skipped_by_robots:
                stats.skipped_by_robots += 1
            if result.error:
                stats.errors.append(result.error)
            if result.fetched:
                stats.detail_pages_scanned += 1
                stats.last_detail_url = result.detail_url

            for item in result.items:
                if on_item(item):
                    stats.items_found += 1

            progress()

    return stats


def validate_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Starting page must be a full http or https URL.")


def build_index_urls(start_url: str, pagination_template: str, page_start: int, page_end: int) -> list[str]:
    if page_end < page_start:
        raise ValueError("Last page number must be greater than or equal to the first page number.")

    urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            urls.append(url)

    add(start_url)
    if pagination_template:
        for page_number in range(page_start, page_end + 1):
            add(apply_pagination_template(start_url, pagination_template, page_number))
    return urls


def apply_pagination_template(start_url: str, pagination_template: str, page_number: int) -> str:
    template = pagination_template.strip()
    if "{page}" in template:
        rendered = template.replace("{page}", str(page_number))
    else:
        rendered = f"{template}{page_number}"

    parsed_rendered = urlsplit(rendered)
    if parsed_rendered.scheme in {"http", "https"} and parsed_rendered.netloc:
        return rendered

    parsed_start = urlsplit(start_url)
    if rendered.startswith("?"):
        return urlunsplit((parsed_start.scheme, parsed_start.netloc, parsed_start.path, rendered[1:], ""))
    if rendered.startswith("&"):
        existing_query = parsed_start.query
        query = f"{existing_query}{rendered}" if existing_query else rendered[1:]
        return urlunsplit((parsed_start.scheme, parsed_start.netloc, parsed_start.path, query, ""))
    if "=" in rendered and not rendered.startswith(("/", "./", "../")):
        return urlunsplit((parsed_start.scheme, parsed_start.netloc, parsed_start.path, rendered, ""))
    return urljoin(start_url, rendered)


def find_detail_links(html: str, page_url: str) -> list[DetailLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[DetailLink] = []
    seen: set[str] = set()
    browse_sections = soup.select(BROWSE_SECTION_SELECTOR)
    search_roots = browse_sections or [soup]

    for root in search_roots:
        for anchor in root.select(DETAIL_LINK_SELECTOR):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            absolute = urljoin(page_url, href)
            if absolute in seen:
                continue

            seen.add(absolute)
            wrapper = anchor.find_parent("div", class_="browse-movie-wrap")
            year_node = wrapper.select_one(PUBLICATION_YEAR_SELECTOR) if wrapper is not None else None
            links.append(
                DetailLink(
                    url=absolute,
                    title=anchor.get_text(" ", strip=True),
                    publication_year=year_node.get_text(" ", strip=True) if year_node is not None else "",
                )
            )
    return links



def extract_detail_metadata(html: str, detail_url: str) -> DetailMetadata:
    soup = BeautifulSoup(html, "html.parser")
    synopsis_node = soup.select_one(SYNOPSIS_SELECTOR)
    poster_node = soup.select_one(POSTER_IMAGE_SELECTOR)
    raw_poster_url = ""
    if poster_node is not None:
        raw_poster_url = (
            poster_node.get("src")
            or poster_node.get("data-src")
            or poster_node.get("data-lazy-src")
            or ""
        ).strip()
    return DetailMetadata(
        synopsis=synopsis_node.get_text("\n", strip=True) if synopsis_node is not None else "",
        poster_url=urljoin(detail_url, raw_poster_url) if raw_poster_url else "",
    )


def extract_download_items(
    html: str,
    detail_url: str,
    source_url: str,
    item_title: str = "",
    publication_year: str = "",
) -> list[ScrapedItem]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(DOWNLOAD_CONTAINER_SELECTOR)
    if container is None:
        return []

    details = _extract_details(soup)
    fallback_name = _page_title(soup)
    items: list[ScrapedItem] = []
    seen_links: set[str] = set()

    for anchor in container.select("a[href]"):
        raw_href = (anchor.get("href") or "").strip()
        if not raw_href:
            continue

        link_url = raw_href if raw_href.lower().startswith("magnet:?") else urljoin(detail_url, raw_href)
        link_type = classify_download_link(link_url)
        if link_type is None or link_url in seen_links:
            continue

        seen_links.add(link_url)
        anchor_title = (anchor.get("title") or "").strip()
        anchor_text = anchor.get_text(" ", strip=True)
        name = item_title or anchor_title or anchor_text or fallback_name or "Untitled torrent"
        description = _join_description(details, anchor_text, name)
        items.append(
            ScrapedItem(
                source_url=source_url,
                detail_url=detail_url,
                link_url=link_url,
                link_type=link_type,
                name=name,
                publication_year=publication_year,
                description=description,
            )
        )

    return items


def classify_download_link(link_url: str) -> str | None:
    lowered = link_url.lower()
    if lowered.startswith("magnet:?"):
        return "magnet"

    path = urlsplit(lowered).path
    if path.endswith(".torrent") or ".torrent" in path:
        return "torrent"
    return None


def crawl_detail_page(
    task: DetailTask,
    config: ScrapeConfig,
    rate_limiter: "RateLimiter",
    robots_cache: dict[str, robotparser.RobotFileParser | None],
    robots_lock: threading.Lock,
    should_stop: Callable[[], bool] | None = None,
) -> DetailResult:
    if should_stop is not None and should_stop():
        return DetailResult(detail_url=task.detail_url, source_url=task.source_url)

    session = _make_session(config)
    result = fetch_url(session, task.detail_url, config, rate_limiter, robots_cache, robots_lock, should_stop)
    if result.cancelled:
        return DetailResult(detail_url=task.detail_url, source_url=task.source_url)
    if should_stop is not None and should_stop():
        return DetailResult(detail_url=task.detail_url, source_url=task.source_url, fetched=bool(result.html))
    if result.skipped_by_robots:
        return DetailResult(detail_url=task.detail_url, source_url=task.source_url, skipped_by_robots=True)
    if result.error:
        return DetailResult(detail_url=task.detail_url, source_url=task.source_url, error=result.error)
    if not result.html:
        return DetailResult(detail_url=task.detail_url, source_url=task.source_url)

    metadata = extract_detail_metadata(result.html, task.detail_url)
    items = tuple(
        extract_download_items(
            result.html,
            task.detail_url,
            task.source_url,
            item_title=task.title,
            publication_year=task.publication_year,
        )
    )
    return DetailResult(
        detail_url=task.detail_url,
        source_url=task.source_url,
        items=items,
        fetched=True,
        synopsis=metadata.synopsis,
        poster_url=metadata.poster_url,
    )


def fetch_url(
    session: requests.Session,
    url: str,
    config: ScrapeConfig,
    rate_limiter: "RateLimiter",
    robots_cache: dict[str, robotparser.RobotFileParser | None],
    robots_lock: threading.Lock,
    should_stop: Callable[[], bool] | None = None,
) -> FetchResult:
    if should_stop is not None and should_stop():
        return FetchResult(cancelled=True)
    if config.respect_robots and not _can_fetch(session, robots_cache, robots_lock, url, config):
        return FetchResult(skipped_by_robots=True)

    if not rate_limiter.wait(should_stop):
        return FetchResult(cancelled=True)
    if should_stop is not None and should_stop():
        return FetchResult(cancelled=True)
    try:
        response = session.get(url, timeout=config.timeout_seconds)
        response.raise_for_status()
    except requests.RequestException as exc:
        return FetchResult(error=f"{url}: {exc}")
    return FetchResult(html=response.text)


class RateLimiter:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = max(delay_seconds, 0)
        self.last_request_at = 0.0
        self.lock = threading.Lock()

    def wait(self, should_stop: Callable[[], bool] | None = None) -> bool:
        if self.delay_seconds <= 0:
            return should_stop is None or not should_stop()
        with self.lock:
            now = time.monotonic()
            wait_for = self.delay_seconds - (now - self.last_request_at)
            if self.last_request_at and wait_for > 0:
                deadline = time.monotonic() + wait_for
                while True:
                    if should_stop is not None and should_stop():
                        return False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.25))
            self.last_request_at = time.monotonic()
        return should_stop is None or not should_stop()


def _make_session(config: ScrapeConfig) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": config.user_agent})
    return session


def _extract_details(soup: BeautifulSoup) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for node in soup.select(DETAILS_SELECTOR):
        text = node.get_text(" ", strip=True)
        if text and text not in seen:
            seen.add(text)
            chunks.append(text)
    return "\n\n".join(chunks)


def _page_title(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def _join_description(details: str, anchor_text: str, name: str) -> str:
    if not anchor_text or anchor_text == name:
        return details
    if not details:
        return anchor_text
    return f"{details}\n\nLink text: {anchor_text}"


def _can_fetch(
    session: requests.Session,
    robots_cache: dict[str, robotparser.RobotFileParser | None],
    robots_lock: threading.Lock,
    url: str,
    config: ScrapeConfig,
) -> bool:
    parsed = urlsplit(url)
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))

    with robots_lock:
        if robots_url not in robots_cache:
            parser = robotparser.RobotFileParser()
            try:
                response = session.get(robots_url, timeout=config.timeout_seconds)
                if response.status_code >= 400:
                    robots_cache[robots_url] = None
                else:
                    parser.parse(response.text.splitlines())
                    robots_cache[robots_url] = parser
            except requests.RequestException:
                robots_cache[robots_url] = None

        parser = robots_cache[robots_url]
    return True if parser is None else parser.can_fetch(config.user_agent, url)