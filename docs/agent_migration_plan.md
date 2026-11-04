# Migration plan: `ml/agents.py` onto the dbt search and embedding tables

Status: plan only, written 2026-11-04 against `ml/agents.py` as it is on `local_implementation` (626 lines, line numbers below refer to that version). Nothing in this document has been implemented. It is written for a fresh session with no context: read section 0 and 9 first, do the pre-flight, then follow section 8 step by step.

Verification notes come from a read-only inspection of `supermarket_data.db` (DuckDB 2.0.0, opened `read_only=True`) and from throwaway tests on an in-memory DuckDB 2.0.0 on 2026-11-04. Re-verify anything marked **[verify]** if the data has been rebuilt since.

---

## 0. Corrections to the original brief (what turned out to be different)

| Brief said | Reality on 2026-11-04 |
|---|---|
| New tables exist in the local DB | `dim_product_search`, the schema `fts_main_dim_product_search` and the macro `bm25_search` do **not** exist in `supermarket_data.db` yet. `emb_gemma2_text` (50,042 rows, `FLOAT[768]`), `fct_embeddings`, `dim_embedding_specs`, `fct_product_listings`, `fct_price_history` and `fct_product_price_daily` do. Pre-flight step P3 must build the search table and index. |
| `rel.fetchmany()`/`.fetchone()` break on 2.0 and must be replaced at ~196/~312 | Already fixed: line 196 is `rel.limit(25).fetchall()` and line 312 is `rel.limit(21).fetchall()`. The only remaining fetch is `.fetchone()` at line 467, called on `cursor().execute(...)` (a connection/cursor object, not a relation). Verified: `duckdb.connect().cursor().execute('select 1').fetchone()` returns `(1,)` on 2.0.0, and `con.sql(...)` relations have neither `fetchone` nor `fetchmany`. Rule for new code: never call fetch methods on a `.sql()` relation, use `.limit(n).fetchall()` or `execute(...).fetchall()`. |
| One legacy FTS schema | One remains, `fts_main_int_supervalu`. A stray `fts_main_tesco_table` (no backing table) and the empty `flock_config` schema were already dropped on 2026-11-04. |
| vss/HNSW may be needed | Not needed. A brute-force `array_cosine_similarity` scan over all 50,042 vectors in `emb_gemma2_text` took 0.14 s. Read-only reads also work without `vss` loaded even though `hnsw_idx` is in the file. The new agent loads neither `vss` nor creates secrets. |
| Allergen matching on "free text lists" | Locally the lists are element lists (`['Barley','Wheat']`, `['Milk']`), not one comma string (the dbt README example `['Milk, gluten']` is stale). Substring matching on `array_to_string(list, ',')` handles both shapes. |
| `coalesce(contains_allergens, [])` in the README example is a safe exclusion filter | It is **not**: it treats unknown as "none", so an unknown product passes a "no milk" filter. Tesco has 11,237 NULL allergen lists out of 20,020 products. See rule R2. |
| `uv run` just works | The `agents` dependency group (`pyproject.toml` lines 61-67) has no `sentence-transformers`. Run with `uv run --group agents --group ml-embed ...`. |

---

## 1. Goal and non-goals

**Goal.** Make the shopping agent answer over **all four retailers** (tesco, aldi, dunnes, supervalu) using the dbt-built tables, with correct, injection-safe, parameterised SQL, a symmetric query/document embedding setup, and no import-time side effects.

**In scope**
- Replace SuperValu-only `int_supervalu` / `int_product_title_embeddings` / `fts_main_int_supervalu` usage with `dim_product_search`, `bm25_search()`, `emb_gemma2_text`, `fct_product_listings`, `fct_price_history`.
- Hybrid search (BM25 + cosine) with dietary, allergen, price and retailer filters.
- Price history tool; category browse across retailers; link/image/price validation generalised to all retailers.
- Lazy encoder (gemma2-text via `ml_common`), env-var configuration, read-only single connection.
- Schema section of the system prompt generated from `information_schema`.
- Unit tests on a tiny fixture DuckDB with a fake encoder; a manual smoke list.

**Non-goals**
- No image or image+text embeddings (they live in a private HF bucket, not in dbt).
- No changes to dbt models, `ml_common`, scrapers or the Batch job (a missing column in `dim_product_search` is an open question for the user, not something to patch here).
- Do not edit `notebooks/` (including `notebooks/agents.ipynb`); do not drop DB objects (the user does, section 10).
- No new boolean attribute columns (AGENTS.md convention): minor characteristics (decaf, craft, protein) stay inferred from the title at search time.
- No HNSW/vss index, no query-time S3 reads.

---

## 2. Current state of `ml/agents.py` (what changes, by line)

| Lines | What | Problem | Fate |
|---|---|---|---|
| 1-19 | imports; `SentenceTransformer` imported at top | pulls torch at import | import encoder lazily inside a function |
| 21-24 | `load_dotenv()`, logfire config | fine | keep |
| 26 | `bi_encoder = SentenceTransformer("microsoft/harrier-oss-v1-270m", ...)` at import | downloads/loads a model at import; wrong model for the new vectors (gemma2-text, 768-d) | delete; replaced by `get_query_encoder()` |
| 28-29 | `open_router_key`, `TABLE_NAME = "int_supervalu"` | single table | key stays (`OPEN_ROUTER_API_KEY`); `TABLE_NAME` removed |
| 31-42 | URL/price regexes, all SuperValu-specific | | generalised (step 6) |
| 44-48 | absolute DB path; `CREATE SECRET ... credential_chain`; `install_extension("vss")` (network); `load_extension("vss")`; `hnsw_enable_experimental_persistence` | absolute path; network at import; S3 secret and vss unneeded | replaced by `get_connection()` |
| 51-53 | `Deps(conn)` | | keep, add nothing global |
| 56-67 | `get_schema(conn, table)` | works, one table | extended to many tables + column comments (step 5) |
| 70-77 | `_cell` single-line rendering | good; but a cell containing ` \| ` breaks the parser at 510-523 | keep, also replace ` \| ` inside cells |
| 80-103 | five `OpenRouterModel` constructions with hard-coded slugs | | keep; default model chosen via `AGENT_MODEL` env |
| 106-112 | `agent = Agent(mimo_model, ...)` | | default model from env |
| 115-180 | system prompt; line 177 schema; line 179 `date.today()` | stale date anchor (data ends 2026-10-29, today is later) | schema regenerated; prompt carries data date, not today |
| 183-204 | `run_sql` (already `rel.limit(25).fetchall()`) | prefix check only (`SELECT`/`WITH`); relies on read-only connection | keep, harden (step 4) |
| 207-252 | `get_product_details(ids)`: `FROM int_supervalu WHERE id IN ?`, builds SuperValu URL (227) and rewrites image to `cell/` (228-231) | single retailer; ids not unique across retailers | rewritten around `product_key` |
| 255-324 | `hybrid_search`: f-string SQL; `'{search_term}'` at 287 (SQL injection and apostrophe crash); `{search_term_embedding}` and weights interpolated (279, 305); `INNER JOIN emb` at 291-300 | injection, drops products without a vector, harrier vectors with no query prefix | replaced by `search_products` |
| 327-366 | `_SORT_COLUMNS`, `_category_conditions` (parameterised values, whitelisted identifiers) | `is_product_available = TRUE` and `price IS NOT NULL` hard-coded; `LOWER(title) LIKE '%'||LOWER(?)||'%'` treats `%`/`_` in terms as wildcards | ported to `dim_product_search`, `contains()` instead of LIKE |
| 369-487 | `search_category`; f-strings at 427-438 and 457-466 interpolate only whitelisted `sort_col`/`direction` and `where_clause` made of `?` placeholders (safe); `.fetchone()` at 467 | single retailer; compares `price_per_unit_normalised` across mixed `unit_normalised` | ported, unit-aware |
| 490-523 | `_authoritative_products` parses `get_product_details` tool-return text from `ctx.messages` | fragile text parse; SuperValu only | keep approach (conversation-scoped), generalise |
| 526-613 | `validate_product_links` | SuperValu host/URL only, filename-based image check | generalised (step 6) |
| 616-626 | module-level `deps = Deps(con)`, `app = agent.to_web(...)` | forces connection at import | deps built lazily; `app` exposure kept |

