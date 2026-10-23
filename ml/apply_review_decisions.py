"""Phase 4 tail: fold the human review decisions from the `Reshelving` artifact back into
`dbt/seeds/supervalu_product_overrides.csv`.

The review console (`ml/review_console.html`, published as an Artifact with the `db` capability)
stores one document per department at `reviews/<dept-slug>`, each shaped:

    {"decisions": {"<product_id>": {"t1","t2","t3","status","ts"}}}

`status` is one of: accepted (LLM pick kept), corrected (moved), flagged (needs a second look).

Claude pulls those docs with the Artifact tool and writes them to a directory:

    Artifact action=read_db url=<artifact-url> db_op=list collection=reviews out_dir=ml/_review_dump

then this script merges them:

    uv run python ml/apply_review_decisions.py ml/_review_dump

Result: every reviewed product's row in the seed gets source=`human` and a real `reviewed_at`;
`corrected` rows get the new taxonomy path; `flagged` rows are written with source=`flagged` so a
dbt test / follow-up query can surface them. Un-reviewed products keep their `source=llm` row.
"""

from __future__ import annotations

import csv
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SEED = REPO / "dbt" / "seeds" / "supervalu_product_overrides.csv"


def main(dump_dir: str) -> None:
    d = pathlib.Path(dump_dir)
    decisions: dict[str, dict] = {}
    for f in sorted(d.rglob("*.json")):
        doc = json.loads(f.read_text())
        for pid, rec in (doc.get("decisions") or {}).items():
            decisions[str(pid)] = rec
    print(f"{len(decisions)} review decisions from {dump_dir}")

    rows = list(csv.DictReader(open(SEED)))
    by_id = {r["id"]: r for r in rows}
    n_acc = n_cor = n_flag = n_new = 0
    for pid, rec in decisions.items():
        status = rec.get("status", "accepted")
        row = by_id.get(pid)
        if row is None:  # product not in the LLM seed (shouldn't happen) — add it
            row = {"id": pid}
            rows.append(row)
            by_id[pid] = row
            n_new += 1
        row["taxonomy_1"] = rec["t1"]
        row["taxonomy_2"] = rec["t2"]
        row["taxonomy_3"] = rec.get("t3", "")
        row["source"] = "flagged" if status == "flagged" else "human"
        row["confidence"] = "1.0" if status != "flagged" else row.get("confidence", "")
        row["reviewed_at"] = rec.get("ts", "")
        if status == "flagged":
            n_flag += 1
        elif status == "corrected":
            n_cor += 1
        else:
            n_acc += 1

    with open(SEED, "w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "id",
                "taxonomy_1",
                "taxonomy_2",
                "taxonomy_3",
                "source",
                "confidence",
                "reviewed_at",
            ],
        )
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["taxonomy_1"], r["taxonomy_2"], r["taxonomy_3"])):
            w.writerow(r)

    total = len(rows)
    print(f"wrote {SEED.name}: {total} rows")
    print(
        f"  reviewed: {n_acc} accepted + {n_cor} corrected + {n_flag} flagged"
        f"{f' + {n_new} added' if n_new else ''}"
    )
    print(f"  still LLM-only (unreviewed): {total - n_acc - n_cor - n_flag - n_new}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: apply_review_decisions.py <dir-of-review-json-dumps>")
    main(sys.argv[1])
