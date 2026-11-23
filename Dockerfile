# SPDX-License-Identifier: Apache-2.0
# feederline-mcp server image: the MCP server, the admin CLI (migrations, loaders, simulator) and sqitch.
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqitch libdbd-pg-perl postgresql-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
LABEL org.opencontainers.image.source="https://github.com/kartha-energy/feederline-mcp" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="feederline-mcp"
EXPOSE 8000
CMD ["uvicorn", "feederline_mcp.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