Facts re-verified in the brief: (1) true (absolute path, import-time loading, SuperValu only, f-string around the term, no query prefix; the old embeddings were made with the long harrier `Instruct:` prompt, `ml_common/specs.py` `HARRIER_PROMPT`, so queries and documents were asymmetric). The new spec is symmetric by construction, see section 4.

---

## 3. Data model the agent will use (verified locally)

All in the main schema of `supermarket_data.db` (dev target).

- `dim_product_search` (**to be built**, P3): one row per `(supermarket, id)` plus `product_key = supermarket || ':' || id`. Columns: `product_key, supermarket, id, title, title_cleaned, brand, is_own_brand, category_1..4, item_description, ingredients, contains_allergens VARCHAR[], may_contain_allergens VARCHAR[], dietary_flags VARCHAR[], is_vegan, is_vegetarian, is_gluten_free, is_organic, is_low_fat, is_kosher, is_halal, is_dairy_free, is_sugar_free, has_no_added_sugar, is_diabetic_friendly, price, was_price, is_discount, is_promotion, unit_price, price_per_unit_normalised, unit_normalised, image_url, is_product_available, scraped_date`. Source: `dbt/models/search/dim_product_search.sql`. It is the **current catalogue snapshot**: today `fct_product_listings` holds exactly one `scraped_date` (2026-10-29) with 4,586 aldi / 10,461 dunnes / 14,909 supervalu / 20,020 tesco rows; no duplicate `(supermarket, id)`; no NULL `price`.
- BM25: schema `fts_main_dim_product_search` plus table macro `bm25_search(query_text) -> (product_key, score)` (only matching rows; non-matching are dropped). Indexed fields: `title_cleaned, brand, category_1..4` (`dbt/macros/search_index.sql`). Ingredients are deliberately not indexed.
- `emb_gemma2_text`: `supermarket, id, vector_index INTEGER, embedding FLOAT[768], content_key, embedded_at`. 50,042 rows, all `vector_index = 0`, unit norm (mean `array_inner_product(e,e)` = 1.00000003), so cosine equals dot product. Coverage gap: 473 `fct_product_listings` rows have no embedding and 539 embeddings have no listing (stale products). **Never inner-join the vectors to the candidate set** (rule R3).
- `dim_embedding_specs`: `spec_id='268dad232ccc02bc'`, name `gemma2-text`, dim 768, n_items 50,042.
- `fct_price_history`: versions `(supermarket, id, title, brand, price, was_price, unit_price, unit_normalised, price_per_unit_normalised, is_discount, is_promotion, valid_from, valid_to, is_current, last_seen_date, is_active)`. `valid_to` is exclusive and NULL on the current version. Live price = `is_current AND is_active`; verified exactly one such row per product (0 duplicates). 201,461 versions, 50,966 live. History starts 2026-02-01 (162 distinct scrape days in `fct_product_price_daily`); a product's first version starts when it was first scraped, not necessarily when its price began.
- `fct_product_price_daily`: daily facts; `max(scraped_date)` per retailer is the **data date** (2026-10-29 for all four today).

Dietary/allergen data quality (counts from `fct_product_listings`):

| supermarket | rows | dietary flags | contains_allergens NULL | `[]` | non-empty |
|---|---|---|---|---|---|
| aldi | 4,586 | **all NULL** (is_gluten_free NULL for every row) | 1,691 | 2,381 | 514 |
| dunnes | 10,461 | filled | 5,165 | 588 | 4,708 |
| supervalu | 14,909 | filled (only retailer with dairy_free/sugar_free/no_added_sugar/diabetic) | 6,851 | 1,035 | 7,023 |
| tesco | 20,020 | filled | 11,237 | 7,734 | 1,049 |

Image hosts present in `image_url`: `digitalcontent.api.tesco.com`, `images.cdn.shop.supervalu.ie` (paths `zoom/`, `detail/`, and a few others), `images.cdn.dunnesstoresgrocery.com`, `dm.emea.cms.aldi.cx`; 83 rows have NULL image_url. No product-page URL field exists in `stg_*` (only image fields), so **only SuperValu has a known product URL pattern** (`https://shop.supervalu.ie/product/id-<id>`, agents.py:227).

---

## 4. Design rules (apply everywhere; each is testable)

**R1. No f-strings with values.** Every value (search text, term lists, prices, the query vector, weights, limits) is a DuckDB bound parameter. Use named `$name` parameters with a dict. DuckDB rejects a dict with keys the SQL does not use (`Parameter argument/count mismatch`, verified), so SQL builders return `(sql, params)` generated together, and a unit test asserts every `$name` in the SQL is in the dict and vice versa. The only interpolated text is from fixed whitelists (`ORDER BY` fragments, retailer names mapped through a dict). Verified on 2.0.0: `SELECT * FROM bm25_search(?)` and `bm25_search($q)` accept a bound parameter through the table macro, including an apostrophe (`"it's bread"`), and `x = ANY($list)` / `x IN $list` accept a bound list.

**R2. NULL means unknown, never false or none.**
- Positive dietary requirement ("vegan", "gluten free"): `p.is_vegan IS TRUE`. Consequence: **Aldi has no dietary flags, so any flag filter excludes all Aldi products.** The tool result must say so ("aldi excluded: no dietary data") and the system prompt must tell the model to mention it, and to fall back to `ingredients ILIKE` / title evidence for Aldi when the user wants Aldi included.
- Allergen **exclusion** ("no milk"): a product qualifies only if `contains_allergens IS NOT NULL` and `array_to_string(contains_allergens, ',') NOT ILIKE '%milk%'`. Products with NULL lists are not silently included; the tool offers `include_unknown_allergens: bool = False` and, when true, returns them with a column `allergens = 'unknown'`. Never use `coalesce(contains_allergens, [])` for exclusion (it passes unknown products; verified: `NOT (coalesce(array_to_string(NULL,','),'') ILIKE '%milk%')` is true).
- `may_contain_allergens` is a second, optional exclusion list with the same rule (`strict_may_contain: bool = False`).
- Open question Q1: does `[]` mean "declared none" or "not parsed"? Tesco has 7,734 `[]` vs 11,237 NULL. Until answered, treat `[]` as "none declared" but surface the caveat in the answer for allergy-critical queries.
- Lambdas: DuckDB 2.0 rejects `->` (`Deprecated lambda arrow`). Use `lambda x: ...` or, preferably, avoid lambdas (`array_to_string` + `ILIKE`) so SQL also runs on Fusion's 1.5.5.

**R3. Never drop products for lacking a vector; use a candidate union.** Candidate set = BM25 top `N_CAND` union cosine top `N_CAND`, both computed over the *filtered* product set, then fused. BM25-only reranking returns nothing for queries with no lexical overlap; inner-joining vectors (as lines 291-300 do) drops the 473 unembedded listings. Products with no vector can still match through BM25.

**R4. Fusion = weighted reciprocal-rank fusion, constants in code.** Chosen over a weighted score because BM25 scores are unbounded and query-dependent while cosine is in [-1,1]; per-query normalisation of either is fragile. RRF is scale-free, needs no calibration, and is what `hybrid_search` (lines 255-324) already does with weights 0.6/0.4 and k=60, so behaviour carries over. Constants (module level in `ml/agent_queries.py`): `RRF_K = 60`, `W_VECTOR = 0.6`, `W_BM25 = 0.4`, `N_CAND = 200`, `DEFAULT_LIMIT = 20`, `MAX_LIMIT = 50`. The tool still lets the model shift weights per call (exact brand names favour BM25) but clamps each to [0, 1]. Known weakness: RRF ignores magnitude (a perfect lexical hit and a weak one at the same rank score the same); acceptable, revisit only if the smoke tests show it.

