# Workspace image for the dev container. Bakes in everything the moon tasks need at
# runtime, so a rebuild is reproducible and nothing depends on postCreate PATH quirks:
#   psql      -> postgresql-client   (schema, partitions, drills, matview refresh)
#   moon      -> the task runner, a standalone Rust binary fetched directly from its
#                GitHub release (see below). No node/pnpm for moon itself: moon is not a
#                node package. Node 22 for services/agent-ts comes from the devcontainer
#                feature in devcontainer.json (on PATH globally, not nvm).
#   uv        -> the seed step (uv run python -m common.seed) and dependency sync
#   redis-cli -> redis-tools (client only): inspect the Module 2 broker/result-backend
#                keys, LLEN/LRANGE the queue, MONITOR, SUBSCRIBE run:<id>. The server
#                is the sibling `redis` service, so use it as `redis-cli -h redis`.
# Postgres itself is the sibling `db` service, not installed here.
FROM mcr.microsoft.com/devcontainers/python:3.13

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client redis-tools xz-utils \
 && rm -rf /var/lib/apt/lists/*

# moon: the ONE task runner. A prebuilt Rust binary from the GitHub release, pinned and
# arch-matched to the image (same approach as dbmate below). The release ships a .tar.xz
# of `moon_cli-<target>/{moon,moonx,...}`; --strip-components=1 drops the two binaries
# straight onto the PATH. Keep this version in step with .moon/workspace.yml's
# versionConstraint and `brew install moon` on the host.
ARG MOON_VERSION=2.4.3
RUN case "$(dpkg --print-architecture)" in \
      amd64) TARGET=x86_64-unknown-linux-gnu ;; \
      arm64) TARGET=aarch64-unknown-linux-gnu ;; \
      *) echo "unsupported arch $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/moonrepo/moon/releases/download/v${MOON_VERSION}/moon_cli-${TARGET}.tar.xz" \
    | tar -xJ -C /usr/local/bin --strip-components=1 "moon_cli-${TARGET}/moon" "moon_cli-${TARGET}/moonx" \
 && chmod +x /usr/local/bin/moon /usr/local/bin/moonx

# uv copied from its official image straight onto the global PATH, so moon tasks and
# interactive terminals find it with no PATH juggling and no install-script network call.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# dbmate: a single-binary SQL migration tool. Migrations are plain .sql files with
# migrate:up / migrate:down sections; it tracks applied ones in a schema_migrations
# table so it never runs one twice. Arch-matched to the image (amd64 or arm64).
RUN ARCH=$(dpkg --print-architecture) \
 && curl -fsSL -o /usr/local/bin/dbmate "https://github.com/amacneil/dbmate/releases/latest/download/dbmate-linux-${ARCH}" \
 && chmod +x /usr/local/bin/dbmate
