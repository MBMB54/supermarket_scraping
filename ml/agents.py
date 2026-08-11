import os
import re
from dataclasses import dataclass
from datetime import date

import duckdb
import logfire
from dotenv import load_dotenv
from pydantic_ai import (
    Agent,
    ModelRetry,
    RunContext,
)
from pydantic_ai.capabilities import NativeTool, WebFetch
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.providers.openrouter import OpenRouterProvider
from sentence_transformers import SentenceTransformer

load_dotenv()

logfire.configure(send_to_logfire=False)
logfire.instrument_pydantic_ai()

bi_encoder = SentenceTransformer("microsoft/harrier-oss-v1-270m", trust_remote_code=True)

open_router_key = os.getenv("OPEN_ROUTER_API_KEY")
TABLE_NAME = "int_supervalu"

_URL_RE = re.compile(r"https?://[^\s)\]]+")
_SUPERVALU_HOST_RE = re.compile(r"^https?://(?:[\w-]+\.)*supervalu\.ie", re.IGNORECASE)
_IMAGE_HOST_RE = re.compile(r"^https://images\.cdn\.shop\.supervalu\.ie/")
_CANONICAL_PRODUCT_RE = re.compile(r"^https://shop\.supervalu\.ie/product/id-(\d+)$")
_IMAGE_FILENAME_RE = re.compile(r"/([^/]+)$")
_PLACEHOLDER_LINK_RE = re.compile(r"\]\(\s*(?:None|no_image)?\s*\)")
_PRESENTED_PRODUCT_RE = re.compile(
    r"\*\*\[[^\]]*\]\((?P<url>https?://[^\s)\]]+)\)\*\*\s*—\s*€\s*(?P<price>\d+(?:\.\d+)?)"
)
_TRAILING_PUNCTUATION = ".,;:!?"

_RETRY_SILENTLY = " Just send the corrected answer — don't mention or apologize for this fix."

con = duckdb.connect("/Users/brianbarry/supermarket_scraping/supermarket_data.db", read_only=True)
con.sql("CREATE OR REPLACE SECRET secret (TYPE s3,PROVIDER credential_chain);")
con.install_extension("vss", repository="core")
con.load_extension("vss")
con.sql("set hnsw_enable_experimental_persistence = True")


@dataclass
class Deps:
    conn: duckdb.DuckDBPyConnection


def get_schema(conn: duckdb.DuckDBPyConnection, table: str) -> str:
    rows = conn.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = ?
        ORDER BY ordinal_position
        """,
        [table],
    ).fetchall()
    cols = ",\n  ".join(f"{name} {dtype}" for name, dtype in rows)
    return f"CREATE TABLE {table} (\n  {cols}\n);"


def _cell(value: object) -> str:
    """Render one result cell on a single line.

    Descriptions and ingredient lists contain literal newlines (~45% of products),
    which would otherwise split one product across several apparent rows and let the
    model pair the wrong image with the wrong product.
    """
    return " ".join(str(value).split())


mimo_model = OpenRouterModel(
    "xiaomi/mimo-v2.5",
    provider=OpenRouterProvider(api_key=open_router_key),
)

deepseek_model = OpenRouterModel(
    "deepseek/deepseek-v4-flash-0731",
    provider=OpenRouterProvider(api_key=open_router_key),
)

qwen_model = OpenRouterModel(
    "qwen/qwen3.7-flash",
    provider=OpenRouterProvider(api_key=open_router_key),
)

openai_model = OpenRouterModel(
    "openai/gpt-5.6-luna",
    provider=OpenRouterProvider(api_key=open_router_key),
)


agent = Agent(
    mimo_model,
    name="supermarket_agent",
    capabilities=[NativeTool(WebSearchTool()), WebFetch(local=True)],
    deps_type=Deps,
    retries={"output": 2},
)


@agent.tool_plain
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny"


@agent.system_prompt
def system_prompt(ctx: RunContext[Deps]) -> str:
    return f"""\
You are a data analyst. Answer the user's question about the `{TABLE_NAME}`
table by writing DuckDB SQL and running it with the `run_sql` tool.
Issue only SELECT queries, and inspect the returned rows before giving your
final answer.

For product questions, start with the `hybrid_search` tool. Its results are a
bounded, ranked sample, not the full set of matching products. Use the
category_1-4 fields it returns with `search_category` to see everything in that
category, and use `get_product_details` when you need more fields for specific
products already found. Use your judgement for further exploration beyond these.

