-- Fun statistics and insights about the influencer data
-- Run with: psql "$DATABASE_URL" -f drills/fun-stats.sql

\echo '==================================================================='
\echo '              INFLUENCER INTELLIGENCE FUN STATS'
\echo '==================================================================='
\echo ''

\echo '--- 📊 Overall Statistics ---'
SELECT 
    (SELECT count(*) FROM influencers) as total_influencers,
    (SELECT count(*) FROM raw_signals) as total_signals,
    (SELECT count(*) FROM sources) as total_sources,
    (SELECT count(DISTINCT source_id) FROM raw_signals WHERE source_id IS NOT NULL) as active_sources;

\echo ''
\echo '--- 🏆 Top 5 Most Active Influencers (by signal count) ---'
SELECT 
    i.instagram_handle,
    i.name,
    count(s.id) as signal_count,
    max(s.captured_at) as most_recent_signal
FROM influencers i
LEFT JOIN raw_signals s ON i.id = s.influencer_id
GROUP BY i.id, i.instagram_handle, i.name
ORDER BY signal_count DESC, most_recent_signal DESC NULLS LAST
LIMIT 5;

\echo ''
\echo '--- 📅 Activity by Day of Week ---'
SELECT 
    to_char(captured_at, 'Day') as day_of_week,
    count(*) as signal_count,
    round(count(*) * 100.0 / sum(count(*)) OVER (), 2) as percentage
FROM raw_signals
GROUP BY to_char(captured_at, 'Day'), extract(dow from captured_at)
ORDER BY extract(dow from captured_at);

\echo ''
\echo '--- ⏰ Activity by Hour of Day ---'
SELECT 
    extract(hour from captured_at) as hour,
    count(*) as signal_count,
    repeat('█', (count(*) / (max(count(*)) OVER ()) * 50)::int) as bar
FROM raw_signals
GROUP BY extract(hour from captured_at)
ORDER BY hour;

\echo ''
\echo '--- 📈 Signals Over Time (last 30 days by day) ---'
SELECT 
    date_trunc('day', captured_at)::date as day,
    count(*) as signals,
    repeat('▓', (count(*) / (max(count(*)) OVER ()) * 40)::int) as activity_bar
FROM raw_signals
WHERE captured_at > now() - interval '30 days'
GROUP BY date_trunc('day', captured_at)
ORDER BY day DESC
LIMIT 30;

\echo ''
\echo '--- 🎯 Signal Distribution by Source Type ---'
SELECT 
    COALESCE(src.kind, 'unknown') as source_type,
    count(*) as signal_count,
    round(count(*) * 100.0 / sum(count(*)) OVER (), 2) as percentage
FROM raw_signals sig
LEFT JOIN sources src ON sig.source_id = src.id
GROUP BY src.kind
ORDER BY signal_count DESC;

\echo ''
\echo '--- 💤 Influencers Who Need Some Love (never scraped or oldest scrape) ---'
SELECT 
    instagram_handle,
    name,
    COALESCE(to_char(last_scraped_at, 'YYYY-MM-DD HH24:MI'), 'never') as last_scraped,
    CASE 
        WHEN last_scraped_at IS NULL THEN 'waiting for first scrape'
        WHEN last_scraped_at < now() - interval '30 days' THEN 'gathering dust'
        WHEN last_scraped_at < now() - interval '7 days' THEN 'could use a refresh'
        ELSE 'recently updated'
    END as status
FROM influencers
ORDER BY last_scraped_at NULLS FIRST
LIMIT 5;

\echo ''
\echo '--- 🔥 Recent Activity (last 24 hours) ---'
WITH recent AS (
    SELECT count(*) as recent_signals
    FROM raw_signals
    WHERE captured_at > now() - interval '24 hours'
)
SELECT 
    recent_signals,
    CASE 
        WHEN recent_signals = 0 THEN '😴 quiet time'
        WHEN recent_signals < 10 THEN '📝 chill vibes'
        WHEN recent_signals < 50 THEN '🔥 things are heating up'
        WHEN recent_signals < 100 THEN '⚡ absolutely buzzing'
        ELSE '🚀 going absolutely feral'
    END as vibe_check
FROM recent;

\echo ''
\echo '==================================================================='
\echo '                      Stats complete!'
\echo '==================================================================='
