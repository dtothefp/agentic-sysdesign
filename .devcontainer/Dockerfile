# Workspace image for the dev container. Bakes in everything the moon tasks need at
# runtime, so a rebuild is reproducible and nothing depends on postCreate PATH quirks:
#   psql      -> postgresql-client   (schema, partitions, drills, matview refresh)
#   node/pnpm -> moon itself is repo-pinned via package.json (@moonrepo/cli), so the
#                image carries the runtime and pnpm; postCreate's `pnpm install` puts
#                the moon binary in node_modules/.bin (on PATH below).
#   uv        -> the seed step (uv run python -m common.seed) and dependency sync
#   redis-cli -> redis-tools (client only): inspect the Module 2 broker/result-backend
#                keys, LLEN/LRANGE the queue, MONITOR, SUBSCRIBE run:<id>. The server
#                is the sibling `redis` service, so use it as `redis-cli -h redis`.
# Postgres itself is the sibling `db` service, not installed here.
FROM mcr.microsoft.com/devcontainers/python:3.13

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client redis-tools \
 && rm -rf /var/lib/apt/lists/*

# node 22 (NodeSource; bookworm's packaged node is too old) + pnpm pinned to the
# version in package.json's packageManager field. Both exist only to install and run
# the repo-pinned moon.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g pnpm@10.12.1

# bare `moon` works in container terminals once postCreate has run `pnpm install`.
ENV PATH="/workspace/node_modules/.bin:${PATH}"

# uv copied from its official image straight onto the global PATH, so make recipes and
# interactive terminals find it with no PATH juggling and no install-script network call.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# dbmate: a single-binary SQL migration tool. Migrations are plain .sql files with
# migrate:up / migrate:down sections; it tracks applied ones in a schema_migrations
# table so it never runs one twice. Arch-matched to the image (amd64 or arm64).
RUN ARCH=$(dpkg --print-architecture) \
 && curl -fsSL -o /usr/local/bin/dbmate "https://github.com/amacneil/dbmate/releases/latest/download/dbmate-linux-${ARCH}" \
 && chmod +x /usr/local/bin/dbmate
