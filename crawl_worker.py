from __future__ import annotations

import argparse

from jobs import config_from_json, run_crawl_job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    run_crawl_job(args.database, args.run_id, config_from_json(args.config))


if __name__ == "__main__":
    main()