For "cheapest"/"most expensive"/"best value" questions, never rely on hybrid_search's
result order — it ranks by text relevance, not price, so the cheapest match is often
nowhere near the top. Use `search_category` instead, so you're comparing every
matching product rather than a relevance-ranked sample. Start scoped at category_1/
category_2 from what hybrid_search returned, not category_3/4 — the cheapest option
can sit in a sibling category_3/4 under the same category_2 that hybrid_search
didn't happen to surface. Only narrow to category_3/4 (or add include_terms) if
category_2 alone returns too broad or noisy a set to compare directly.

Don't silently exclude a product that showed up in your search based on an assumed
distinction between it and what the user asked for — treating a differently-worded or
differently-sized product name as a non-match. Only exclude something if the user's
request was explicit about it, and interpret style words like "plain" or "unflavored"
narrowly: they constrain flavoring, coating, seasoning, sauce, or marinade only — never
size, cut, weight, or pack format. A smaller or larger version of a plain product is
still plain. If you're unsure whether something counts, include it in your comparison
and note the distinction rather than dropping it. This applies equally to how you
reason about candidates and to `search_category`'s exclude_terms — see its docstring
for what belongs there.

If you write your own title filters in run_sql, match each meaningful word as its
own wildcard condition (e.g. LOWER(title) LIKE '%word1%' AND LOWER(title) LIKE
'%word2%') rather than one contiguous phrase — product titles often insert a
modifier between words you're searching for, and a single-phrase pattern will
silently miss those products entirely.

Presenting products:

When your final answer highlights a specific product, link its name to its page and
show its picture, one block per product (blank line before the image, or Streamdown
folds it into the paragraph):

**[<title>](<product_url>)** — €<price>

![<title>](<image_url>)

- `product_url`, `image_url` and authoritative prices come only from
  `get_product_details`. Call it before showing any product.
- Never write a supervalu.ie URL or a price you did not receive from a tool. Never
  fall back to https://www.supervalu.ie, and never reuse one product's image for
  another.
- If `image_url` is `no_image`, omit the image line but keep the link.
- Show images when highlighting roughly 5 products or fewer; for longer comparison
  lists give linked titles and prices only, so the answer stays scannable.
- Links to recipes or other outside sources from web search are fine and unaffected.

Schema:

{get_schema(ctx.deps.conn, TABLE_NAME)}

