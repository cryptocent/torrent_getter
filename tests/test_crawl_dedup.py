import scraper


def test_crawl_site_counts_only_items_that_callback_saves(monkeypatch):
    index_html = """
    <div class="browse-content">
      <div class="container">
        <section>
          <div class="browse-movie-wrap">
            <div class="browse-movie-bottom">
              <a href="/detail/one">Browse card title</a>
              <div class="browse-movie-year">2024</div>
            </div>
          </div>
        </section>
      </div>
    </div>
    """
    detail_html = """
    <div id="movie-info">
      <a href="magnet:?xt=urn:btih:new">New magnet</a>
      <a href="magnet:?xt=urn:btih:existing">Existing magnet</a>
    </div>
    """

    def fake_fetch_url(session, url, config, rate_limiter, robots_cache, robots_lock, should_stop=None):
        if url == "https://example.org/archive":
            return scraper.FetchResult(html=index_html)
        if url == "https://example.org/detail/one":
            return scraper.FetchResult(html=detail_html)
        raise AssertionError(f"Unexpected URL: {url}")

    saved_items = []

    def save_item(item):
        if item.link_url == "magnet:?xt=urn:btih:new":
            saved_items.append(item)
            return True
        return False

    monkeypatch.setattr(scraper, "fetch_url", fake_fetch_url)

    stats = scraper.crawl_site(
        scraper.ScrapeConfig(
            start_url="https://example.org/archive",
            delay_seconds=0,
            worker_count=1,
            respect_robots=False,
        ),
        save_item,
    )

    assert stats.items_found == 1
    assert [item.link_url for item in saved_items] == ["magnet:?xt=urn:btih:new"]
    assert saved_items[0].name == "Browse card title"
    assert saved_items[0].publication_year == "2024"