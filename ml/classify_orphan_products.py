"""Phase 3 of the SuperValu taxonomy consolidation.

Classifies every product sitting in a `dissolve` bucket of `dbt/seeds/supervalu_category_map.csv`
against the frozen 566-leaf taxonomy in `dbt/seeds/supervalu_taxonomy.csv`, and writes
`dbt/seeds/supervalu_product_overrides.csv` (id, taxonomy_1..3, source='llm', confidence,
reviewed_at='') plus a sidecar `ml/orphan_classifications.jsonl` with the model's reasoning for the
Phase 4 review UI.

- One `pydantic_ai` Agent over an OpenRouter model, structured output validated against the real
  taxonomy leaves (invalid paths are retried once, then left for human review).
- Products are batched by their source `dissolve` bucket so the taxonomy context and the bucket's
  hand-written routing `note` are shared across the batch.
- `storage_state` (Ambient/Chilled/Frozen) is passed as a tie-break and used for a post-hoc QA
  flag: an Ambient product placed in `Frozen Foods` (or vice versa) is marked `qa_flag=1`
  regardless of the model's confidence.

Run:  uv run --group agents python ml/classify_orphan_products.py
      uv run --group agents python ml/classify_orphan_products.py --limit 60   # smoke test
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import pathlib
from datetime import date  # noqa: F401

import duckdb
import logfire
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

load_dotenv()
logfire.configure(send_to_logfire="if-token-present", scrubbing=False)
logfire.instrument_pydantic_ai()

REPO = pathlib.Path(__file__).resolve().parent.parent
SEEDS = REPO / "dbt" / "seeds"
DB = REPO / "supermarket_data.db"
OUT_CSV = SEEDS / "supervalu_product_overrides.csv"
OUT_JSONL = REPO / "ml" / "orphan_classifications.jsonl"

MODEL_NAME = os.getenv("ORPHAN_MODEL", "openai/gpt-5.6-luna")
BATCH_SIZE = 12
CONCURRENCY = 8


# --------------------------------------------------------------------------- schema
class Classification(BaseModel):
    index: int = Field(description="the 0-based index of the product in the list you were given")
    reasoning: str = Field(description="one sentence: what the product is and why this leaf")
    product_type: str = Field(
        description="free-text product type, e.g. 'greek yogurt', 'rice cakes'"
    )
    leaf_id: int = Field(description="the [number] of the chosen taxonomy leaf, exactly as shown")
    confidence: float = Field(ge=0.0, le=1.0, description="calibrated 0-1; <0.5 = genuinely unsure")


class Batch(BaseModel):
    classifications: list[Classification]


# --------------------------------------------------------------------------- data
def load_taxonomy() -> tuple[
    dict[int, tuple[str, str, str]], dict[str, list[int]], dict[int, str], str
]:
    """Return (leaf_id -> tuple, department -> [leaf_id], leaf_id -> description, numbered name tree)."""
    rows = []
    with open(SEEDS / "supervalu_taxonomy.csv") as f:
        for r in csv.DictReader(f):
            rows.append(
                (
                    r["taxonomy_1"].strip(),
                    r["taxonomy_2"].strip(),
                    r["taxonomy_3"].strip(),
                    (r["description"] or "").strip(),
                )
            )
    rows.sort()
    id_to_leaf: dict[int, tuple[str, str, str]] = {}
    id_to_desc: dict[int, str] = {}
    dept_ids: dict[str, list[int]] = {}
    tree_lines: list[str] = []
    cur_dept = None
    for i, (t1, t2, t3, desc) in enumerate(rows):
        id_to_leaf[i] = (t1, t2, t3)
        id_to_desc[i] = desc
        dept_ids.setdefault(t1, []).append(i)
        if t1 != cur_dept:
            tree_lines.append(f"\n{t1}:")
            cur_dept = t1
        leaf = f"{t2} > {t3}" if t3 else t2
        tree_lines.append(f"  [{i}] {leaf}")
    return id_to_leaf, dept_ids, id_to_desc, "\n".join(tree_lines)


def load_dissolve_buckets() -> list[dict]:
    buckets = []
    with open(SEEDS / "supervalu_category_map.csv") as f:
        for r in csv.DictReader(f):
            if r["disposition"].strip() == "dissolve":
                buckets.append(r)
    return buckets


def fetch_products(buckets: list[dict], limit: int | None) -> list[dict]:
    con = duckdb.connect(str(DB), read_only=True)
    con.sql(
        "CREATE OR REPLACE SECRET secret (TYPE s3, PROVIDER credential_chain, REGION 'eu-west-1');"
    )
    # storage_state lives in staging (not yet in int_supervalu until Phase 5)
    try:
        con.sql("CREATE OR REPLACE TEMP VIEW ss AS SELECT id, storage_state FROM stg_supervalu")
        has_ss = True
    except Exception:
        has_ss = False

    rows: list[dict] = []
    for b in buckets:
        conds, params = [], []
        for col in ("category_1", "category_2", "category_3", "category_4"):
            v = b[col].strip()
            if v:
                conds.append(f"s.{col} = ?")
                params.append(v)
            else:
                conds.append(f"(s.{col} IS NULL OR s.{col} = '')")
        ss_sel = "ss.storage_state" if has_ss else "NULL AS storage_state"
        ss_join = "LEFT JOIN ss USING (id)" if has_ss else ""
        q = f"""
            SELECT s.id, s.title, s.brand, s.title_cleaned, s.item_description,
                   left(coalesce(s.ingredients, ''), 240) AS ingredients, {ss_sel}
            FROM int_supervalu s {ss_join}
            WHERE {" AND ".join(conds)}
        """
        for rec in con.execute(q, params).fetchall():
            rows.append(
                {
                    "id": rec[0],
                    "title": rec[1],
                    "brand": rec[2],
                    "title_cleaned": rec[3],
                    "item_description": (rec[4] or "")[:200],
                    "ingredients": rec[5],
                    "storage_state": rec[6],
                    "src_path": " > ".join(
                        x
                        for x in (
                            b["category_1"],
                            b["category_2"],
                            b["category_3"],
                            b["category_4"],
                        )
                        if x.strip()
                    ),
                    "note": b["note"].strip(),
                    "src_dept": b["category_1"].strip(),
                }
            )
    con.close()
    if limit:
        rows = rows[:limit]
    return rows


# --------------------------------------------------------------------------- prompt
DEPT_TOKENS = {  # words that appear in dissolve notes -> department name
    "Bakery": "Bakery",
    "Chilled Food": "Chilled Food",
    "Frozen": "Frozen Foods",
    "Food Cupboard": "Food Cupboard",
    "Drinks": "Drinks",
    "Meat": "Meat & Poultry",
    "Fish": "Fish & Seafood",
    "Cheese": "Cheese",
    "Milk": "Milk, Yogurt, Butter & Eggs",
    "Yogurt": "Milk, Yogurt, Butter & Eggs",
    "Wine": "Wine, Beer & Spirits",
    "Fruit & Vegetables": "Fruit & Vegetables",
    "Health & Wellness": "Health & Wellness",
}

INSTRUCTIONS = """\
You are a supermarket product classification expert working on the SuperValu (Ireland) catalogue.