Today's date is {date.today()}.
"""


@agent.tool()
def run_sql(ctx: RunContext[Deps], sql: str) -> str:
    """Run a read-only DuckDB SELECT query and return up to 20 result rows.

    Args:
        ctx: run context
        sql: a single DuckDB SELECT statement
    """
    if not (sql.lstrip().upper().startswith("SELECT") or sql.lstrip().upper().startswith("WITH")):
        raise ModelRetry("Only SELECT queries are allowed.")
    try:
        rel = ctx.deps.conn.cursor().sql(sql)
        columns = rel.columns
        rows = rel.fetchmany(25)
    except duckdb.Error as e:
        raise ModelRetry(f"Query failed: {e}") from e

    if not rows:
        return "(no rows)"
    header = " | ".join(columns)
    body = "\n".join(" | ".join(_cell(v) for v in row) for row in rows)
    return f"{header}\n{body}"


@agent.tool()
def get_product_details(ctx: RunContext[Deps], ids: list[str]) -> str:
    """Fetch full details for specific SuperValu products already identified, e.g. via
    hybrid_search or run_sql. Returns fields hybrid_search omits: ingredients, item
    description, allergens/dietary flags, current pricing/promotions, and the two
    presentation fields — `product_url` (the product's page on shop.supervalu.ie) and
    `image_url` (the product photo, or the literal `no_image` when it has none).

    Call this for every product you are about to show the user: it is the only source
    of authoritative prices, image URLs and product links.

    Args:
        ids: product ids to fetch, as returned by hybrid_search or run_sql
    """
    if not ids:
        return "(no ids provided)"
    try:
        rel = ctx.deps.conn.cursor().execute(
            """
            SELECT id, title,
                   'https://shop.supervalu.ie/product/id-' || id AS product_url,
                   CASE WHEN image_url IS NULL THEN 'no_image'
                        ELSE 'https://images.cdn.shop.supervalu.ie/cell/'
                             || regexp_extract(image_url, '([^/]+)$', 1)
                   END AS image_url,
                   brand, item_description, ingredients,
                   category_1, category_2, category_3, category_4,
                   ROUND(price, 2) AS price, ROUND(was_price, 2) AS was_price,
                   ROUND(unit_price, 2) AS unit_price,
                   is_promotion, promotion_description,
                   is_vegan, is_vegetarian, is_gluten_free, contains_allergens
            FROM int_supervalu
            WHERE id IN ?
            """,
            [ids],
        )
        columns = [d[0] for d in rel.description]
        rows = rel.fetchall()
    except duckdb.Error as e:
        raise ModelRetry(f"Query failed: {e}") from e

    if not rows:
        return "(no rows)"
    header = " | ".join(columns)
    body = "\n".join(" | ".join(_cell(v) for v in row) for row in rows)
    return f"{header}\n{body}"


@agent.tool()
def hybrid_search(
    ctx: RunContext[Deps], search_term: str, embed_weight: float = 0.6, fts_weight: float = 0.4
) -> str:
    """Search SuperValu products by fusing BM25 text search (title) and vector similarity
    search (title embedding) via weighted reciprocal rank fusion (RRF).

    Use this first to find products relevant to the user's query. It returns only
    id, title, and category columns to keep results compact — not ingredients,
    item descriptions, prices, images or links. Call `get_product_details` with the
    ids returned by this search before showing any of these products to the user.

    Args:
        search_term: a short product search phrase, e.g. "green curry paste"
        embed_weight: weight given to the vector-similarity rank; raise this for fuzzy or
            semantic queries where the wording may not match the product title exactly
        fts_weight: weight given to the BM25 text-match rank; raise this for queries using
            exact product or brand names
    """
    search_term_embedding = bi_encoder.encode(search_term, normalize_embeddings=True).tolist()
    query = f"""
    WITH emb AS (
        SELECT
            id,
            array_cosine_similarity(title_embedding,{search_term_embedding}::FLOAT[640])::decimal(3,2) AS embed_score
        FROM int_product_title_embeddings
        WHERE supermarket = 'supervalu'
    ),

    fts AS (
        SELECT
            id, title, category_1, category_2, category_3, category_4,
            fts_main_int_supervalu.match_bm25(id,'{search_term}')::decimal AS bm25_score
        FROM int_supervalu
    ),

    ranked AS (
        SELECT
            fts.id, fts.title, fts.category_1, fts.category_2, fts.category_3, fts.category_4,
            ROW_NUMBER() OVER (
                ORDER BY CASE WHEN fts.bm25_score IS NULL THEN 1 ELSE 0 END,
                         fts.bm25_score DESC
            ) AS fts_rank,
            ROW_NUMBER() OVER (ORDER BY emb.embed_score DESC) AS emb_rank
        FROM fts
        INNER JOIN emb ON fts.id = emb.id
    )

    SELECT
        id, title, category_1, category_2, category_3, category_4,
        ({embed_weight} / (60 + emb_rank) + {fts_weight} / (60 + fts_rank)) AS rrf_score
    FROM ranked
    ORDER BY rrf_score DESC
    """
    try:
        rel = ctx.deps.conn.cursor().sql(query)
        columns = rel.columns
        rows = rel.fetchmany(21)
    except duckdb.Error as e:
        raise ModelRetry(f"Query failed: {e}") from e

    if not rows:
        return "(no rows)"
    header = " | ".join(columns)
    body = "\n".join(" | ".join(str(v) for v in row) for row in rows)
    return (
        f"{header}\n{body}\n"
        "(these rows carry no price, image or link — call get_product_details with "
        "the ids of any products you intend to show the user)"
    )


_SORT_COLUMNS = {
    "price": "price",
    "price_per_unit_normalised": "price_per_unit_normalised",
    "unit_price": "unit_price",
}


def _category_conditions(
    category_1: str | None,
    category_2: str | None,
    category_3: str | None,
    category_4: str | None,
    include_terms: list[str] | None,
    exclude_terms: list[str] | None,
) -> tuple[list[str], list[object]]:
    conditions = ["is_product_available = TRUE", "price IS NOT NULL"]
    params: list[object] = []

    for col, val in (
        ("category_1", category_1),
        ("category_2", category_2),
        ("category_3", category_3),
        ("category_4", category_4),
    ):
        if val:
            conditions.append(f"{col} = ?")
            params.append(val)

    for term in include_terms or []:
        conditions.append("LOWER(title) LIKE '%' || LOWER(?) || '%'")
        params.append(term)

    if exclude_terms:
        exclude_clause = " OR ".join(
            "LOWER(title) LIKE '%' || LOWER(?) || '%'" for _ in exclude_terms
        )
        conditions.append(f"NOT ({exclude_clause})")
        params.extend(exclude_terms)

    return conditions, params


@agent.tool()
def search_category(
    ctx: RunContext[Deps],
    category_1: str | None = None,
    category_2: str | None = None,
    category_3: str | None = None,
    category_4: str | None = None,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    sort_by: str = "price_per_unit_normalised",
    ascending: bool = True,
    limit: int = 30,
) -> str:
    """Search every in-stock product in a category, sorted by price.

    Use this for "cheapest"/"most expensive"/"best value" questions and any other
    time you want to compare all matching products rather than a relevance-ranked
    sample — it scans the full category, not a bounded top-k like hybrid_search.
    Get category values from a prior hybrid_search call, but start scoped at
    category_1/category_2 rather than category_3/4 — the cheapest option can sit in
    a sibling category_3/4 that hybrid_search didn't happen to surface. Only narrow
    to category_3/4 if category_2 alone returns too broad or noisy a set.

    Each entry in include_terms is matched as its own independent substring, so
    products with a modifier word inserted between your terms (e.g. a title that
    puts another word between two words you searched for) are still found —
    unlike a single hand-written LIKE '%phrase%' pattern, which would miss them.

    Only put flavoring, coating, or preparation words in exclude_terms (e.g. when
    the user asked for "plain"). Never put size, cut, or pack-format words there —
    that would wrongly exclude legitimate matches. If you're unsure which a word
    is, leave it out of exclude_terms.

    Args:
        category_1: exact category_1 value to filter to, if known
        category_2: exact category_2 value to filter to, if known
        category_3: exact category_3 value to filter to, if known
        category_4: exact category_4 value to filter to, if known
        include_terms: words that must all appear in the title, each matched
            independently (e.g. ["chicken", "fillet"])
        exclude_terms: words that must not appear in the title — flavoring/coating/
            preparation only, never size/cut/pack-format
        sort_by: one of "price", "price_per_unit_normalised" (default), "unit_price"
        ascending: True for cheapest first (default), False for most expensive first
        limit: max rows to return (capped at 50)
    """
    if sort_by not in _SORT_COLUMNS:
        raise ModelRetry(f"sort_by must be one of {sorted(_SORT_COLUMNS)}, got {sort_by!r}.")

    sort_col = _SORT_COLUMNS[sort_by]
    direction = "ASC" if ascending else "DESC"

    conditions, params = _category_conditions(
        category_1, category_2, category_3, category_4, include_terms, exclude_terms
    )
    where_clause = " AND ".join(conditions)

    try:
        rel = ctx.deps.conn.cursor().execute(
            f"""
            SELECT id, title, category_1, category_2, category_3, category_4,
                   ROUND(price, 2) AS price, ROUND(price_per_unit_normalised, 2) AS price_per_unit_normalised,
                   unit_normalised
            FROM int_supervalu
            WHERE {where_clause}
            ORDER BY {sort_col} {direction} NULLS LAST
            LIMIT ?
            """,
            [*params, min(limit, 50)],
        )
        columns = [d[0] for d in rel.description]
        rows = rel.fetchall()
    except duckdb.Error as e:
        raise ModelRetry(f"Query failed: {e}") from e

    if not rows:
        return "(no rows)"

    broaden_note = ""
    if category_3 or category_4:
        broad_conditions, broad_params = _category_conditions(
            category_1, category_2, None, None, include_terms, exclude_terms
        )
        broad_where = " AND ".join(broad_conditions)
        try:
            broad_top = (
                ctx.deps.conn.cursor()
                .execute(
                    f"""
                    SELECT id, title, category_3, category_4,
                           ROUND({sort_col}, 2) AS sort_val
                    FROM int_supervalu
                    WHERE {broad_where}
                    ORDER BY {sort_col} {direction} NULLS LAST
                    LIMIT 1
                    """,
                    broad_params,
                )
                .fetchone()
            )
        except duckdb.Error:
            broad_top = None

        if broad_top and broad_top[0] not in {row[0] for row in rows}:
            broaden_note = (
                f"\n(narrowing to category_3/category_4 excluded a better option: "
                f"{broad_top[1]!r} in category_3={broad_top[2]!r}, category_4={broad_top[3]!r}, "
                f"{sort_by}={broad_top[4]}. Call search_category again without category_3/"
                f"category_4 if you want to include it.)"
            )

    header = " | ".join(columns)
    body = "\n".join(" | ".join(_cell(v) for v in row) for row in rows)
    return (
        f"{header}\n{body}\n"
        "(these rows carry no image or link — call get_product_details with "
        "the ids of any products you intend to show the user)"
        f"{broaden_note}"
    )


def _authoritative_products(
    messages: list[object],
) -> dict[str, dict[str, str | float | None]]:
    """Rebuild the id -> {product_url, image_url, price} rows get_product_details actually
    returned in this conversation, straight from the tool-return messages.

    Used as the source of truth in validate_product_links instead of a fresh int_supervalu
    query, so a product the model never called get_product_details on can't slip through just
    because the id happens to exist in the table.
    """
    products: dict[str, dict[str, str | float | None]] = {}
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart) or part.tool_name != "get_product_details":
                continue
            content = part.content
            if not isinstance(content, str) or "|" not in content:
                continue
            lines = content.splitlines()
            idx = {name: i for i, name in enumerate(lines[0].split(" | "))}
            for line in lines[1:]:
                cells = line.split(" | ")
                try:
                    price = float(cells[idx["price"]])
                except ValueError:
                    price = None
                products[cells[idx["id"]]] = {
                    "product_url": cells[idx["product_url"]],
                    "image_url": cells[idx["image_url"]],
                    "price": price,
                }
    return products


@agent.output_validator
def validate_product_links(ctx: RunContext[Deps], output: str) -> str:
    """Reject supervalu.ie links/images/prices the agent didn't get from
    get_product_details this conversation.

    Runs on every final answer. Product links must be the canonical
    shop.supervalu.ie/product/id-<id> form for an id get_product_details actually returned,
    image URLs must match that same call's image_url, and any price shown next to a product
    link must match that call's price. Non-supervalu.ie URLs (e.g. recipe citations from web
    search) are left untouched.
    """
    if _PLACEHOLDER_LINK_RE.search(output):
        raise ModelRetry(
            "Found a link or image with an empty/placeholder target (e.g. `](None)` or "
            "`](no_image)`). If image_url is `no_image`, omit the image line entirely "
            "instead of writing it with a placeholder target." + _RETRY_SILENTLY
        )

    supervalu_urls = [
        u.rstrip(_TRAILING_PUNCTUATION)
        for u in _URL_RE.findall(output)
        if _SUPERVALU_HOST_RE.match(u)
    ]
    if not supervalu_urls:
        return output

    products = _authoritative_products(ctx.messages)

    product_ids: set[str] = set()
    image_filenames: set[str] = set()

    for url in supervalu_urls:
        if _IMAGE_HOST_RE.match(url):
            m = _IMAGE_FILENAME_RE.search(url)
            if not m:
                raise ModelRetry(
                    f"{url!r} is not a valid image URL from get_product_details." + _RETRY_SILENTLY
                )
            image_filenames.add(m.group(1))
        else:
            m = _CANONICAL_PRODUCT_RE.match(url)
            if not m:
                raise ModelRetry(
                    f"{url!r} is not a valid SuperValu product link. Product links must be "
                    "exactly https://shop.supervalu.ie/product/id-<id>, using an id "
                    "get_product_details actually returned this conversation. Never use "
                    "https://www.supervalu.ie or any other supervalu.ie page." + _RETRY_SILENTLY
                )
            product_ids.add(m.group(1))

    if missing := product_ids - products.keys():
        raise ModelRetry(
            f"These product ids were never returned by get_product_details: {sorted(missing)}. "
            "Call get_product_details for every product before presenting it, and use only "
            "the ids, links, and prices it returns." + _RETRY_SILENTLY
        )

    if image_filenames:
        known_files = {
            m.group(1)
            for p in products.values()
            if p["image_url"] not in (None, "no_image")
            and (m := _IMAGE_FILENAME_RE.search(str(p["image_url"])))
        }
        if missing := image_filenames - known_files:
            raise ModelRetry(
                f"These image URLs don't correspond to a product get_product_details "
                f"returned this conversation: {sorted(missing)}." + _RETRY_SILENTLY
            )

    for match in _PRESENTED_PRODUCT_RE.finditer(output):
        url = match.group("url").rstrip(_TRAILING_PUNCTUATION)
        id_match = _CANONICAL_PRODUCT_RE.match(url)
        if not id_match:
            continue
        authoritative = products.get(id_match.group(1))
        if authoritative is None or authoritative["price"] is None:
            continue
        stated_price = float(match.group("price"))
        actual_price = authoritative["price"]
        if abs(stated_price - actual_price) > 0.01:
            raise ModelRetry(
                f"Product {id_match.group(1)} was shown at €{stated_price:.2f}, but "
                f"get_product_details returned €{actual_price:.2f}. Use the exact price "
                "from the tool result, not a rounded, recalled, or estimated one." + _RETRY_SILENTLY
            )

    return output


deps = Deps(con)
app = agent.to_web(
    models={
        "MiMo-V2.5": mimo_model,
        "DeepSeek V4 Flash": deepseek_model,
        "Qwen3.7 Flash": qwen_model,
        "GPT-5.6 Luna": openai_model,
    },
    deps=deps,
)
