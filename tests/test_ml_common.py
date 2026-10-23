import hashlib
from dataclasses import replace

import polars as pl
import pytest

from ml_common import table
from ml_common.catalogue import load_catalogue
from ml_common.diff import pending, with_content_keys
from ml_common.embedders import EmbedInput, EmbedResult
from ml_common.keys import content_key, image_key, input_key, stable_hash
from ml_common.pipeline import run_embed
from ml_common.specs import REGISTRY, EmbeddingSpec, get_spec
from ml_common.storage import Storage

TEXT = get_spec("gemma2-text")
IMAGE = get_spec("gemma2-image")
BOTH = get_spec("gemma2-image-text")
HARRIER = get_spec("harrier-title")


class FakeEmbedder:
    """Deterministic 4-d vectors; text 'FAIL' and image bytes b'bad' produce error results."""

    def __init__(self, fail_items: set | None = None):
        self.calls: list[EmbedInput] = []
        self.fail_items = fail_items if fail_items is not None else {"FAIL", b"bad"}

    def encode(self, inputs):
        self.calls.extend(inputs)
        results = []
        for item in inputs:
            if item.text in self.fail_items or item.image in self.fail_items:
                results.append(EmbedResult(None, "decode error"))
                continue
            digest = hashlib.sha256(f"{item.text}{item.image}".encode()).digest()
            results.append(EmbedResult([b / 255 for b in digest[:4]]))
        return results


def catalogue(**titles) -> pl.DataFrame:
    ids = list(titles) or ["a", "b", "c"]
    return pl.DataFrame(
        {
            "id": ids,
            "supermarket": ["tesco"] * len(ids),
            "title": [titles.get(i, f"title {i}") for i in ids],
            "title_cleaned": [titles.get(i, f"title {i}") for i in ids],
        }
    )


def test_content_key_is_sha256_of_text_and_bytes():
    assert content_key("milk") == hashlib.sha256(b"milk").hexdigest()
    assert content_key(b"milk") == content_key("milk")
    assert content_key("milk") != content_key("milk ")


def test_input_key_per_modality():
    assert input_key("text", "milk", None) == content_key("milk")
    assert input_key("image", None, "abc") == "abc"
    assert input_key("image_text", "milk", "abc") != input_key("image_text", "milk2", "abc")
    assert input_key("image_text", "milk", "abc") != input_key("image_text", "milk", "abd")


def test_image_key_is_sharded_by_prefix_without_extension():
    assert image_key("abcdef") == "images/ab/abcdef"


def test_stable_hash_ignores_field_order():
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})


def test_spec_id_is_stable_and_registry_ids_are_distinct():
    assert get_spec("gemma2-text").spec_id == get_spec("gemma2-text").spec_id
    assert len(REGISTRY) == 6
    assert len({s.spec_id for s in REGISTRY.values()}) == 6


def test_image_token_variants_differ_only_in_name_and_budget():
    for base in ("gemma2-image", "gemma2-image-text"):
        variant = get_spec(f"{base}-t140")
        assert variant.image_tokens == 140 and get_spec(base).image_tokens is None
        assert variant.spec_id != get_spec(base).spec_id
        assert replace(variant, name=base, image_tokens=None) == get_spec(base)


@pytest.mark.parametrize(
    "change",
    [
        {"revision": "0" * 40},
        {"prefix": "other: "},
        {"dim": 256},
        {"normalize": False},
        {"dtype": "bfloat16"},
        {"model_id": "other/model"},
        {"text_field": "title"},
    ],
)
def test_spec_id_changes_with_any_vector_affecting_field(change):
    assert replace(TEXT, **change).spec_id != TEXT.spec_id


def test_spec_id_ignores_operational_fields():
    assert replace(TEXT, name="renamed", batch_size=1, query_prefix="q").spec_id == TEXT.spec_id


def test_spec_rejects_fields_that_do_not_match_modality():
    with pytest.raises(ValueError):
        EmbeddingSpec(name="x", model_id="m", revision="r", modality="image", text_field="t")


def test_harrier_spec_keeps_incumbent_prompt():
    assert HARRIER.prefix.startswith("Instruct: Retrieve semantically similar supermarket")
    assert HARRIER.prefix.endswith("Query: ")
    assert HARRIER.dim is None and HARRIER.normalize


def test_with_content_keys_drops_rows_without_input():
    cat = pl.DataFrame(
        {"id": ["a", "b", "c"], "supermarket": ["t"] * 3, "title_cleaned": ["x", None, ""]}
    )
    assert with_content_keys(TEXT, cat)["id"].to_list() == ["a"]


def test_storage_round_trip_and_listing(tmp_path):
    store = Storage(str(tmp_path))
    df = pl.DataFrame({"id": ["a"], "v": [[1.0, 2.0]]})
    assert not store.exists("x/y.parquet") and store.list("x") == []
    store.write_parquet(df, "x/y.parquet")
    store.write_bytes("x/z.bin", b"hi")
    assert store.read_parquet("x/y.parquet").equals(df)
    assert store.read_parquet("x/y.parquet", columns=["id"]).columns == ["id"]
    assert store.read_bytes("x/z.bin") == b"hi"
    assert store.list("x") == ["x/y.parquet", "x/z.bin"]


def test_load_catalogue_reads_only_spec_columns(tmp_path):
    catalogue().with_columns(extra=pl.lit(1)).write_parquet(tmp_path / "cat.parquet")
    store = Storage(str(tmp_path / "store"))
    assert load_catalogue(str(tmp_path / "cat.parquet"), TEXT, store).columns == [
        "id",
        "supermarket",
        "title_cleaned",
    ]
    with pytest.raises(FileNotFoundError):
        load_catalogue(str(tmp_path / "missing.parquet"), TEXT, store)


