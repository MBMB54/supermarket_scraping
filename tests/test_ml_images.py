import asyncio
import io
import random
import threading
from dataclasses import replace
from functools import partial

import polars as pl
import pytest
from aiohttp import web
from PIL import Image

from ml_common import image_map, images
from ml_common.catalogue import load_catalogue, load_image_catalogue
from ml_common.cli import images as images_cli
from ml_common.config import MLConfig
from ml_common.embedders import EmbedInput, SentenceTransformerEmbedder
from ml_common.images import fetch_images, inspect_image, seed_from_shards
from ml_common.keys import content_key, image_key
from ml_common.specs import get_spec
from ml_common.storage import Storage
from scraper_common.http import request_with_retry


def picture(seed: str, fmt: str = "PNG", size: int = 32) -> bytes:
    """Deterministic noise image: distinct per seed and large enough to pass the size sanity check."""
    noise = random.Random(seed).randbytes(size * size * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (size, size), noise).save(buf, fmt)
    return buf.getvalue()


RED, BLUE, GREEN = picture("red"), picture("blue"), picture("green", "JPEG")


class Origin:
    """In-process image server on its own event loop; `routes` maps path -> (status, body)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.hits: dict[str, int] = {}
        self.loop = asyncio.new_event_loop()
        self.runner = web.AppRunner(web.Application())
        self.runner.app.router.add_get("/{path:.*}", self._serve)
        self.base = ""

    async def _serve(self, request):
        path = "/" + request.match_info["path"]
        self.hits[path] = self.hits.get(path, 0) + 1
        status, body = self.routes.get(path, (404, b""))
        return web.Response(status=status, body=body)

    def start(self):
        async def boot():
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            return site._server.sockets[0].getsockname()[1]

        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            port = self.loop.run_until_complete(boot())
            self.base = f"http://127.0.0.1:{port}"
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)

    def url(self, path: str) -> str:
        return f"{self.base}{path}"


@pytest.fixture
def origin():
    server = Origin()
    server.start()
    server.routes.update({"/red": (200, RED), "/red2": (200, RED), "/blue": (200, BLUE)})
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(
        images, "request_with_retry", partial(request_with_retry, attempts=2, base_delay=0.0)
    )


@pytest.fixture
def cfg():
    return replace(MLConfig.from_env(), image_delay=0.0, chunk_rows=100, image_max_attempts=3)


def cat(origin, **paths) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "id": list(paths),
            "supermarket": ["tesco"] * len(paths),
            "image_url": [origin.url(p) if p else None for p in paths.values()],
        }
    )


def blobs(store: Storage) -> list[str]:
    return store.list("images")


def test_incremental_fetch_downloads_only_new_and_failed_rows(tmp_path, origin, cfg):
    store = Storage(str(tmp_path))
    origin.routes["/flaky"] = (503, b"")
    catalogue = cat(origin, a="/red", b="/blue", c="/gone", d="/flaky", e=None)

    first = fetch_images(store, catalogue, cfg)
    assert (first.stats.ok, first.stats.not_found, first.stats.retryable) == (2, 1, 1)
    assert first.by_supermarket == {"tesco": {"ok": 2, "permanent": 1, "transient": 1}}
    assert first.bytes_total == len(RED) + len(BLUE) and first.failures

    origin.routes["/flaky"] = (200, GREEN)
    hits = dict(origin.hits)
    second = fetch_images(store, catalogue, cfg)
    assert second.pending == 1 and second.stats.ok == 1 and not second.failures
    assert origin.hits["/flaky"] == hits["/flaky"] + 1
    assert all(origin.hits[p] == hits[p] for p in ("/red", "/blue", "/gone"))

    third = fetch_images(store, catalogue, cfg)
    assert third.pending == 0 and not third.failures
    permanent = image_map.read_map(store).filter(pl.col("status") == image_map.PERMANENT)
    assert permanent["error"].to_list() == ["HTTP 404"] and permanent["attempts"].to_list() == [1]

    again = fetch_images(store, catalogue, cfg, retry_failed=True)
    assert again.pending == 1 and origin.hits["/gone"] == hits["/gone"] + 1


def test_identical_bytes_share_one_blob_and_identical_urls_one_request(tmp_path, origin, cfg):
    store = Storage(str(tmp_path))
    catalogue = cat(origin, a="/red", b="/red2", c="/red")
    summary = fetch_images(store, catalogue, replace(cfg, chunk_rows=1))
    assert origin.hits["/red"] == 1
    assert blobs(store) == [image_key(content_key(RED))]
    assert store.read_bytes(image_key(content_key(RED))) == RED
    rows = image_map.read_map(store)
    assert rows["image_sha256"].n_unique() == 1 and len(rows) == 3
    assert summary.blobs_reused >= 1
    assert rows["content_type"].unique().to_list() == ["image/png"]
    assert (rows["width"][0], rows["height"][0], rows["bytes"][0]) == (32, 32, len(RED))


def test_corrupt_payload_is_a_permanent_error_retried_only_on_request(tmp_path, origin, cfg):
    store = Storage(str(tmp_path))
    origin.routes.update(
        {"/html": (200, b"<html>" + b"x" * 400), "/pixel": (200, picture("red", size=2))}
    )
    catalogue = cat(origin, a="/html", b="/pixel", c="/red")
    fetch_images(store, catalogue, cfg)
    failed = image_map.read_map(store).filter(pl.col("status") != image_map.OK).sort("id")
    assert failed["status"].to_list() == [image_map.PERMANENT] * 2
    assert "not a decodable image" in failed["error"][0]
    assert blobs(store) == [image_key(content_key(RED))]

    assert fetch_images(store, catalogue, cfg).pending == 0
    origin.routes["/html"] = (200, BLUE)
    retried = fetch_images(store, catalogue, cfg, retry_failed=True)
    assert retried.pending == 2 and retried.stats.ok == 1
    latest = image_map.latest(image_map.read_map(store)).filter(pl.col("id") == "a")
    assert latest["status"].to_list() == [image_map.OK] and latest["attempts"].to_list() == [2]


def test_transient_failures_stop_retrying_at_the_attempt_cap(tmp_path, origin, cfg):
    store = Storage(str(tmp_path))
    origin.routes["/flaky"] = (503, b"")
    catalogue = cat(origin, a="/flaky")
    pendings = [fetch_images(store, catalogue, cfg).pending for _ in range(4)]
    assert pendings == [1, 1, 1, 0]
    assert fetch_images(store, catalogue, cfg, retry_failed=True).pending == 1


def test_retry_after_is_honoured_and_transport_errors_are_transient(tmp_path, origin, cfg):
    assert images._retry_after(type("R", (), {"headers": {"Retry-After": "7"}})) == 7
    assert images._retry_after(type("R", (), {"headers": {"Retry-After": "Wed, 21 Oct"}})) is None
    store = Storage(str(tmp_path))
    origin.stop()
    dead = cat(origin, a="/red")
    summary = fetch_images(store, dead, cfg)
    assert summary.stats.retryable == 1
    assert image_map.read_map(store)["status"].to_list() == [image_map.TRANSIENT]
    origin.start()


def test_interrupted_run_resumes_without_redoing_finished_batches(
    tmp_path, origin, cfg, monkeypatch
):
    store = Storage(str(tmp_path))
    origin.routes.update(
        {
            "/a": (200, picture("red")),
            "/b": (200, picture("blue")),
            "/c": (200, picture("green")),
            "/d": (200, picture("yellow")),
        }
    )
    catalogue = cat(origin, a="/a", b="/b", c="/c", d="/d")
    small = replace(cfg, chunk_rows=2)

    real, calls = Storage.append_parquet, []

    def crash_on_second(self, df, prefix):
        calls.append(prefix)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(self, df, prefix)

    monkeypatch.setattr(Storage, "append_parquet", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        fetch_images(store, catalogue, small)
    monkeypatch.undo()
    assert len(image_map.read_map(store)) == 2

    resumed = fetch_images(store, catalogue, small)
    assert resumed.pending == 2 and resumed.blobs_reused == 2
    assert origin.hits["/a"] == origin.hits["/b"] == 1
    assert len(blobs(store)) == 4 and fetch_images(store, catalogue, small).pending == 0


def test_url_change_refetches_and_catalogue_follows_the_new_hash(tmp_path, origin, cfg):
    store = Storage(str(tmp_path))
    fetch_images(store, cat(origin, a="/red"), cfg)
    moved = cat(origin, a="/blue")
    assert attach(store, moved)["image_sha256"].to_list() == [None]

    summary = fetch_images(store, moved, cfg)
    assert summary.pending == 1
    assert attach(store, moved)["image_sha256"].to_list() == [content_key(BLUE)]
    assert attach(store, cat(origin, a="/red"))["image_sha256"].to_list() == [content_key(RED)]
    assert len(image_map.read_map(store)) == 2


def attach(store: Storage, catalogue: pl.DataFrame) -> pl.DataFrame:
    return image_map.attach_image_sha256(catalogue, image_map.read_map(store))


def test_supermarkets_filter_limit_and_interleaving(tmp_path, origin, cfg):
    catalogue = pl.concat(
        [
            cat(origin, a="/red", b="/red2").with_columns(supermarket=pl.lit("tesco")),
            cat(origin, c="/blue", d="/red").with_columns(supermarket=pl.lit("aldi")),
        ]
    )
    todo = image_map.pending(
        catalogue, image_map.read_map(Storage(str(tmp_path))), max_attempts=3, retry_failed=False
    )
    assert todo["supermarket"].to_list() == ["aldi", "tesco", "aldi", "tesco"]

    store = Storage(str(tmp_path / "s"))
    only = fetch_images(store, catalogue, cfg, supermarkets=["aldi"], limit=1)
    assert only.pending == 1 and image_map.read_map(store)["supermarket"].to_list() == ["aldi"]


def shard(path, rows):
    pl.DataFrame(
        rows,
        schema={
            "id": pl.String,
            "supermarket": pl.String,
            "title": pl.String,
            "title_cleaned": pl.String,
            "image_url": pl.String,
            "image_bytes": pl.Binary,
        },
        orient="row",
    ).write_parquet(path)


def test_seed_imports_shards_by_hash_and_prevents_refetch(tmp_path, origin, cfg):
    src = tmp_path / "src"
    src.mkdir()
    shard(
        src / "part-00000.parquet",
        [
            ("a", "tesco", "t", "t", origin.url("/red"), RED),
            ("b", "tesco", "t", "t", origin.url("/red2"), RED),
            ("c", "aldi", "t", "t", origin.url("/blue"), None),
            ("d", "aldi", "t", "t", origin.url("/junk"), b"x" * 300),
        ],
    )
    shard(src / "part-00001.parquet", [("e", "tesco", "t", "t", origin.url("/green"), GREEN)])
    store = Storage(str(tmp_path / "store"))

    summary = seed_from_shards(store, str(src / "part-*.parquet"), batch_rows=2)
    assert (summary.rows_seen, summary.imported, summary.skipped) == (5, 3, 2)
    assert summary.blobs_reused == 1
    assert sorted(blobs(store)) == sorted(image_key(content_key(b)) for b in (RED, GREEN))
    rows = image_map.read_map(store)
    assert rows["attempts"].to_list() == [0, 0, 0] and rows["fetched_at"].null_count() == 0
    assert rows["status"].unique().to_list() == [image_map.OK]

    again = seed_from_shards(store, str(src / "part-*.parquet"), batch_rows=2)
    assert again.imported == 0 and again.already_mapped == 3

    catalogue = cat(origin, a="/red", b="/red2", e="/green")
    assert fetch_images(store, catalogue, cfg).pending == 0
    assert origin.hits == {}


def test_seed_max_rows_stops_early(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    rows = [
        (str(i), "tesco", "t", "t", f"http://x/{i}", picture("red", size=20 + i)) for i in range(6)
    ]
    shard(src / "part-00000.parquet", rows)
    summary = seed_from_shards(
        Storage(str(tmp_path / "s")), str(src / "p*.parquet"), batch_rows=2, max_rows=3
    )
    assert summary.rows_seen == 4 and summary.imported == 4


def test_load_catalogue_joins_image_sha256_for_image_specs(tmp_path, origin, cfg):
    store = Storage(str(tmp_path / "store"))
    products = pl.concat([cat(origin, a="/red", b="/blue"), cat(origin, c="/gone")]).with_columns(
        title_cleaned=pl.lit("milk")
    )
    products.write_parquet(tmp_path / "cat.parquet")
    fetch_images(store, load_image_catalogue(str(tmp_path / "cat.parquet")), cfg)

    spec = get_spec("gemma2-image-text-t140")
    loaded = load_catalogue(str(tmp_path / "cat.parquet"), spec, store).sort("id")
    assert loaded.columns == ["id", "supermarket", "title_cleaned", "image_sha256"]
    assert loaded["image_sha256"].to_list() == [content_key(RED), content_key(BLUE), None]


def test_embedder_sniffs_formats_from_extensionless_bytes():
    embedder = SentenceTransformerEmbedder(get_spec("gemma2-image"))
    for data in (RED, GREEN, picture("red", "WEBP")):
        assert embedder._payload(EmbedInput(image=data)).mode == "RGB"
    both = SentenceTransformerEmbedder(get_spec("gemma2-image-text"))._payload(
        EmbedInput(text="milk", image=RED)
    )
    assert both["text"].endswith("milk <|image|>") and both["image"][0].size == (32, 32)


def test_inspect_image_reports_sniffed_type_and_rejects_bad_payloads():
    jpeg = inspect_image(GREEN)
    assert (jpeg.content_type, jpeg.width, jpeg.height) == ("image/jpeg", 32, 32)
    for bad in (b"tiny", b"x" * 500, picture("red", size=4)):
        with pytest.raises(ValueError):
            inspect_image(bad)


def test_cli_exits_non_zero_when_the_failure_rate_is_over_the_threshold(
    tmp_path, origin, monkeypatch
):
    monkeypatch.setenv("IMAGE_DELAY", "0")
    cat(origin, a="/gone", b="/gone2", c="/red").write_parquet(tmp_path / "cat.parquet")
    argv = ["--store", str(tmp_path / "s"), "fetch", "--catalogue", str(tmp_path / "cat.parquet")]
    assert images_cli.main(argv) == 1
    assert images_cli.main(argv) == 0
    assert images_cli.main([*argv, "--retry-failed", "--limit", "1"]) == 1
