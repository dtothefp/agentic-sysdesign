"""The Celery application: the process that runs background scrape jobs.

ExtractIQ builds its Celery app off Django settings; we have no Django, so we configure it
directly from env vars (the same REDIS_URL split the dev container sets). Two roles:

  * broker (CELERY_BROKER_URL, redis /0): the queue tasks are pushed onto and workers pull from.
  * result backend (CELERY_RESULT_BACKEND, redis /1): where a task's return value is stored,
    which is also how a chord knows all its fan-out tasks finished before firing the callback.

Start a worker from a dev-container terminal with `make worker` (and `make worker-beat` for
the periodic refresh backstop). Tasks live in worker.tasks; include= tells Celery to import
that module on startup so the tasks register.
"""

import os

from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "sysdesign",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
    include=["worker.tasks"],
)

celery_app.conf.update(
    task_track_started=True,  # report a 'started' state, not just pending -> success
    result_expires=14400,  # drop task results from Redis after 4h (ExtractIQ's value)
    worker_prefetch_multiplier=1,  # fair fan-out: don't let one worker hoard queued tasks
)

# Backstop refresh of the rollup. The fan-in step already refreshes after every run, so the
# matview is fresh the moment a run finishes. This periodic task only covers the gap where
# signals arrive by some path that didn't go through a run (manual insert, a partial failure),
# so the dashboard is never more than a few minutes stale. Runs alongside the worker via
# `make worker-beat` (celery beat is a separate singleton process).
celery_app.conf.beat_schedule = {
    "refresh-rollup-backstop": {
        "task": "worker.tasks.refresh_rollup_task",
        "schedule": crontab(minute="*/5"),
    },
    # Module 4 backstop: sweep signals that never got a rating (exhausted retries, API-path
    # inserts). No-ops instantly when RATING_MODEL is unset, so it's safe to schedule always.
    "sweep-unrated-backstop": {
        "task": "worker.tasks.sweep_unrated",
        "schedule": crontab(minute="*/10"),
    },
}