Every product you are given was sitting in a category bucket that is being dissolved because its
name described a marketing tier, a dietary attribute, a packaging format, or was a mixed grab-bag.
Your job: assign each product to the ONE leaf of the new taxonomy that best answers "what is this
product?".

Rules:
- Pick the single best leaf from the taxonomy list and return its [number] as leaf_id. Only use a
  number that appears in the list.
- Classify by product identity, not by diet or brand positioning. A gluten-free penne is pasta
  (Food Cupboard > Rice, Pasta & Noodles > Pasta). A protein yogurt is yogurt.
- Respect storage state: a product marked storage_state=Frozen belongs in a "Frozen Foods" leaf; an
  Ambient product does not. Chilled products go to chilled leaves (Chilled Food, Cheese, Milk...).
- The source bucket and its routing note (written by a human reviewer) are strong hints — follow
  them unless the individual product clearly says otherwise.
- confidence: be honest. 0.9+ only when the title alone is unambiguous. <0.5 when you are guessing.
"""


def build_prompt(
    batch: list[dict],
    dept_ids: dict[str, list[int]],
    id_to_leaf: dict[int, tuple],
    id_to_desc: dict[int, str],
    name_tree: str,
) -> str:
    note = batch[0]["note"]
    dept = batch[0]["src_dept"]
    cand_depts = {dept} | {d for tok, d in DEPT_TOKENS.items() if tok in note}
    detail_lines = []
    for d in sorted(cand_depts):
        for lid in dept_ids.get(d, []):
            t = id_to_leaf[lid]
            leaf = f"{t[1]} > {t[2]}" if t[2] else t[1]
            desc = id_to_desc.get(lid, "")
            detail_lines.append(f"  [{lid}] {d} > {leaf}" + (f" — {desc}" if desc else ""))
    detail = "\n".join(detail_lines)
    lines = []
    for i, p in enumerate(batch):
        bits = [f"[{i}] {p['title']!r}"]
        if p["brand"]:
            bits.append(f"brand={p['brand']}")
        if p["storage_state"]:
            bits.append(f"storage_state={p['storage_state']}")
        if p["item_description"]:
            bits.append(f"desc={p['item_description']!r}")
        if p["ingredients"]:
            bits.append(f"ingredients={p['ingredients']!r}")
        lines.append("  " + "  ".join(bits))
    return f"""\
SOURCE BUCKET (being dissolved): {batch[0]["src_path"]}
HUMAN ROUTING NOTE: {note or "(none)"}

--- FULL TAXONOMY: every valid leaf as  [id] Aisle > Type  ---
{name_tree}

--- CANDIDATE DEPARTMENTS (same leaves, with descriptions — most products belong to one of these) ---
{detail}

