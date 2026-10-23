import argparse
import sys

from ml_common.catalogue import load_image_catalogue
from ml_common.config import MLConfig, setup_logging
from ml_common.images import fetch_images, seed_from_shards
from ml_common.storage import Storage

LEGACY_SHARDS = "hf://datasets/brianbarry97/irish_supermarket_data/images/part-*.parquet"


def main(argv: list[str] | None = None) -> int:
    cfg = MLConfig.from_env()
    parser = argparse.ArgumentParser(description="Maintain the content-addressed image store.")
    parser.add_argument("--store", default=cfg.store)
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="Download images not yet in the image map.")
    fetch.add_argument("--catalogue", default=cfg.catalogue)
    fetch.add_argument("--limit", type=int)
    fetch.add_argument("--supermarkets", nargs="+")
    fetch.add_argument("--retry-failed", action="store_true")

    seed = commands.add_parser("seed", help="Import the legacy HF dataset image shards.")
    seed.add_argument("--source", default=LEGACY_SHARDS)
    seed.add_argument("--max-rows", type=int)

    args = parser.parse_args(argv)
    logger = setup_logging("images")
    storage = Storage(args.store)
    if args.command == "seed":
        logger.info(
            "%s",
            seed_from_shards(
                storage, args.source, batch_rows=cfg.chunk_rows, max_rows=args.max_rows
            ),
        )
        return 0
    summary = fetch_images(
        storage,
        load_image_catalogue(args.catalogue),
        cfg,
        limit=args.limit,
        supermarkets=args.supermarkets,
        retry_failed=args.retry_failed,
    )
    return 1 if summary.failures else 0


if __name__ == "__main__":
    sys.exit(main())
