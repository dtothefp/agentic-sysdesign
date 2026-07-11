-- Module 4 visibility: count how many of a run's signals have been rated.
--
-- The rating stage is decoupled from the scrape chord on purpose: a slow model call must
-- never block a scrape or the fan-in barrier. So a run flips to `completed` the moment
-- scraping finishes, while its rate_signal jobs are still draining behind it. Without a
-- counter, that whole phase is invisible, the SSE stream just goes quiet between the last
-- `progress` and `done`.
--
-- rated_count is the numerator the worker bumps as each rating lands. runs.inserted is the
-- denominator (every inserted signal enqueues exactly one rate_signal for its run). The pair
-- lets a client render "rating 12/25" as a real phase WITHOUT coupling run status to the
-- model. Sweep-originated ratings carry no run_id and don't touch this counter.

-- migrate:up
ALTER TABLE runs ADD COLUMN rated_count integer NOT NULL DEFAULT 0;

-- migrate:down
ALTER TABLE runs DROP COLUMN IF EXISTS rated_count;
