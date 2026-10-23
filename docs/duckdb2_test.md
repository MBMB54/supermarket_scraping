# DuckDB 2.0.0 upgrade test

**Recommendation: upgrade later (not now). Do it once two small code fixes are ready and dbt Fusion bundles DuckDB 2.x.**

Nothing was changed in the repo. Everything ran on copies in a scratch folder, with a separate virtual environment holding duckdb 2.0.0 (and 1.5.3 for comparison). Python 3.13 and 3.14 both worked.

## 1. Storage: safe, and not one-way by default

- DuckDB 2.0.0 opened a copy of `supermarket_data.db` read-only and read-write with no warnings. All tables, the FTS schema and the HNSW index were intact. No export/import or ATTACH step is needed.
- After a 2.0.0 read-write open, a write and a checkpoint, 1.5.3 still opened the file (read-only and read-write) and could read the table written by 2.0.0. The file stays in the old storage format (`v1.0.0+`).
- The one-way door is opt-in. A file created with `storage_version 'latest'` in 2.0.0 cannot be opened by 1.5.3 ("version number 999 ... created with a newer version"). Nothing does that unless you ask for it.
- Gotcha, which probably exists on 1.5.3 too (not tested): writing to this DB without `vss` loaded fails, because the file holds an HNSW index. Load `vss` and set `hnsw_enable_experimental_persistence` first.

## 2. Our workloads

**ml/agents.py: two things break**
1. `rel.fetchmany()` and `rel.fetchone()` no longer exist on relations (`con.sql(...)`). `agents.py` lines 196 and 312 call `rel.fetchmany(...)`, so `run_sql` and `hybrid_search` would crash with an AttributeError (not a duckdb error, so no retry). The fix works on both versions: `rel.limit(25).fetchall()` and `rel.limit(21).fetchall()`. The same two lines are in `notebooks/agents.ipynb`. `cursor().execute(...).fetchone()` (line 467) still works.
2. The FTS index still works, with identical BM25 scores, but is far slower. `match_bm25` over `int_supervalu` takes 0.25 to 0.8 s on 2.0.0, against about 6 ms on 1.5.3, so `hybrid_search` goes from about 25 ms to about 320 ms. Rebuilding the index on 2.0.0 (`pragma create_fts_index(..., overwrite=1)`) brought it back to about 10 ms. The fix is to rebuild the FTS indexes after upgrading. This was tested on a copy where I also dropped the HNSW index to avoid the bind error, so I did not check that the rebuilt copy is still readable by 1.5.3.

Other checks passed. The cosine similarity query against `FLOAT[640]` embeddings works, as do `.pl()` and ordinary aggregates. `vss` loads and the stored HNSW index is used. One SQL change: a column alias that reuses a function with side effects, in the same `WHERE`/`ORDER BY` as it is defined, now errors. Wrap it in a subquery. The `agents.py` CTEs are unaffected. `.df()` needs pandas in both versions.

**dbt Fusion 2.0.6 embeds its own DuckDB, v1.5.5**, shown by `select version()` through `dbt show`. It does not use the Python package. On the copies, `dbt show --inline "select count(*) from fct_price_history"` returned 197,703 rows against both the untouched 1.5.3 file and the file that 2.0.0 had opened read-write and checkpointed. It fails on a `storage_version 'latest'` file (error dbt1308). So Fusion keeps working as long as the file stays in the default format, and Python 2.0.0 and Fusion can share one DB. A newer Fusion (2.0.9) is available; I did not install it, and I do not know which DuckDB it bundles. The repo's `dbt-duckdb` group (>=1.4.3, not pinned below 2) was not tested because dbt-core does not import on 3.14.

## 3. What you gain

- **HF extension** (`INSTALL huggingface FROM community`): works on 2.0.0 with a DuckDB secret (`TYPE huggingface, token ...`). On the private `brianbarry97/irish_supermarket_data`, `hf_files` listed the Parquet shards, `hf_profile` reported 126,711 rows in 13 files, and `hf_scan` with LIMIT returned `id, title` in about 3 s without touching the image columns. `hf_dataset_estimate` failed with a binder error ("column images not found"), an extension bug for this schema.
- **Native `hf://buckets/`**: works. `glob` listed `brianbarry97/jobs-artifacts` (about 3 s), and `read_text` read a 2 KB file in 0.3 s. Writes are not supported (`COPY ... TO` failed with "DirectoryExists is not implemented"), so a bucket remains read-only for DuckDB. I confirmed no test file reached the bucket.
- **Performance**: no meaningful speed-up. Brute-force `array_cosine_distance` on 100k x 768 floats took about 0.075 s warm on both versions. HNSW build took about 30 s on both, and queries took 5 to 7 ms. Persisted HNSW (experimental flag) works on both: it built, checkpointed, reopened and was used. The 2.0.0 file was smaller (605 MB against 765 MB). Random Gaussian data gave poor recall on both versions, so that test says nothing about quality. Inserting from Arrow was faster on 2.0.0 (1.5 s against 3.4 s).

So the gain is mostly convenience: reading HF buckets and datasets from SQL. You can already do both from Python on 1.5.3.

## 4. Rollout path (when you do it)

1. Fix the two `fetchmany` calls in `ml/agents.py` and `notebooks/agents.ipynb` first. The change is compatible with 1.5.3.
2. Back up: `cp supermarket_data.db supermarket_data_pre2.db`.
3. Pin `duckdb==2.0.0` in the notebook/ml group only. Leave Fusion alone.
4. Rebuild the FTS indexes for `int_supervalu` and `tesco_table`, then re-time `hybrid_search`.
5. Never use `storage_version 'latest'` while Fusion embeds 1.x.
6. Rollback: reinstall 1.5.3, or restore the backup. Because the format is unchanged, plain downgrade should also work.

## Unverified

- Fusion running a full `dbt run` or `build` against a file 2.0.0 had written.
- Whether Fusion 2.0.9 bundles DuckDB 2.x.
- dbt-duckdb with duckdb 2.
- A 1.5.3 open of a 2.0.0-rebuilt FTS index.
- Whether the slow FTS is a 2.0.0 regression or a stale stored macro. Rebuilding fixes it either way.
- Note: `pyproject.toml` and `uv.lock` already showed as modified in git before my tests. I did not change them.
