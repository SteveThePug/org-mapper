FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" CONFIG_DIR=/app/config DATA_DIR=/app/data
RUN useradd --create-home --uid 10001 radar && mkdir -p /app/data && chown radar /app/data
USER radar

EXPOSE 8000
# The API is reachable only through Caddy on the compose network, so trust its X-Forwarded-For:
# the client IP is used for login rate limiting, and without this every request looks like Caddy.
CMD ["uvicorn", "radar.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