**R5. Relative dates anchor on the data date, not `date.today()`.** The data ends 2026-10-29 while today differs (line 179 injects `date.today()`). The prompt states `Data is current as of <max scraped_date per retailer>`; "this month" / "last week" resolve against that date. A staleness warning appears in the prompt if the data date is more than 3 days behind today.

**R6. Prices and links come only from tools.** Keep the existing contract: `get_product_details` is the only source of price, `product_url`, `image_url`; the output validator rejects anything else.

**R7. Connection and encoder are lazy and process-singletons.** Nothing heavy at import.

---

## 5. New module layout

Create `ml/agent_queries.py` (pure functions, no pydantic_ai import, importable in tests without OpenRouter or a model): constants, `get_connection`, `get_query_encoder`, SQL builders, result formatting, schema rendering. `ml/agents.py` keeps the agent, tools (thin wrappers that call the builders and format), prompt and validator. Rationale: `agents.py` constructs `OpenRouterModel`s and `agent.to_web(...)` at import, which makes it unusable in unit tests.

`ml/` has no `__init__.py` (namespace package). `pyproject.toml` sets `pythonpath = ["."]`, so `from ml.agent_queries import ...` works in tests. If it does not, add an empty `ml/__init__.py` and note it.

Run with: `uv run --group agents --group ml-embed uvicorn ml.agents:app` or the existing launch method **[verify how the user launches it; `app = agent.to_web(...)` at line 617 suggests an ASGI server]**.

### 5.1 Configuration (env vars, no absolute paths)

