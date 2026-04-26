# EventAlpha - Complete Setup & Deployment Guide

## Table of Contents
1. [Local Setup (5 minutes)](#1-local-setup)
2. [Supabase Database (3 minutes)](#2-supabase-database-free)
3. [Discord Alerts (1 minute)](#3-discord-alerts)
4. [Telegram Alerts (2 minutes)](#4-telegram-alerts)
5. [NewsAPI (1 minute)](#5-newsapi-optional)
6. [Deploy to Render.com (5 minutes)](#6-deploy-to-rendercom-free)
7. [Keep It Alive](#7-keep-it-alive)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Local Setup

### Prerequisites
- Python 3.9+ installed
- Git installed
- Internet connection

### Steps

```bash
# 1. Clone or navigate to the project
cd Event-Trad

# 2. Install dependencies (one command)
pip install -r requirements.txt

# 3. Copy the env template
cp .env.example .env

# 4. Initialize the database (creates local SQLite)
python scraper/database_schema.py

# 5. Start the app (this starts API + frontend + scraper)
python backend/api.py
```

**Open http://localhost:5000** in your browser. Done.

The scraper runs automatically every 10 minutes. First run starts immediately on app launch.

### What Happens on Startup
1. Flask starts serving the frontend on port 5000
2. SQLite database is created at `data/eventalpha.db`
3. APScheduler kicks off the scraper immediately
4. Scraper fetches RSS feeds + Google News + stock data from yfinance
5. NLP engine extracts events, classifies sentiment, detects macro themes
6. Alpha scoring engine scores each signal (8-factor formula)
7. Everything is written to the database
8. Frontend auto-refreshes every 60 seconds

---

## 2. Supabase Database (Free)

**Why?** Local SQLite works for development, but Render.com's free tier wipes files on restart. Supabase gives you free persistent PostgreSQL.

### Steps

1. Go to **https://supabase.com** and sign up (use GitHub login)
2. Click **"New Project"**
   - Name: `eventalpha`
   - Password: choose a strong password (you'll need it)
   - Region: pick closest to you
   - Click **Create**
3. Wait ~2 minutes for project to initialize
4. Go to **Project Settings** (gear icon, bottom left)
5. Click **Database** in the left menu
6. Under **Connection string**, find the **URI** tab
7. Copy the connection string. It looks like:
   ```
   postgresql://postgres.[PROJECT-ID]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres
   ```
8. Open your `.env` file and paste it:
   ```
   DATABASE_URL=postgresql://postgres.xxxx:YOUR_PASSWORD@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
   ```
9. Run the schema migration:
   ```bash
   python scraper/database_schema.py
   ```
   You should see: `Database schema initialized successfully!` and `Using: PostgreSQL`

### Supabase Free Tier Limits
- 500 MB storage (plenty for signals)
- 2 GB bandwidth/month
- Pauses after 1 week of inactivity (auto-resumes on next request)

---

## 3. Discord Alerts

Discord uses **webhooks** - no bot token needed. Webhooks are one-way: EventAlpha posts alerts to your Discord channel.

### Steps

1. Open **Discord** desktop or web
2. Go to the server where you want alerts
3. Click the **channel name** > **Edit Channel** (gear icon)
4. Go to **Integrations** > **Webhooks**
5. Click **"New Webhook"**
6. Name it `EventAlpha` and pick an avatar if you want
7. Click **"Copy Webhook URL"**
8. Two options to save it:
   - **Option A (recommended):** Open EventAlpha > Alerts page > paste in "Discord Webhook URL" field > Save
   - **Option B:** Add to `.env` file:
     ```
     DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/abcdef...
     ```

### What Alerts Look Like
Discord embeds with:
- Ticker + Alpha Score in the title
- Color-coded: green (bullish), red (bearish)
- Event type, confidence, entry price
- Predicted returns for 1D, 3D, 20D

---

## 4. Telegram Alerts

Telegram requires a **bot** (free, takes 2 minutes via @BotFather).

### Step 1: Create the Bot

1. Open **Telegram** (phone or desktop)
2. Search for **@BotFather** and open it
3. Send: `/newbot`
4. BotFather asks for a name - type: `EventAlpha Alerts`
5. BotFather asks for a username - type something unique: `eventalpha_yourname_bot`
6. BotFather gives you a token like:
   ```
   7123456789:AAHbcDefGhIjKlMnOpQrStUvWxYz
   ```
   **Copy this token.**

### Step 2: Get Your Chat ID

1. Open the bot you just created in Telegram (search for its username)
2. Send it any message (just type `hello`)
3. Open this URL in your browser (replace TOKEN with your actual token):
   ```
   https://api.telegram.org/bot7123456789:AAHbcDefGhIjKlMnOpQrStUvWxYz/getUpdates
   ```
4. In the JSON response, find `"chat":{"id":` - that number is your chat ID
   ```json
   "chat":{"id":987654321,"first_name":"Your Name"...}
   ```
   **Copy the number** (e.g., `987654321`)

### Step 3: Save Config

- **Option A (recommended):** Open EventAlpha > Alerts page > paste Bot Token + Chat ID > Save
- **Option B:** Add to `.env`:
  ```
  TELEGRAM_BOT_TOKEN=7123456789:AAHbcDefGhIjKlMnOpQrStUvWxYz
  TELEGRAM_CHAT_ID=987654321
  ```

### Test It
Go to Alerts page > click **"Test Notification"**. You should get a test message in both Discord and Telegram within seconds.

---

## 5. NewsAPI (Optional)

Adds broader financial news coverage beyond RSS feeds. Free tier: 100 requests/day.

1. Go to **https://newsapi.org/register**
2. Sign up with email
3. Copy your API key from the dashboard
4. Add to `.env`:
   ```
   NEWSAPI_KEY=your_api_key_here
   ```

The scraper automatically uses NewsAPI when the key is present.

---

## 6. Deploy to Render.com (Free)

### Prerequisites
- Code pushed to a **GitHub repository**
- Supabase DATABASE_URL ready (from step 2)

### Steps

1. Go to **https://render.com** and sign up (use GitHub login)
2. Click **"New +"** > **"Web Service"**
3. Connect your GitHub repo (you may need to grant access)
4. Select your EventAlpha repo
5. Configure:
   - **Name:** `eventalpha` (this becomes your URL: eventalpha.onrender.com)
   - **Runtime:** `Python`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn backend.api:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
   - **Plan:** `Free`
6. Click **"Advanced"** and add environment variables:

   | Key | Value |
   |-----|-------|
   | `DATABASE_URL` | Your Supabase connection string |
   | `SECRET_KEY` | Any random string (e.g., `myapp-secret-2024`) |
   | `SCRAPER_INTERVAL` | `10` |
   | `NEWSAPI_KEY` | *(optional)* Your NewsAPI key |
   | `DISCORD_WEBHOOK_URL` | *(optional)* Your Discord webhook |
   | `TELEGRAM_BOT_TOKEN` | *(optional)* Your Telegram bot token |
   | `TELEGRAM_CHAT_ID` | *(optional)* Your Telegram chat ID |

7. Click **"Create Web Service"**
8. Wait 3-5 minutes for the first build and deploy
9. Your app is live at: **https://eventalpha.onrender.com**

### Auto-Deploy
Every time you `git push` to your repo, Render automatically redeploys.

---

## 7. Keep It Alive

Render's free tier **sleeps after 15 minutes of inactivity**. The scraper won't run while sleeping. Two solutions:

### Option A: Free Cron Ping (Recommended)

1. Go to **https://cron-job.org** (free, no card required)
2. Sign up and create a new cron job:
   - **URL:** `https://eventalpha.onrender.com/api/health`
   - **Schedule:** Every 14 minutes
   - **OR** Custom: only during market hours (Mon-Fri 9:00-16:30 IST)
3. This keeps the service awake so the scraper runs on schedule

### Option B: UptimeRobot

1. Go to **https://uptimerobot.com** (free tier: 50 monitors)
2. Add HTTP monitor for `https://eventalpha.onrender.com/api/health`
3. Check interval: 5 minutes

---

## 8. Troubleshooting

### "No data showing on the frontend"
- The scraper needs 30-60 seconds to complete its first run
- Check `/api/scraper/status` to see if it ran
- Check `/api/health` to verify the server is up

### "Database connection failed"
- Verify your `DATABASE_URL` starts with `postgresql://` (not `postgres://`)
- Check Supabase project is not paused (visit Supabase dashboard)
- The connection string password must not contain special characters that need URL-encoding

### "Scraper fails with yfinance error"
- yfinance occasionally gets rate-limited by Yahoo
- The scraper will retry on next scheduled run
- Check logs: `scraper/logs/scraper.log`

### "Discord/Telegram alerts not working"
- Go to Alerts page > click "Test Notification"
- Discord: verify webhook URL starts with `https://discord.com/api/webhooks/`
- Telegram: verify you messaged the bot at least once before testing
- Check the bot token format: `NUMBER:ALPHANUMERIC_STRING`

### "Render build fails"
- Check that `requirements.txt` is in the repo root (not in a subfolder)
- The build log in Render dashboard shows the exact error
- Common: Python version mismatch - add `PYTHON_VERSION=3.11.0` env var

### "Map shows no markers"
- Markers come from geo events detected by the scraper
- Geo events are only created when articles mention countries/regions
- Check `/api/geoevents` to see if data exists

---

## Architecture Summary

```
Your Browser
    |
    v
[Render.com - Free Web Service]
    |
    |-- GET / .............. Serves frontend (HTML/JS/CSS)
    |-- GET /api/signals ... Returns trading signals
    |-- GET /api/events .... Returns news events
    |-- GET /api/geoevents . Returns map markers
    |-- GET /api/dashboard . Returns stats
    |-- POST /api/notifications/config . Save Discord/Telegram settings
    |
    |-- [APScheduler - every 10 min]
    |       |
    |       v
    |   [Scraper Pipeline]
    |       |-- RSS Feeds (10+ sources, free)
    |       |-- Google News RSS (free)
    |       |-- NewsAPI (optional, 100 req/day free)
    |       |-- yfinance (32 NSE stocks, free)
    |       |
    |       v
    |   [NLP Engine]
    |       |-- Event classification (7 types)
    |       |-- Entity-aware sentiment analysis
    |       |-- Macro theme detection (war, oil, rates, etc.)
    |       |-- Macro → Sector → Stock expansion
    |       |
    |       v
    |   [Alpha Scoring Engine - 8 factors]
    |       |-- Base score + Event weight + Sentiment
    |       |-- Timing + Quality + Sector momentum
    |       |-- Relative strength + Divergence bonus
    |       |
    |       v
    |   [Notifications]
    |       |-- Discord webhook
    |       |-- Telegram bot
    |
    v
[Supabase PostgreSQL - Free]
    |-- signals, events, predictions
    |-- geo_events, market_regimes
    |-- notification_config, watchlist, alerts
```

---

## Quick Reference

| Action | Command/URL |
|--------|-------------|
| Start locally | `python backend/api.py` |
| Open app | `http://localhost:5000` |
| Check health | `/api/health` |
| See scraper status | `/api/scraper/status` |
| Trigger scraper manually | `POST /api/scraper/run` |
| View all signals | `/api/signals` |
| View all events | `/api/events` |
| View geo events | `/api/geoevents` |
| Dashboard stats | `/api/dashboard` |
