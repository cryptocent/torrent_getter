from scraper import build_index_urls, extract_detail_metadata, extract_download_items, find_detail_links


def test_build_index_urls_adds_start_and_paginated_pages():
    urls = build_index_urls("https://example.org/archive", "?page={page}", 2, 3)

    assert urls == [
        "https://example.org/archive",
        "https://example.org/archive?page=2",
        "https://example.org/archive?page=3",
    ]


def test_find_detail_links_uses_nested_browse_movie_bottom_links():
    html = """
    <div class="browse-movie-wrap">
      <a href="/ignored-parent">Ignored parent link</a>
      <div class="browse-movie-bottom">
        <a href="/detail/one">Dataset One</a>
        <div class="browse-movie-year">2023</div>
      </div>
    </div>
    <div><a href="/ignored">Ignored</a></div>
    <div class="browse-movie-wrap other">
      <div class="browse-movie-bottom">
        <a href="https://data.example/detail/two">Dataset Two</a>
        <browse-movie-year>2024</browse-movie-year>
      </div>
    </div>
    """

    links = find_detail_links(html, "https://example.org/archive?page=1")

    assert [link.url for link in links] == [
        "https://example.org/detail/one",
        "https://data.example/detail/two",
    ]
    assert [link.title for link in links] == ["Dataset One", "Dataset Two"]
    assert [link.publication_year for link in links] == ["2023", "2024"]


def test_find_detail_links_prefers_browse_content_section():
    html = """
    <div class="browse-movie-wrap">
      <div class="browse-movie-bottom"><a href="/outside">Outside</a></div>
    </div>
    <div class="browse-content">
      <div class="container">
        <section>
          <div><a href="/ignored">Ignored</a></div>
          <div class="browse-movie-wrap">
            <div class="browse-movie-bottom">
              <a href="/detail/movie">Movie Title</a>
              <div class="browse-movie-year">2025</div>
            </div>
          </div>
        </section>
      </div>
    </div>
    """

    links = find_detail_links(html, "https://example.org/archive?page=1")

    assert [link.url for link in links] == ["https://example.org/detail/movie"]
    assert links[0].title == "Movie Title"
    assert links[0].publication_year == "2025"


def test_extract_download_items_uses_browse_title_and_year():
    html = """
    <html>
      <head><title>Fallback title</title></head>
      <body>
        <div class="hidden-xs">Public dataset details</div>
        <div id="movie-info">
          <a href="magnet:?xt=urn:btih:abc" title="Dataset magnet">Magnet download</a>
          <a href="/files/dataset.torrent">Dataset torrent</a>
          <a href="/not-a-download">Ignore this</a>
        </div>
      </body>
    </html>
    """

    items = extract_download_items(
        html,
        "https://example.org/detail/one",
        "https://example.org/archive",
        item_title="Browse card title",
        publication_year="2024",
    )

    assert len(items) == 2
    assert items[0].link_type == "magnet"
    assert items[0].name == "Browse card title"
    assert items[0].publication_year == "2024"
    assert items[0].description == "Public dataset details\n\nLink text: Magnet download"
    assert items[1].link_type == "torrent"
    assert items[1].name == "Browse card title"
    assert items[1].publication_year == "2024"
    assert items[1].link_url == "https://example.org/files/dataset.torrent"

def test_extract_detail_metadata_reads_synopsis_and_poster_url():
    html = """
    <div id="movie-poster"><img src="/images/poster.jpg"></div>
    <div id="synopsis"><p>First line.</p><p>Second line.</p></div>
    """

    metadata = extract_detail_metadata(html, "https://example.org/detail/one")

    assert metadata.poster_url == "https://example.org/images/poster.jpg"
    assert metadata.synopsis == "First line.\nSecond line."