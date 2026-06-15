import duckdb
import polars as pl
from sentence_transformers import SentenceTransformer

con = duckdb.connect("/Users/brianbarry/supermarket_scraping/supermarket_data.db")
con.sql("CREATE OR REPLACE SECRET secret (TYPE s3,PROVIDER credential_chain);")


bi_encoder = SentenceTransformer("microsoft/harrier-oss-v1-270m")

df_sv = con.sql("SELECT * FROM int_supervalu").pl()
df_dunnes = con.sql("SELECT * FROM int_dunnes").pl()
df_tesco = con.sql("SELECT * FROM int_tesco").pl()
df_aldi = con.sql("SELECT * FROM int_aldi").pl()

df = con.sql("SELECT * FROM int_all").pl()

product_titles = df.filter(pl.col("title").is_not_null())["title"].to_list()
product_embeddings = bi_encoder.encode(
    product_titles, normalize_embeddings=True, show_progress_bar=True, batch_size=128
)

# Create emdebeddings table with id, title, embedding and embedding type on first run - CREATE OR REPLACE?
# Perform anti-join between embeddings table and SCD to embed any new products
# Add any new embeddings to int duckdb table


#
con.execute("ALTER TABLE int_all ADD COLUMN harrier_embeddings FLOAT[1024]")

# Update from the polars dataframe
con.execute("""
    UPDATE int_all
    SET harrier_embeddings = int_all.harrier_embeddings
    FROM int_a
    WHERE tesco_products.tpnc = tesco_raw_df.tpnc
""")