--- PRODUCTS TO CLASSIFY ({len(batch)}) ---
{chr(10).join(lines)}

Return one classification per product. Set `index` to the product's [n], and `leaf_id` to the
[number] of the taxonomy leaf you chose."""


# --------------------------------------------------------------------------- run
async def classify_all(products, id_to_leaf, dept_ids, id_to_desc, name_tree) -> list[dict]:
    agent = Agent(
        OpenRouterModel(
            MODEL_NAME, provider=OpenRouterProvider(api_key=os.getenv("OPEN_ROUTER_API_KEY"))
        ),
        output_type=Batch,
        instructions=INSTRUCTIONS,
        retries=2,
    )
    # group by (src_path) so notes/context are shared, then chunk
    by_bucket: dict[str, list[dict]] = {}
    for p in products:
        by_bucket.setdefault(p["src_path"], []).append(p)
    batches: list[list[dict]] = []
    for group in by_bucket.values():
        for i in range(0, len(group), BATCH_SIZE):
            batches.append(group[i : i + BATCH_SIZE])

    done = load_done_ids()
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []
    lock = asyncio.Lock()

    async def run_batch(batch: list[dict], bi: int):
        if all(p["id"] in done for p in batch):
            return
        prompt = build_prompt(batch, dept_ids, id_to_leaf, id_to_desc, name_tree)
        async with sem:
            try:
                res = await agent.run(prompt)
            except Exception as e:  # noqa: BLE001
                print(f"  batch {bi}: FAILED {type(e).__name__}: {e}")
                return
        out = {c.index: c for c in res.output.classifications}
        recs = []
        for i, p in enumerate(batch):
            c = out.get(i)
            if c is None:
                continue
            t = id_to_leaf.get(c.leaf_id)
            leaf_ok = t is not None
            if not leaf_ok:
                t = ("", "", "")
            qa = 0
            if p["storage_state"] == "Frozen" and t[0] != "Frozen Foods":
                qa = 1
            if p["storage_state"] == "Ambient" and t[0] in ("Frozen Foods",):
                qa = 1
            recs.append(
                {
                    "id": p["id"],
                    "title": p["title"],
                    "brand": p["brand"],
                    "src_path": p["src_path"],
                    "storage_state": p["storage_state"],
                    "taxonomy_1": t[0],
                    "taxonomy_2": t[1],
                    "taxonomy_3": t[2],
                    "product_type": c.product_type,
                    "reasoning": c.reasoning,
                    "confidence": round(c.confidence, 2),
                    "leaf_valid": int(leaf_ok),
                    "qa_flag": qa,
                }
            )
        async with lock:
            with open(OUT_JSONL, "a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            results.extend(recs)
        print(f"  batch {bi}/{len(batches)}  ({batch[0]['src_path'][:50]})  +{len(recs)}")

    await asyncio.gather(*(run_batch(b, i) for i, b in enumerate(batches)))
    return results


def load_done_ids() -> set[str]:
    if not OUT_JSONL.exists():
        return set()
    ids = set()
    with open(OUT_JSONL) as f:
        for line in f:
            try:
                ids.add(json.loads(line)["id"])
            except Exception:  # noqa: BLE001, PERF203
                pass
    return ids


def write_override_csv():
    """Collapse the JSONL (last write per id wins) into the dbt seed."""
    best: dict[str, dict] = {}
    with open(OUT_JSONL) as f:
        for line in f:
            r = json.loads(line)
            best[r["id"]] = r
    n_valid = n_invalid = n_qa = 0
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["id", "taxonomy_1", "taxonomy_2", "taxonomy_3", "source", "confidence", "reviewed_at"]
        )
        for r in sorted(best.values(), key=lambda x: (x["src_path"], -x["confidence"])):
            if not r["leaf_valid"]:
                n_invalid += 1
                continue
            n_valid += 1
            n_qa += r["qa_flag"]
            w.writerow(
                [
                    r["id"],
                    r["taxonomy_1"],
                    r["taxonomy_2"],
                    r["taxonomy_3"],
                    "llm",
                    r["confidence"],
                    "",
                ]
            )
    print(
        f"\nwrote {OUT_CSV.name}: {n_valid} rows ({n_qa} storage-QA-flagged), "
        f"{n_invalid} dropped for invalid leaf (need manual assignment)"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fresh", action="store_true", help="ignore existing JSONL and re-run all")
    args = ap.parse_args()
    if args.fresh and OUT_JSONL.exists():
        OUT_JSONL.unlink()

    id_to_leaf, dept_ids, id_to_desc, name_tree = load_taxonomy()
    buckets = load_dissolve_buckets()
    products = fetch_products(buckets, args.limit)
    print(f"{len(products)} products in {len(buckets)} dissolve buckets  (model={MODEL_NAME})")

    asyncio.run(classify_all(products, id_to_leaf, dept_ids, id_to_desc, name_tree))
    write_override_csv()


if __name__ == "__main__":
    main()
