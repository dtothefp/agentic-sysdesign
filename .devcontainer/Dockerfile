# Workspace image for the dev container. Bakes in everything the Makefile targets need
# at runtime, so a rebuild is reproducible and nothing depends on postCreate PATH quirks:
#   psql  -> postgresql-client   (schema, partitions, drills, matview refresh)
#   make  -> the Makefile itself  (not guaranteed in the base image)
#   uv    -> the seed step (uv run python -m common.seed) and dependency sync
# Postgres itself is the sibling `db` service, not installed here.
FROM mcr.microsoft.com/devcontainers/python:3.13

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client make \
 && rm -rf /var/lib/apt/lists/*

# uv copied from its official image straight onto the global PATH, so make recipes and
# interactive terminals find it with no PATH juggling and no install-script network call.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
