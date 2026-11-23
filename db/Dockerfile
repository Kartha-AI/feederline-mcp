# SPDX-License-Identifier: Apache-2.0
# Postgres 17 + PostGIS 3.5 + pgRouting from apt.postgresql.org (multi-arch; the postgis/postgis image ships no pgRouting).
FROM postgres:17-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-postgis-3 postgresql-17-postgis-3-scripts postgresql-17-pgrouting \
 && rm -rf /var/lib/apt/lists/*
# postgis, pgrouting, uuid-ossp on first start
COPY init/ /docker-entrypoint-initdb.d/
LABEL org.opencontainers.image.source="https://github.com/Kartha-AI/feederline-mcp" \
      org.opencontainers.image.title="feederline-postgis"
