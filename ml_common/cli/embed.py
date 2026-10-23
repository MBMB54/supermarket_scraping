import argparse

from ml_common.catalogue import load_catalogue
from ml_common.config import MLConfig, setup_logging
from ml_common.embedders import SentenceTransformerEmbedder
from ml_common.pipeline import run_embed
from ml_common.specs import REGISTRY, get_spec
from ml_common.storage import Storage


def main(argv: list[str] | None = None) -> None:
    cfg = MLConfig.from_env()
    parser = argparse.ArgumentParser(description="Embed catalogue rows missing for a spec.")
    parser.add_argument("--spec", required=True, choices=sorted(REGISTRY))
    parser.add_argument("--catalogue", default=cfg.catalogue)
    parser.add_argument("--store", default=cfg.store)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    logger = setup_logging("embed")
    spec = get_spec(args.spec)
    summary = run_embed(
        spec,
        load_catalogue(args.catalogue, spec),
        Storage(args.store),
        SentenceTransformerEmbedder(spec),
        limit=args.limit,
        chunk_rows=cfg.chunk_rows,
    )
    logger.info("%s", summary)


if __name__ == "__main__":
    main()