| Env var | Default | Use |
|---|---|---|
| `SUPERMARKET_DB_PATH` | `<repo root>/supermarket_data.db` computed as `Path(__file__).resolve().parents[1] / "supermarket_data.db"` | DB file (local dbt build, or a published `search.duckdb`) |
| `OPEN_ROUTER_API_KEY` | none (required) | existing name, keep. Fail with a clear message at first model use, not a stack trace from deep inside pydantic_ai |
| `AGENT_MODEL` | `xiaomi/mimo-v2.5` (today's default, line 85) | OpenRouter slug of the default model; the model dict at 618-624 keeps its other entries |
| `AGENT_QUERY_SPEC` | `gemma2-text` | registry name of the query-encoder spec |
| `AGENT_EMBEDDING_TABLE` | `emb_gemma2_text` | fixed-size embeddings relation; validated against an allow-list `{"emb_gemma2_text"}` because it is interpolated as an identifier |
| `AGENT_DEVICE` | unset (auto: `ml_common.embedders.pick_device()`) | `cpu`/`mps` override |

`load_dotenv()` stays (line 21).

### 5.2 Query encoder (lazy, once, on first use)

```python
# ml/agent_queries.py (shape, not final code)
_lock = threading.Lock()
_encoder = None

def get_query_encoder():
    global _encoder
    with _lock:
        if _encoder is None:
            from ml_common.embedders import SentenceTransformerEmbedder   # imports torch lazily
            from ml_common.specs import get_spec
            spec = get_spec(os.getenv("AGENT_QUERY_SPEC", "gemma2-text"))
            _encoder = SentenceTransformerEmbedder(spec, device=os.getenv("AGENT_DEVICE"))
        return _encoder

def embed_query(conn, text: str) -> list[float]:
    enc = get_query_encoder()
    _assert_spec_matches_db(conn, enc.spec)       # once: spec_id == dim_embedding_specs row; len == dim
    with _lock:                                   # SentenceTransformer is not guaranteed thread-safe
        vec = enc.encode_queries([text])[0]       # numpy (n, 768) -> row; applies spec.query_prefix
    return [float(x) for x in vec]                # bind as $qvec::FLOAT[768]
```

Facts (verified in code):
- `SentenceTransformerEmbedder.encode_queries(queries)` (`ml_common/embedders.py:139`) calls `model.encode(queries, prompt=spec.query_prefix or None, normalize_embeddings=spec.normalize, truncate_dim=spec.dim, ...)` and returns a numpy array of shape `(n, 768)`. The model loads on first `.model` access (`embedders.py:62-81`), not in the constructor; the gemma2-text spec pins `google/embeddinggemma-2@914f7f89...`, `dtype float32`, `disabled_configs=("vision_config","audio_config")`, `query_prefix = "task: search result | query: "`, document prefix `"title: none | text: "`, `normalize=True`.
- Queries get the query prefix, documents were embedded with the document prefix: symmetric as the model intends. The old setup was asymmetric (no prefix on queries vs the long `Instruct:` prompt on documents).
- First-use check `_assert_spec_matches_db`: `SELECT spec_id, dim FROM dim_embedding_specs WHERE name = ?` must equal `(spec.spec_id, 768)`; `spec.spec_id` must be `268dad232ccc02bc`. If it differs the vectors in the DB were made by a different spec: raise a clear error (the search tool then degrades to BM25-only with a note, rule below).
- The model may be gated or need an HF token; pre-flight P6 downloads it. If loading fails at runtime, `search_products` degrades to **BM25-only**, and says so in its output ("semantic search unavailable: <reason>"), rather than crashing the conversation.
- Cache encoded queries with `functools.lru_cache(maxsize=256)` keyed on the text (the same query is often repeated by the model within a run).

### 5.3 Connection handling

```python
_conn = None
def get_connection():
    global _conn
    if _conn is None:
        path = os.getenv("SUPERMARKET_DB_PATH") or DEFAULT_DB_PATH
        _conn = duckdb.connect(path, read_only=True)
        _try_load(_conn, "fts")     # best effort, see below
    return _conn
```
- One connection per process; each tool call uses `conn.cursor()` (already the pattern at 194, 224, 310, 427) so concurrent tool threads do not share a cursor.
- `read_only=True` stays. It is not a nicety: writing to the file without `vss` loaded fails while `hnsw_idx` exists, and an agent must never write. If the DB file is write-locked by a notebook or dbt, `duckdb.connect(..., read_only=True)` fails with a lock error. Catch it and raise a message saying to close the other process or point `SUPERMARKET_DB_PATH` at a copy (`~/supermarket_backups/` holds dated copies).
- Extensions: reading an existing FTS index does **not** need `LOAD fts` (verified: `fts_main_int_supervalu.docs` was queryable in a fresh read-only connection with fts not loaded, because the FTS functions are plain macros stored in the catalog). Still attempt `LOAD fts` inside `try/except` (extension cached in `~/.duckdb/extensions/v2.0.0/`; the dbt README says to `LOAD fts` first in a fresh connection) and do **not** call `INSTALL` (network). Do not load `vss`, do not `CREATE SECRET`, do not set `hnsw_enable_experimental_persistence` (lines 45-48 are all deleted).
- Optional hardening after setup (verify it does not break `bm25_search` first): `SET enable_external_access = false` so `run_sql` cannot read local files or S3 via `read_csv`/`read_parquet`.
- DuckDB version caveat: the project env is locked to DuckDB 2.0 (`duckdb>=2.0.0`), local dbt (Fusion) bundles 1.5.5. A file written by 1.5.5 is readable by 2.0. An FTS index built on 1.5.5 was 40-100x slower on 2.0 until rebuilt, so the index the agent queries must be (re)built by 2.0, see P4. The agent can only check this indirectly, so `get_connection()` runs one timed `SELECT count(*) FROM bm25_search('milk')` on first use and logs a warning if it takes more than 2 s (**[verify threshold]**; expected well under 0.5 s on 2.0 with a fresh index at ~50k rows).
- Data-date probe, once per process: `SELECT supermarket, max(scraped_date) FROM fct_product_price_daily GROUP BY 1`, kept in `Deps` for the prompt.

---

## 6. Tool-by-tool mapping, with SQL

`dim_product_search` is aliased `p` throughout. `$name` are bound parameters. Filter fragments come from `build_filters(...)` which returns `(where_sql, params)`; the SQL below shows the complete text for the default (no optional filter) case and then the optional fragments.

### 6.1 `hybrid_search` -> `search_products` (all retailers)

Signature (docstring should tell the model about NULL semantics and Aldi):

```python
search_products(
    query: str,
    supermarkets: list[str] | None = None,        # subset of the four; None = all
    max_price: float | None = None, min_price: float | None = None,
    vegan: bool = False, vegetarian: bool = False, gluten_free: bool = False,
    dairy_free: bool = False, organic: bool = False, low_fat: bool = False,
    sugar_free: bool = False, no_added_sugar: bool = False, diabetic_friendly: bool = False,
    kosher: bool = False, halal: bool = False,
    exclude_allergens: list[str] | None = None,    # e.g. ["milk", "nuts"]
    include_unknown_allergens: bool = False,
    on_promotion: bool = False,
    limit: int = 20, embed_weight: float = W_VECTOR, fts_weight: float = W_BM25,
) -> str
```
Flag arguments map to a fixed whitelist `{"vegan": "p.is_vegan", ...}`; each `True` argument adds `AND p.<col> IS TRUE` (identifier from the whitelist, never from the model's text).

Complete query (hybrid; `$qvec` is the 768-float list):

```sql
WITH filtered AS (
    SELECT p.product_key, p.supermarket, p.id
    FROM dim_product_search AS p
    WHERE p.is_product_available IS TRUE
      AND p.price IS NOT NULL
      /* optional fragments appended here, each with its own named params, e.g.
         AND p.supermarket = ANY($supermarkets)
         AND p.price <= $max_price
         AND p.is_gluten_free IS TRUE
         AND on_promotion:  AND p.is_promotion IS TRUE
         AND NOT ($ex0 allergen): p.contains_allergens IS NOT NULL
                 AND array_to_string(p.contains_allergens, ',') NOT ILIKE '%' || $ex0 || '%'
         (include_unknown_allergens=True instead emits:
            (p.contains_allergens IS NULL OR array_to_string(...) NOT ILIKE ...)) */
),
bm AS (
    SELECT product_key, rnk FROM (
        SELECT f.product_key,
               row_number() OVER (ORDER BY b.score DESC, f.product_key) AS rnk
        FROM bm25_search($q) AS b
        JOIN filtered AS f USING (product_key)
    ) WHERE rnk <= $n_cand
),
vec AS (
    SELECT product_key, rnk, sim FROM (
        SELECT f.product_key,
               array_cosine_similarity(e.embedding, $qvec::FLOAT[768]) AS sim,
               row_number() OVER (
                   ORDER BY array_cosine_similarity(e.embedding, $qvec::FLOAT[768]) DESC,
                            f.product_key) AS rnk
        FROM emb_gemma2_text AS e
        JOIN filtered AS f ON f.supermarket = e.supermarket AND f.id = e.id
        WHERE e.vector_index = 0
    ) WHERE rnk <= $n_cand
),
fused AS (
    SELECT product_key,
           bm.rnk AS bm25_rank, vec.rnk AS vector_rank, vec.sim,
           coalesce($w_bm25 / ($rrf_k + bm.rnk), 0)
         + coalesce($w_vec  / ($rrf_k + vec.rnk), 0) AS rrf
    FROM bm FULL OUTER JOIN vec USING (product_key)
)
SELECT p.product_key, p.supermarket, p.title, p.brand,
       round(p.price, 2) AS price, round(p.was_price, 2) AS was_price, p.is_promotion,
       round(p.price_per_unit_normalised, 2) AS price_per_unit, p.unit_normalised,
       p.category_1, p.category_2, p.category_3, p.category_4,
       CASE WHEN p.contains_allergens IS NULL THEN 'unknown'
            ELSE array_to_string(p.contains_allergens, ', ') END AS contains_allergens,
       round(fused.rrf, 4) AS score
FROM fused JOIN dim_product_search AS p USING (product_key)
ORDER BY fused.rrf DESC, p.product_key
LIMIT $lim
```
Params: `{"q": query, "qvec": vec, "n_cand": N_CAND, "rrf_k": RRF_K, "w_bm25": fts_weight, "w_vec": embed_weight, "lim": min(limit, MAX_LIMIT), ...optional}`.

Notes
- Filters are applied *before* both candidate generators (inside `filtered`), so a restrictive filter such as "under 4 euro, gluten free" cannot be starved by an unfiltered top-N. BM25 `bm25_search` returns all matches by design, so the filter-after-match advice in the dbt README is satisfied by joining to `filtered`.
- BM25-only fallback (encoder unavailable): same query with the `vec` CTE and `$qvec` removed and `W_VEC` ignored; the SQL builder takes `use_vectors: bool`.
- Empty or stop-word-only query: `bm25_search` returns no rows; the vector branch still returns results. An empty `query` string raises `ModelRetry`.
- Brute force over the filtered embeddings is ~0.14 s unfiltered; no index needed.
- Output ends with the existing reminder to call `get_product_details` for any product shown (lines 320-324), plus: "aldi has no dietary flags" when a dietary filter was used and aldi was not explicitly excluded, plus the count of results with unknown allergens when `exclude_allergens` was used.

### 6.2 `get_product_details` (lines 207-252) -> by `product_key`

```sql
SELECT p.product_key, p.supermarket, p.id, p.title, p.brand, p.item_description, p.ingredients,
       p.category_1, p.category_2, p.category_3, p.category_4,
       round(p.price, 2) AS price, round(p.was_price, 2) AS was_price,
       round(p.unit_price, 2) AS unit_price,
       round(p.price_per_unit_normalised, 2) AS price_per_unit, p.unit_normalised,
       p.is_promotion, p.is_discount,
       p.is_vegan, p.is_vegetarian, p.is_gluten_free, p.is_dairy_free, p.is_organic,
       CASE WHEN p.contains_allergens IS NULL THEN 'unknown'
            ELSE array_to_string(p.contains_allergens, ', ') END AS contains_allergens,
       CASE WHEN p.may_contain_allergens IS NULL THEN 'unknown'
            ELSE array_to_string(p.may_contain_allergens, ', ') END AS may_contain_allergens,
       p.scraped_date,
       coalesce(p.image_url, 'no_image') AS image_url
FROM dim_product_search AS p
WHERE list_contains($keys, p.product_key)      -- or p.product_key = ANY($keys)
```
- `product_url` is computed in Python (not SQL) from a constant dict `PRODUCT_URL_TEMPLATES = {"supervalu": "https://shop.supervalu.ie/product/id-{id}"}`; retailers without a verified template get `product_url = 'no_link'`. Do not invent Tesco/Aldi/Dunnes patterns (open question Q2). Add the column to the returned table so the parser in `_authoritative_products` keeps working.
- Flags print as `true/false/unknown` (map NULL to `unknown` explicitly in Python; the model otherwise reads `None` as false).
- Cap: at most 20 keys per call (`ModelRetry` otherwise).
- Image URL: the old code rewrote to `https://images.cdn.shop.supervalu.ie/cell/<filename>` (228-231). The stored SuperValu URLs are `zoom/<id>_<n>` or `detail/<id>_<n>` and are verified-scraped URLs. Plan: return the **stored URL verbatim** (no rewrite); the `cell/` size variant is unverified against the current data. Open question Q3 if the user wants thumbnails.

### 6.3 `search_category` (lines 369-487) -> cross-retailer, unit-aware

Category values are per-retailer taxonomies (SuperValu's is the consolidated tree; Tesco/Dunnes/Aldi differ), so the tool gains an optional `supermarkets` filter, and a helper tool `list_categories` lets the model discover valid values (the old one relied on values from a prior hybrid_search call).

```sql
-- search_category
SELECT p.product_key, p.supermarket, p.title, p.brand, p.category_1, p.category_2, p.category_3, p.category_4,
       round(p.price, 2) AS price,
       round(p.price_per_unit_normalised, 2) AS price_per_unit, p.unit_normalised
FROM dim_product_search AS p
WHERE p.is_product_available IS TRUE AND p.price IS NOT NULL
  /* optional, each with named params:
     AND p.supermarket = ANY($supermarkets)
     AND p.category_1 = $c1 ... AND p.category_4 = $c4
     AND p.unit_normalised = $unit              -- required when sorting by price_per_unit
     AND contains(lower(p.title), lower($inc0)) -- one per include term, ANDed
     AND NOT (contains(lower(p.title), lower($exc0)) OR ...)
     + the same dietary/allergen fragments as 6.1 */
ORDER BY <whitelisted fragment> NULLS LAST, p.product_key
LIMIT $lim
```
- `ORDER BY` fragments from a dict: `{"price": "p.price", "price_per_unit": "p.price_per_unit_normalised", "unit_price": "p.unit_price"}` x `ASC|DESC`; unknown `sort_by` -> `ModelRetry` (as 415-416).
- `contains(lower(title), lower($t))` replaces `LOWER(title) LIKE '%'||LOWER(?)||'%'` (356, 361) so a `%` or `_` in a term is literal.
- **Unit awareness (new):** `price_per_unit_normalised` is only comparable within one `unit_normalised` (kg, l, each...). When sorting by price per unit, the tool first computes the dominant `unit_normalised` among matches (`SELECT unit_normalised, count(*) ... GROUP BY 1 ORDER BY 2 DESC`) and either filters to it (parameter `unit` overrides) or returns a note listing the excluded units and counts. **[verify the actual `unit_normalised` value set with `SELECT unit_normalised, count(*) FROM dim_product_search GROUP BY 1` before coding.]**
- The "broaden" hint (447-478): keep it. The second query becomes the same builder with `category_3/4` dropped, `LIMIT 1`, executed with `cursor().execute(sql, params).fetchall()` and the first row taken (replaces `.fetchone()` at 467; either works on a cursor, but standardise).
- New helper `list_categories(supermarket: str | None, category_1: str | None, category_2: str | None)`:
  ```sql
  SELECT p.supermarket, p.category_1, p.category_2, p.category_3, count(*) AS n
  FROM dim_product_search AS p
  WHERE p.is_product_available IS TRUE
    /* AND p.supermarket = $s  AND p.category_1 = $c1  AND p.category_2 = $c2 */
  GROUP BY ALL ORDER BY n DESC LIMIT 60
  ```

### 6.4 New `price_history`

```sql
SELECT h.supermarket, h.id, h.title, h.price, h.was_price, h.unit_price,
       h.valid_from, h.valid_to, h.is_current, h.is_active, h.last_seen_date
FROM fct_price_history AS h
WHERE h.supermarket = $supermarket AND h.id = $id
  AND (h.valid_from >= $since OR h.valid_to IS NULL OR h.valid_to > $since)
ORDER BY h.valid_from
```
- Input is `product_key`; split on the first `:` in Python into `(supermarket, id)` (validate it is one of the four retailers).
- `$since` defaults to `data_date - 31 days` and is computed in Python from the data date (R5). The predicate returns the version in effect at `$since` plus every later version. `valid_to` is exclusive, `NULL` on the current version. The tool's text output derives "changed N times since X; was €a from d1, €b from d2" so the model does not re-derive it.
- "Live current price" semantics: `is_current AND is_active`. If the row is `is_current` but not `is_active` the product was not seen recently (delisted or fetch gaps within `active_grace_days`); the tool says "not currently listed (last seen <last_seen_date>)" rather than quoting a price as current.
- Caveat to state in the docstring: the first version starts at the first scrape, not at the price's real start; a product first seen this month shows one version only.
- Cross-product variant for questions like "what got cheaper this week": do not add a new tool; the model uses `run_sql` with `fct_price_history` (the prompt gives the semantics).

### 6.5 `run_sql` (lines 183-204)

Keep, plus: strip a trailing `;`, reject any `;` inside (single statement), keep the `SELECT`/`WITH` prefix check, keep the 25-row cap (already `rel.limit(25).fetchall()`). The read-only connection is the real guard. Allowed tables are those listed in the prompt schema (not enforced).

### 6.6 `validate_product_links` / `_authoritative_products` (lines 490-613)

Generalise, keep the mechanism (rebuild authoritative `product_url`/`image_url`/`price` per product from `get_product_details` tool returns in `ctx.messages`, 500-523):
- Key authoritative rows by `product_key` and index them by `product_url` and by `image_url`.
- Known retailer product hosts come from `PRODUCT_URL_TEMPLATES` (today only supervalu.ie). A URL on a known product host must equal a returned `product_url` exactly (as `_CANONICAL_PRODUCT_RE`, 34, 566-575 does).
- Image allow-list built from the four verified hosts: `digitalcontent.api.tesco.com`, `images.cdn.shop.supervalu.ie`, `images.cdn.dunnesstoresgrocery.com`, `dm.emea.cms.aldi.cx`. An image URL on these hosts must equal a returned `image_url` **in full** (the old check compared only the filename, 583-594, which cannot work for Tesco/Aldi URLs whose filename is a UUID or empty).
- Price check (596-611): match `**[title](url)** — €price` as today; the lookup becomes `url -> price` for products with a link, and for products without a template (no link) the pattern `**title** — €price` is checked against the title in the returned rows (add a second regex).
- Placeholder check (537-542) stays. Non-retailer URLs (web-search recipe links) stay untouched.
- Robustness: `_cell` also replaces ` | ` inside values so the ` | ` split parser (511-513) cannot mis-split a title containing a pipe.

---

## 7. System prompt changes (lines 115-180)

1. **Schema section generated, not hard-coded.** Replace `get_schema(ctx.deps.conn, TABLE_NAME)` (177) with `render_schema(conn, ["dim_product_search", "fct_price_history"])`: for each table, `SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position` plus the column comment if present (`SELECT column_name, comment FROM duckdb_columns() WHERE table_name = ?`; dbt may or may not persist docs into comments **[verify]**; render the comment only when non-NULL). Render once per process (cache), not per request. Do not include `emb_*`, `fct_embeddings` or index schemas: the model should never write vector SQL; it uses `search_products`.
2. Replace "You are a data analyst ... `{TABLE_NAME}` table" with a description of the tables: `dim_product_search` = current catalogue snapshot, one row per retailer product, `product_key = '<supermarket>:<id>'`; `fct_price_history` = versioned prices.
3. Replace `date.today()` (179) with: `Data is current as of: tesco 2026-10-29, aldi ..., ...` built from the data-date probe; "resolve 'today', 'this week', 'this month' against that date". Add a stale-data warning line when applicable.
4. Add the NULL rules: dietary flag NULL = unknown not false; Aldi has no dietary flags and ingredients for roughly a quarter of products; allergen NULL = unknown; for an allergy exclusion say that unknown products were excluded or flagged; never claim a product is "free from" something unless a flag or list says so.
5. Replace hybrid_search references (123-136) with `search_products`; keep the "cheapest" guidance but point to `search_category` (unit-aware); mention `list_categories`, `price_history`.
6. Presenting products block (155-173): `product_url` may be `no_link` (then show title and price without a link); stop saying "supervalu.ie" only; images shown when `image_url != 'no_image'`.
7. Multi-retailer wording: when comparing across shops, always show the retailer name next to each price.
8. Keep the paragraphs on not excluding products by assumption (138-147) and per-word title filters in `run_sql` (149-153), now with `contains(lower(title), ...)` or `ILIKE`.

---

## 8. Implementation order (each step leaves the agent runnable)

Run tests as `uv run --group test --group agents --group ml-embed pytest tests/test_agent_queries.py`. Do not run ruff/format unless the user asks (repo convention is ruff, line length 100; run it on the files you changed at the very end only if instructed).

**Step 0 - pre-flight (section 9).** Gate for everything else.

**Step 1 - hygiene only, old tables still used.** In `agents.py`: delete lines 44-48 (replace with lazy `get_connection()` in new `ml/agent_queries.py`), env-var DB path, lazy `deps`, move `bi_encoder` (still harrier, still for the old `hybrid_search`) behind a lazy accessor. No behavioural change. Acceptance: `python -c "import ml.agents"` (with `OPEN_ROUTER_API_KEY=dummy`) completes with no model download and no DB open (check with `strace`-free proxy: set `SUPERMARKET_DB_PATH=/nonexistent` and import still succeeds); the web app still answers a SuperValu question. Because the DROPs have not happened yet, the old tables still exist.

**Step 2 - `ml/agent_queries.py` + fixture + tests (no agent change).** Implement constants, `build_filters`, `build_hybrid_query`, `build_category_query`, `build_history_query`, `embed_query`/`get_query_encoder` (injectable encoder for tests), `render_schema`, `PRODUCT_URL_TEMPLATES`. Create `tests/test_agent_queries.py` with a fixture builder (see 8.1). Acceptance: all tests in 8.2 pass offline.

**Step 3 - add `search_products` next to the old `hybrid_search`.** Register the new tool; old tools still registered. Update the prompt to prefer `search_products`. Acceptance: smoke queries 1-4 (section 8.3) return multi-retailer rows; `hybrid_search` still works until step 7.

**Step 4 - port `get_product_details`, `search_category`, `run_sql` hardening, add `price_history` and `list_categories`.** Cut over by `product_key`. Remove `TABLE_NAME`. Acceptance: unit tests for each builder; smoke queries 5-8.

**Step 5 - generated schema in the prompt + data-date probe.** Acceptance: a test that `render_schema` on the fixture lists every column of `dim_product_search`; the prompt text contains the data date and no `int_supervalu`.

**Step 6 - generalise the output validator.** Acceptance: validator unit tests (8.2) including the Tesco-UUID image case and the unlinked-retailer price case.

**Step 7 - delete leftovers.** Remove old `hybrid_search`, `bi_encoder`, `int_supervalu` references, SuperValu-only regexes, unused imports. Acceptance: `grep -nE "int_supervalu|int_product_title_embeddings|fts_main_int_supervalu|harrier|bi_encoder|TABLE_NAME|date.today" ml/agents.py ml/agent_queries.py` returns nothing (apart from comments you deliberately kept). Then run the full smoke list. Then hand section 10 to the user.

### 8.1 Test fixture (tiny DuckDB in `tests/`, no network, no model)

`tests/test_agent_queries.py` builds an in-memory (or `tmp_path`) DuckDB 2.0 database:
- `INSTALL fts; LOAD fts` (skip the whole module with `pytest.importorskip`-style guard if the fts extension is not cached and the machine is offline: `pytest.skip` on `duckdb.IOException`).
- Table `dim_product_search` with the real column names/types from section 3, ~12 hand-written rows across all four retailers, covering: a gluten-free bread (flag true), a bread with `is_gluten_free` false, an Aldi bread with all flags NULL, a Tesco milk with `contains_allergens = ['Milk']`, a Tesco item with NULL allergens, a product with an apostrophe in the title (`Kendamil Goat's First Infant Milk`), a product with no embedding, an unavailable product (`is_product_available = FALSE`), same title at two retailers.
- `PRAGMA create_fts_index('dim_product_search','product_key','title_cleaned','brand','category_1','category_2','category_3','category_4', overwrite=1)` and the macro `bm25_search` created **by reading the text of `dbt/macros/search_index.sql`** is brittle (Jinja); instead copy the macro body verbatim into the fixture and add a test that the column list in the fixture PRAGMA equals the column list parsed from `dbt/macros/search_index.sql` (regex on the quoted names after `product_key'`), so the fixture cannot silently drift from dbt.
- `emb_gemma2_text(supermarket, id, vector_index, embedding FLOAT[768], content_key, embedded_at)`: tiny vectors would not match `FLOAT[768]`, so the fixture builds 768-d unit vectors from a deterministic function of a "topic" (e.g. one-hot in the first 3 slots plus zeros), and `FakeEncoder.encode_queries([text])` returns the vector of the topic keyed by substring in the query (`"bread"` -> topic A, `"milk"` -> topic B). One product deliberately has no row (coverage gap).
- `dim_embedding_specs(spec_id, name, dim)` with the real spec id `268dad232ccc02bc`, so `_assert_spec_matches_db` is tested, plus a mismatch case.
- `fct_price_history` and `fct_product_price_daily` with one product having three versions and one `is_current AND NOT is_active` product.

### 8.2 Unit tests (all offline, fake encoder)

1. `test_params_bound_not_interpolated`: the search term `x'; DROP TABLE dim_product_search; --` and `Goat's` run without error and the table still exists; the generated SQL string does not contain the term.
2. `test_named_params_match`: for every builder and every combination of optional filters, the set of `$names` in the SQL equals the keys of the params dict.
3. `test_bm25_macro_accepts_bound_param`: `SELECT * FROM bm25_search($q)` returns the expected `product_key`s.
4. `test_hybrid_includes_unembedded_product`: the product without a vector appears via BM25; a query with no lexical overlap (`"loaf"` vs title "bread") still returns vector-only matches; `bm25_rank`/`vector_rank` NULLs are handled.
5. `test_filters_applied_before_candidates`: with `N_CAND` monkeypatched to 1, a gluten-free filter still returns the gluten-free bread (it would be lost if the filter ran after top-1).
6. `test_dietary_null_is_unknown`: `gluten_free=True` excludes the Aldi row (all NULL) and the false row; the tool text mentions aldi has no dietary data.
7. `test_allergen_exclusion_excludes_unknown`: `exclude_allergens=["milk"]` removes the milk product and the NULL-allergen product by default; with `include_unknown_allergens=True` the NULL product returns labelled `unknown`; matching is case-insensitive and substring (`"nut"` matches `Peanuts` if present).
8. `test_unavailable_and_null_price_excluded`.
9. `test_rrf_weights_clamped`: weights outside [0,1] clamp; with `embed_weight=0` ordering equals BM25 order.
10. `test_category_sort_whitelist`: bad `sort_by` raises `ModelRetry`; `%` in an include term is literal (a title containing `100%` matches only `100%`).
11. `test_price_per_unit_not_mixed`: kg and l products are not interleaved in a price-per-unit sort.
12. `test_price_history_semantics`: version in effect at `since` plus later versions returned; `valid_to` exclusive; inactive current version reports "not currently listed".
13. `test_data_date_probe`: returns per-retailer `max(scraped_date)`; relative-date helper uses it, not `date.today()` (monkeypatch `date.today`).
14. `test_spec_check`: matching spec passes; mismatching `spec_id` raises and `search_products` falls back to BM25-only with the notice.
15. `test_render_schema`: every column of the fixture `dim_product_search` appears; no `emb_` table appears.
16. `test_validator_*`: accepts a SuperValu link/price/image returned by `get_product_details`; rejects an invented supervalu.ie URL; rejects an image URL not returned (full-URL comparison, Tesco UUID path); rejects a wrong price; ignores a recipe link on another host; handles a retailer with `no_link`.
17. `test_import_has_no_side_effects`: importing `ml.agent_queries` does not import `torch` or `sentence_transformers` (`'torch' not in sys.modules` in a subprocess) and does not open a DB.

### 8.3 Manual smoke list (real DB, real encoder, real model; ~$0.01 each)

A good answer is: correct products, each with retailer, link only where a template exists, price equal to `get_product_details`, honest handling of unknowns.

1. "gluten free bread under 4 euro": only `is_gluten_free IS TRUE` items priced <= 4.00 from tesco/dunnes/supervalu; states that Aldi has no dietary data; 3-5 products with prices, none above 4.
2. "vegan sausages": vegan flag true, titles are sausages (not supplements; ingredients are not indexed); retailers named.
3. "kendamil first infant milk": exact brand match at the top (BM25 side), only products whose title contains Kendamil, retailer prices side by side.
4. "cheapest 2 litre milk across shops": uses `search_category`/price-per-unit within one `unit_normalised` (l); compares across all four retailers; states unit and retailer; does not mix "each".
5. "did the price of Kendamil first infant milk change this month" (or any product the model finds): calls `price_history`, anchors on the data date (2026-10-29 today), reports versions with dates, or "no change since <first seen>"; does not say "today" is the real calendar date.
6. "milk-free biscuits for my daughter, she is allergic": excludes products whose `contains_allergens` mention milk, flags that some products have unknown allergen data and were excluded (or listed as unknown), recommends checking packaging.
7. "something like oat milk but cheaper": semantic query with little lexical overlap; vector side contributes results; dairy-free alternatives.
8. "what does Tesco have in baby food under 2 euro": single-retailer filter plus category browse (`list_categories` then `search_category`).
9. "compare the price of Heinz baked beans in each shop": same product across retailers, each with its own price and link status.
10. "show me something with an apostrophe: Cadbury's dairy milk": no SQL error (the old tool crashed on apostrophes), correct products.

For each: confirm the answer's prices equal `fct_price_history WHERE is_current AND is_active` for the same `(supermarket, id)` (they should match `dim_product_search.price`; a mismatch means the local tables came from different builds, see P5).

---

## 9. Pre-flight checklist (fresh session, before step 1)

Use `read_only=True` or a copy from `~/supermarket_backups/` (dated copies exist, e.g. `supermarket_data_20261029.db`); never write to `supermarket_data.db` from the agent session. Run Python with `uv run --group agents --group ml-embed python`.

- **P1. Environment.** `uv run python -c "import duckdb; print(duckdb.__version__)"` gives 2.0.x. `git status` and note uncommitted changes (`notebooks/embeddings.ipynb` and untracked docs are the user's).
- **P2. DB lock.** `lsof <db path>` (or try `duckdb.connect(path, read_only=True)`); if write-locked, stop and ask the user to close the notebook, or use a backup copy.
- **P3. Objects exist.** One query each; expected values as of 2026-11-04:
  - `SELECT count(*) FROM dim_product_search` -> equals `SELECT count(*) FROM fct_product_listings` (49,976 at the last inspection: 4,586 + 10,461 + 14,909 + 20,020).
  - `SELECT count(*) FROM emb_gemma2_text` -> 50,042 (will grow if embeddings were refreshed).
  - `SELECT spec_id, dim FROM dim_embedding_specs WHERE name='gemma2-text'` -> `268dad232ccc02bc, 768`.
  - `SELECT count(*) FROM duckdb_functions() WHERE function_name='bm25_search'` -> 1 (**currently 0**, see P4).
  - FTS doc count equals the table: `SELECT (SELECT count(*) FROM fts_main_dim_product_search.docs), (SELECT count(*) FROM dim_product_search)` (**schema does not exist yet**).
- **P4. Build the search table and the 2.0 index (if P3 shows they are missing, and the DB has been backfilled/refreshed first, see P5).** Order matters because of two constraints: (a) Fusion's DuckDB is 1.5.5 and the index should be rebuilt by 2.0 which serves it; (b) while `hnsw_idx` exists in the file, any write by an engine without `vss` loaded fails. Sequence:
  1. Close all other connections to the DB.
  2. `dbtf build --project-dir dbt --select dim_product_search --vars '{run_date: <latest date in fct_product_price_daily>}'` (Fusion binary `~/.local/bin/dbt`; `run_date` here only selects raw partitions that this model does not read; if the build complains, add `--exclude` of upstream models rather than running upstream builds that touch S3). Its post-hook builds the 1.5.5 index and the macro. If the build fails with a vss/HNSW write error, either load `vss` for that session, or drop the legacy index first (this conflicts with "drop only after verification": in that case the drop of `hnsw_idx` and `int_product_title_embeddings` is the one cleanup you may need to do early; the old agent can no longer search afterwards, which is acceptable once step 3-4 are ready; get the user's explicit OK).
  3. Rebuild the index with 2.0 (the macro text is unchanged and survives):
     ```python
     con = duckdb.connect(DB)                      # writable, 2.0, nothing else open
     con.execute("INSTALL fts; LOAD fts;")
     # if hnsw_idx still exists: con.execute("INSTALL vss; LOAD vss;") first
     con.execute("""PRAGMA create_fts_index('dim_product_search','product_key',
         'title_cleaned','brand','category_1','category_2','category_3','category_4', overwrite=1)""")
     ```
     (column list copied from `dbt/macros/search_index.sql`; keep identical.) Time it: `SELECT count(*) FROM bm25_search('milk')` should be well under a second.
  4. Check Fusion can still open the file afterwards (`dbtf` any trivial `show`/`compile`); if not, record it as a risk for the user, since the file would then be 2.0-only.
  5. After every future `dbt build` of `dim_product_search` under Fusion, the post-hook recreates the 1.5.5 index: re-run step 3. This should become a one-line script (`ml/reindex_search.py`, ~15 lines) if the user rebuilds often.
- **P5. DB freshness.** `SELECT supermarket, max(scraped_date) FROM fct_product_price_daily GROUP BY 1` -> 2026-10-29 for all four (as of 2026-11-04 the local DB is 6 days behind; the scheduled AWS job does **not** feed the local DB and does not build the BM25 index). To refresh locally: `dbt/scripts/backfill.py --start <date> --end <date> ...` then `dbtf build` (see `dbt/scripts/backfill.py` docstring and `dbt/README.md`), or use a published `search.duckdb` if one exists. `docs/next_steps.md` does not exist at the time of writing; if it exists when you read this, follow it for the published-file option. After a refresh, redo P3/P4 and compare: `SELECT count(*) FROM fct_product_listings l ANTI JOIN emb_gemma2_text e USING (supermarket, id)` (473 today) shows how many new products lack vectors until the embedding job and a dbt rebuild run.
- **P6. Encoder download.** `uv run --group ml-embed python -c "from ml_common.embedders import SentenceTransformerEmbedder as E; from ml_common.specs import get_spec; s=get_spec('gemma2-text'); print(s.spec_id); print(E(s).encode_queries(['gluten free bread']).shape)"` -> `268dad232ccc02bc` and `(1, 768)`. The model (`google/embeddinggemma-2`) may be gated or need `HF_TOKEN`; if it fails, ask the user to authenticate (the `hf-cli` skill covers it). BM25-only mode works without it.
- **P7. Legacy still present (expected).** `SELECT index_name FROM duckdb_indexes()` -> `hnsw_idx`; `int_product_title_embeddings` has 40,228 rows; schema `fts_main_int_supervalu` (the stray `fts_main_tesco_table` and `flock_config` were dropped on 2026-11-04). Do not touch them; see section 10.
- **P8. Sanity of the semantic side.** In Python, embed "bread" with the fake-free real encoder and run `SELECT supermarket, id, array_cosine_similarity(embedding, $v::FLOAT[768]) s FROM emb_gemma2_text ORDER BY s DESC LIMIT 5`; the top rows should be breads. If they are not, the query/doc prefixes or spec do not match the stored vectors; stop.
- **P9. Table macros accept bound parameters** (verified 2026-11-04 on 2.0.0 in memory; re-run on the real DB): `con.execute("SELECT count(*) FROM bm25_search(?)", ["it's bread"]).fetchone()` executes without error. If it ever fails, call `fts_main_dim_product_search.match_bm25(product_key, $q)` directly in a subquery instead of the macro.

---

## 10. Cleanup after the migration is verified (user runs these; DB drops are blocked for agents)

Do this only after the smoke list passes. The objects (verified present on 2026-11-04):

- Table `int_product_title_embeddings` (40,228 rows) with index `hnsw_idx` (`USING HNSW (title_embedding) WITH (metric = 'cosine')`).
- Schema `fts_main_int_supervalu` (FTS index over table `int_supervalu`).
- Views/tables possibly retired by the same change set; user decision: `int_supervalu` (a stale **BASE TABLE** locally while the other `int_*` are views), `int_aldi`, `int_tesco`, `int_dunnes`, `int_all_supermarket_products` (view), `stg_*`. Ask before dropping any: dbt rebuilds the int/stg models, and `fct_product_listings` is built from them. The brief names only the embedding table, the HNSW index and the two FTS schemas.

Run in a notebook (not by an agent), with the DB closed elsewhere. The index must be dropped with `vss` loaded:

```sql
INSTALL vss; LOAD vss;
SET hnsw_enable_experimental_persistence = true;
DROP INDEX hnsw_idx;
DROP TABLE int_product_title_embeddings;

INSTALL fts; LOAD fts;
PRAGMA drop_fts_index('int_supervalu');       -- preferred; fallback if it errors:
-- DROP SCHEMA fts_main_int_supervalu CASCADE;

CHECKPOINT;
```
Then verify: `SELECT * FROM duckdb_indexes()` is empty; `SELECT schema_name FROM duckdb_schemas() WHERE schema_name LIKE 'fts%'` returns only `fts_main_dim_product_search`; the agent still passes the smoke list (it needs none of the dropped objects). Optionally `VACUUM`/export-import to reclaim file space (the file is ~1.1 GB).

Code cleanup (the implementing session, step 7): remove `bi_encoder`, harrier references, `int_supervalu`, SuperValu-only regexes and unused imports from `ml/agents.py`; check `AGENTS.md` and `dbt/README.md` sentences that point at `ml/agents.py::hybrid_search` ("as `ml/agents.py::hybrid_search` does for SuperValu today", Product search section) and the `ml/agents.py` mention at the end of `AGENTS.md` ("`processed/embeddings/<slug>/...parquet` that dbt and `ml/agents.py` read") for staleness, and tell the user rather than editing docs they did not ask about. **`notebooks/agents.ipynb` is the user's call; do not edit notebooks.**

---

## 11. What can change before implementation, and how to adapt

- **Local DB is rebuilt/backfilled.** Re-run P3-P5. Everything in the agent is data-driven except three constants that depend on contents: the expected `dim` (768, asserted at first use), the embedding-table name, and the `unit_normalised` value set (verify, 6.3). The data date comes from the probe, so the prompt updates itself. New products without vectors are handled by R3. If `emb_gemma2_text` is rebuilt with a **new spec_id** (different model/revision/prefix), the first-use spec check fails loudly: update `AGENT_QUERY_SPEC`/`AGENT_EMBEDDING_TABLE` and the `FLOAT[768]` literal (derive it from `dim_embedding_specs.dim` and interpolate only after validating it is an integer in a small range; a `FLOAT[dim]` cast cannot be bound). Always re-run P4 step 3 after any Fusion rebuild of `dim_product_search`.
- **`dim_product_search` gains/loses columns.** Tools select columns explicitly, and the prompt schema is generated from `information_schema`, so extra columns appear in `run_sql` automatically but are not in tool output until you add them. A removed column breaks the specific SQL that names it: the fixture test 15 plus a startup self-check (`SELECT <all columns the tools use> FROM dim_product_search LIMIT 0` once at `get_connection()`) fails fast with the missing name. If a new product-URL column appears (it would answer Q2), add it to `get_product_details` and replace `PRODUCT_URL_TEMPLATES` with it. If new dietary flags are added, add them to the flag whitelist dict; AGENTS.md says the current set is complete and new flags are not welcome.
- **A published `search.duckdb` replaces the local file.** Set `SUPERMARKET_DB_PATH`. Requirements to check on arrival: it must contain `dim_product_search`, `bm25_search`, `fts_main_dim_product_search`, `emb_gemma2_text` (or a table the user names), `dim_embedding_specs`, `fct_price_history`, `fct_product_price_daily` (the data-date probe) and be indexed by DuckDB 2.0 (rebuild the FTS index if it was built by 1.5.5; check with the timed query in 5.3). If it ships without `emb_gemma2_text`, the agent runs BM25-only (the vector branch is feature-flagged by catalogue presence: `SELECT 1 FROM duckdb_tables() WHERE table_name = ?`). If it ships without price tables, `price_history` is disabled and the prompt says so. A read-only file on a read-only filesystem works since the agent already opens it read-only.
- **The model slug or provider changes.** `AGENT_MODEL` env; nothing else.

---

## 12. Risks

- **Embedding/doc symmetry regression**: if someone changes `GEMMA` prefixes in `ml_common/specs.py`, `spec_id` changes and the check fails; good. Do not hand-edit prefixes in the agent.
- **First query latency**: model load on first use (several seconds, GBs of RAM for a Gemma-sized model **[measure; the 2.0.0 test box has `mps`]**); mitigate with an optional warm-up call at server start behind an env flag (`AGENT_WARM_ENCODER=1`), default off to keep import side-effect-free.
- **FTS index staleness/version**: see P4/5.3. A stale index returns old products; `assert_search_index_fresh` in dbt catches it only inside dbt. The agent's startup probe compares `count(*) FROM fts_main_dim_product_search.docs` with `count(*) FROM dim_product_search` and logs a warning on mismatch.
- **Allergen safety**: wrong or unknown allergen data can harm. The prompt and tool output must never imply safety for NULL data (R2). Q1 is an open question precisely because of this.
- **Unlinked retailers**: without product URLs for tesco/aldi/dunnes, users get prices but no clickable page; images still work.
- **RRF coarseness**: see R4.
- **Concurrent threads**: pydantic-ai runs sync tools in threads; every call uses its own `cursor()`, and the encoder is guarded by a lock. Test with two simultaneous requests during smoke testing.
- **Fusion/2.0 file compatibility** after a 2.0 write to the file: checked in P4.4.
- **SQL tool abuse**: `run_sql` allows any SELECT on a read-only DB, including file/S3 table functions unless `enable_external_access` is disabled (5.3); verify before relying on it.

## 13. Open questions for the user

- Q1. Does an empty allergen list (`[]`) mean "none declared" or "not parsed"? (Tesco: 7,734 `[]` vs 11,237 NULL.) Until answered the agent treats `[]` as none declared but caveats.
- Q2. Product-page URL patterns for tesco, dunnes and aldi: not present in the repo (`stg_*` only carry image URLs). Provide verified templates, or accept answers without links for those retailers. (Do not guess.)
- Q3. Keep stored SuperValu image URLs (`zoom/`, `detail/`) as-is, or restore the `cell/` thumbnail rewrite after the user confirms those URLs resolve?
- Q4. Which retired objects besides `int_product_title_embeddings`/`hnsw_idx`/`fts_main_int_supervalu` should be dropped (`int_supervalu` base table, `int_all_supermarket_products` view, others)?
- Q5. Should I add the small `ml/reindex_search.py` (2.0 FTS rebuild) so the index is rebuilt by 2.0 after each Fusion build?
- Q6. Is the intended deployment still `agent.to_web` served locally? A published `search.duckdb` or the Batch-built data changes the DB path and refresh story (section 11).
- Q7. Should the agent state when the data is stale (data date older than N days vs today)? Default proposed: warn when more than 3 days.

---

## Appendix: original design intent for the agent's query flow

Preserved verbatim from the owner's early notes (previously in the repo-root file `tesco_sample_output.txt`,
which also held two sample LLM answers about Tesco steak cuts and has been removed). It is the intended
end-state flow; the current `ml/agents.py` implements part of it (hybrid search, category tools, link
validation). Use it to decide what later steps add; it is not a requirement of the migration above.

```text
QUERY FLOW:
--> Initial Search
--> Gliner to identify product, brand, supermarket quantity
--> if brand and supermarket detected, ensure these exist in database using levnsthein distance maybe?
--> Pass this metadata to initial LLM and perform hybrid Search (top k) and rerank
 --> Select disitnct aisle and relevant shelves in aisle
 --> Use LLM to parse intent and pick most applicable, or multiple if relevant, aisles/shelves 
 --> get similar categories if applicable(e.g there is Natural & Greek Style Yoghurts & Low Fat & Fat Free Yoghurt aisle create matrix of category similarities) 
 --> with relevant aisles picked feed this metadata and SQL prompt info into another LLM network to generate relevant list of products 
 --> Run SQL query 
 --> Return results and have LLM summarise these to end user (incluidng product photos would be useful here)
```
