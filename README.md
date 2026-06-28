# Torrent Getter

A small Flask app for collecting magnet and `.torrent` links from open academic or public-domain sources.

The crawler scans a starting page plus optional paginated pages, follows detail links from `div.browse-movie-wrap > .browse-movie-bottom > a[href]`, uses that anchor text as the saved title, stores the browse-card publication year, then extracts magnet and `.torrent` links from the detail page download section. Text from `div.hidden-xs` is saved as item details, `#synopsis` is saved as the synopsis, and the image under `#movie-poster img` is downloaded for display.

Crawls run in the background after you submit the form. Index pages and discovered detail links are stored in SQLite with statuses so a stopped run can be resumed without rebuilding completed work. Detail pages are deduplicated before processing, then scanned by up to 100 workers so two workers do not scrape the same detail page. Saved magnet and torrent links are keyed by URL. Already-added links refresh their title, publication year, and last-seen time in later crawls, but they are not counted as newly saved links or reassigned to the newest run.

Each new crawl runs in its own worker process. Use **Stop scrape** on a run page to stop one crawl, or **Stop all** in the header to terminate all worker processes started by the current app process. If a worker was started by an older app version or another Flask process, the stop request is still saved in SQLite and the worker will stop when it next checks the database.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

```powershell
python app.py
```

The listener is configured from `.env`:

```dotenv
APP_HOST=0.0.0.0
APP_PORT=5000
APP_DEBUG=false
RSS_FEED_LIMIT=500
```

Use `127.0.0.1` for local-only access, or `0.0.0.0` to listen on the LAN. Open the Flask URL shown in the terminal, usually `http://127.0.0.1:5000` for local-only or `http://<your-computer-ip>:5000` from another device on the network.

## Radarr RSS Feed

The saved movie releases are available at:

```text
http://torrent_getter:5000/rss/movies.xml
```

When Radarr and Torrent Getter are attached to the same Docker network, add this URL in Radarr under **Settings > Indexers > Add > Torrent RSS Feed**. Enable RSS sync and enable **Allow Zero Size**, because source pages do not always publish torrent sizes. For a non-container installation, replace `torrent_getter` with the machine IP and exposed port.

Each saved magnet or `.torrent` link is a separate feed entry. Release titles include the movie name, year, and detected resolution so Radarr can match them correctly. `RSS_FEED_LIMIT` controls how many of the newest saved releases are published and defaults to 500.

## Container On Debian

Install Docker and the Compose plugin on the Debian host:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
```

Clone and run the app:

```bash
git clone https://github.com/cryptocent/torrent_getter.git
cd torrent_getter
printf "APP_HOST=0.0.0.0\nAPP_PORT=5000\nAPP_DEBUG=false\nRSS_FEED_LIMIT=500\n" > .env
docker compose up -d --build
```

Open `http://<debian-machine-ip>:5000` from your browser. The SQLite database and downloaded posters are persisted in `./instance` on the host.

Useful commands:

```bash
docker compose logs -f
docker compose restart
docker compose down
```
## Pagination

Use `{page}` where the page number belongs:

- `?page={page}`
- `/archive?page={page}`
- `https://example.org/archive?page={page}`

The app always includes the starting page URL once, then adds the generated pagination URLs.

## Data

SQLite data is stored at `instance/torrent_getter.sqlite` by default.

The latest crawl can be opened from the **Latest run** link. Each run stores status, start/finish times, worker count, progress counters, the last index/detail page touched, errors, discovered detail-link statuses, poster filenames, synopsis text, and the links saved by that run.

Only crawl sites and files you are allowed to access and redistribute.
