# Fun Features

Because data analysis should spark joy! This document covers the entertaining additions to the sysdesign project.

## 🎉 What's New

### 1. 🥚 Hidden Easter Egg

Try `curl http://localhost:8000/teapot` for a surprise! This implements the famous HTTP 418 "I'm a teapot" status code from RFC 2324. It's hidden from the OpenAPI docs for the joy of discovery.

### 2. `/vibes` API Endpoint

A creative, Spotify-Wrapped-style endpoint that gives you entertaining insights about your influencer data.

**Usage:**

```bash
curl http://localhost:8000/vibes
```

**What you get:**

- Total signals and influencer counts
- Energy level assessment (from "hibernating" to "absolutely feral")
- Most active influencer with a fun vibe description
- Creative vibe check based on recent activity
- Fun facts about your data

**Example response:**

```json
{
  "total_signals": 4523,
  "total_influencers": 5,
  "most_active_influencer": {
    "handle": "example_creator",
    "name": "Example Creator",
    "signal_count": 1234,
    "vibe": "@example_creator is absolutely carrying the team"
  },
  "vibe_check": "the timeline is absolutely BUZZING with 89 signals this week",
  "fun_fact": "the group chat is POPPIN",
  "energy_level": "buzzing"
}
```

### 3. Vibes CLI Tool

A colorful command-line tool that fetches and displays your vibes in style.

**Usage:**

```bash
moon run core:vibes

# Or with custom API URL:
uv run python -m common.vibes_cli --api-url http://localhost:8000
```

**Output:**

```
============================================================
           ✨ INFLUENCER VIBES CHECK ✨
============================================================

📊 Total Signals: 4,523
👥 Total Influencers: 5
⚡ Energy Level: BUZZING
💭 Fun Fact: the group chat is POPPIN

------------------------------------------------------------
🎯 VIBE CHECK
------------------------------------------------------------
   the timeline is absolutely BUZZING with 89 signals this week

------------------------------------------------------------
🏆 MOST ACTIVE INFLUENCER
------------------------------------------------------------
   @example_creator (Example Creator)
   1,234 signals
   @example_creator is absolutely carrying the team

============================================================
```

### 4. Fun Statistics SQL Drills

A comprehensive SQL script that generates entertaining visualizations and insights directly in your terminal.

**Usage:**

```bash
moon run core:fun-stats

# Or directly:
psql "$DATABASE_URL" -f packages/core/drills/fun-stats.sql
```

**What you get:**

- 📊 Overall statistics
- 🏆 Top 5 most active influencers
- 📅 Activity by day of week
- ⏰ Activity by hour of day (with ASCII bar chart!)
- 📈 30-day activity timeline
- 🎯 Signal distribution by source
- 💤 Influencers who need attention
- 🔥 Recent 24-hour activity vibe check

## 🎨 The Vibe Vocabulary

Your data gets described with terms like:

- "absolutely unhinged"
- "giving main character energy"
- "the vibe is immaculate"
- "chef's kiss perfection"
- "absolutely sending it"
- And more!

## 🚀 Quick Start

1. Make sure your API is running:
   ```bash
   moon run api:dev
   ```

2. Check your vibes:
   ```bash
   moon run core:vibes
   ```

3. Or see detailed stats:
   ```bash
   moon run core:fun-stats
   ```

## 📝 Notes

- The `/vibes` endpoint is fully integrated into the OpenAPI spec at `/docs`
- Vibes are calculated in real-time from your actual data
- The CLI tool works with any API URL (local or deployed)
- Fun stats require a populated database (run `moon run core:seed` first)

## 🎯 Why Though?

Because monitoring dashboards don't have to be boring. Data should be delightful. And sometimes you just need to know if your influencers are "absolutely sending it" or "in their bag."

Plus, it's a great example of:
- Adding new endpoints to a FastAPI app
- Creating CLI tools with Python
- Writing fun SQL queries with visualizations
- Making data analysis entertaining

Enjoy the vibes! ✨
