"""The task contract: everything the API is allowed to know about the worker.

Before the monorepo split, the API did `from worker.tasks import start_run` and ran the
whole dispatch (DB row + Celery chord) in its own process. That import meant the API
couldn't build, test, or deploy without the worker's code and its dependency set, and any
worker refactor was an API change too. Classic distributed monolith.

The contract inverts that. The two services now share only what actually crosses the wire:

  * task NAMES, plain strings the broker routes on. The worker registers its dispatch task
    under DISPATCH_RUN; the API sends to that name with `send_task`, never importing the
    function. Renaming or refactoring the implementation is invisible to the API as long
    as the registered name (this file) doesn't change.
  * payload SHAPE, documented on the constant. Celery JSON-serializes kwargs, so the shape
    is the contract; keep it flat and JSON-native.

`send_only_celery()` is deliberately minimal: broker URL only, no result backend, no task
imports. The API fires and forgets; the durable record of the request is the `runs` row the
API already wrote, not a Celery result. If the worker is down, the row sits in 'queued' and
the run picks up when the worker returns, which is exactly the visibility you want.
"""

import os

from celery import Celery

# kwargs: run_id (int), mode ("live" | "demo"), limit (int), model (str | None).
# The API creates the runs row first and passes its id; the worker owns everything after.
DISPATCH_RUN = "runs.dispatch"


def send_only_celery() -> Celery:
    """A minimal Celery client for send_task. Broker only: no result backend, no includes,
    no worker config. Reads the same CELERY_BROKER_URL the worker's app reads."""
    return Celery(broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