def run(store, spec, cat, embedder=None, **kw):
    embedder = embedder or FakeEmbedder()
    return run_embed(spec, cat, Storage(str(store)), embedder, **kw), embedder


def test_first_run_embeds_everything_and_rerun_embeds_nothing(tmp_path):
    cat = catalogue()
    first, _ = run(tmp_path, TEXT, cat)
    again, embedder = run(tmp_path, TEXT, cat)
    assert (first.pending, first.embedded, first.failed) == (3, 3, 0)
    assert (again.pending, again.embedded) == (0, 0) and embedder.calls == []


def test_new_row_embeds_only_that_row(tmp_path):
    run(tmp_path, TEXT, catalogue())
    summary, embedder = run(
        tmp_path, TEXT, catalogue(a="title a", b="title b", c="title c", d="new")
    )
    assert summary.embedded == 1 and [c.text for c in embedder.calls] == ["new"]


def test_changed_title_reembeds_exactly_one_row(tmp_path):
    run(tmp_path, TEXT, catalogue())
    summary, embedder = run(tmp_path, TEXT, catalogue(a="title a", b="changed", c="title c"))
    assert summary.embedded == 1 and [c.text for c in embedder.calls] == ["changed"]
    store = Storage(str(tmp_path))
    full = table.read_table(store, TEXT)
    assert len(full) == 4
    current = table.latest_ok(full)
    assert len(current) == 3
    changed = current.filter(pl.col("id") == "b")
    assert changed["content_key"].to_list() == [content_key("changed")]


def test_spec_change_starts_a_separate_table(tmp_path):
    run(tmp_path, TEXT, catalogue())
    other = replace(TEXT, dim=256)
    summary, _ = run(tmp_path, other, catalogue())
    assert summary.embedded == 3
    assert {k.split("/")[1] for k in Storage(str(tmp_path)).list("embeddings")} == {
        TEXT.spec_id,
        other.spec_id,
    }


def test_failures_are_recorded_and_retried(tmp_path):
    cat = catalogue(a="FAIL", b="ok b")
    first, _ = run(tmp_path, TEXT, cat)
    assert (first.embedded, first.failed) == (1, 1)
    full = table.read_table(Storage(str(tmp_path)), TEXT)
    failed = full.filter(pl.col("status") == table.ERROR)
    assert failed["id"].to_list() == ["a"] and failed["embedding"].null_count() == 1
    assert failed["error"].to_list() == ["decode error"]

    retry, embedder = run(tmp_path, TEXT, cat, FakeEmbedder(fail_items=set()))
    assert retry.embedded == 1 and [c.text for c in embedder.calls] == ["FAIL"]
    assert table.latest_ok(table.read_table(Storage(str(tmp_path)), TEXT)).height == 2


def test_parts_are_append_only_and_chunked(tmp_path):
    store = Storage(str(tmp_path))
    run(tmp_path, TEXT, catalogue(), chunk_rows=2)
    parts = table.part_keys(store, TEXT)
    assert len(parts) == 2
    run(tmp_path, TEXT, catalogue(a="title a", b="title b", c="title c", d="new"))
    assert set(parts) < set(table.part_keys(store, TEXT))
    assert store.exists(f"{TEXT.table_prefix}/manifest.json")


def test_limit_caps_rows_per_run(tmp_path):
    summary, _ = run(tmp_path, TEXT, catalogue(), limit=2)
    assert (summary.pending, summary.embedded) == (3, 2)
    assert run(tmp_path, TEXT, catalogue())[0].embedded == 1


def test_clean_schema_and_dbt_schema(tmp_path):
    run(tmp_path, TEXT, catalogue())
    clean = table.read_table(Storage(str(tmp_path)), TEXT)
    assert clean.columns == [
        "id",
        "supermarket",
        "content_key",
        "spec_id",
        "model_id",
        "model_revision",
        "modality",
        "embedding",
        "embedded_at",
        "status",
        "error",
    ]
    assert clean.schema["embedding"] == pl.List(pl.Float32)

    run(tmp_path, HARRIER, catalogue())
    dbt = table.read_table(Storage(str(tmp_path)), HARRIER)
    assert dbt.columns[:6] == [
        "id",
        "supermarket",
        "title",
        "title_cleaned",
        "title_embedding",
        "embedding_model",
    ]
    assert dbt["embedding_model"].unique().to_list() == ["microsoft/harrier-oss-v1-270m"]


def test_image_specs_load_bytes_from_store_and_record_missing(tmp_path):
    store = Storage(str(tmp_path))
    good, bad, missing = b"png-good", b"bad", b"png-missing"
    for blob in (good, bad):
        store.write_bytes(image_key(content_key(blob)), blob)
    cat = catalogue().with_columns(
        image_sha256=pl.Series([content_key(good), content_key(bad), content_key(missing)])
    )
    summary, embedder = run(tmp_path, BOTH, cat, FakeEmbedder({b"bad", None}))
    assert (summary.embedded, summary.failed) == (1, 2)
    assert [c.image for c in embedder.calls] == [good, bad, None]
    assert all(c.text for c in embedder.calls)

    again, _ = run(tmp_path, BOTH, cat, FakeEmbedder({b"bad", None}))
    assert again.pending == 2


def test_pending_uses_only_ok_rows_of_matching_spec(tmp_path):
    cat = catalogue()
    done = pl.DataFrame(
        {"id": ["a"], "supermarket": ["tesco"], "content_key": [content_key("title a")]}
    )
    assert pending(TEXT, cat, done)["id"].to_list() == ["b", "c"]
