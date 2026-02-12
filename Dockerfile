FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (better layer caching)
# COPY pyproject.toml uv.lock ./

# Install dependencies into system Python
# RUN uv pip install --system -r pyproject.toml
RUN uv pip install --system aiohttp boto3 brotli

# Copy application files
COPY tesco/tesco_api.py ./
COPY tesco/graphql_query.txt ./
COPY tesco/ie_tesco_ids.csv ./

COPY supervalu/supervalu_api.py ./
COPY supervalu/supervalu_ids.csv ./

CMD ["python", "tesco_api.py"]