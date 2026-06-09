# FC Bot — Runbook

Operations guide for the FC Transfer Market automation bot.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Environment Configuration](#3-environment-configuration)
4. [First-Time Setup](#4-first-time-setup)
5. [Running the Bot](#5-running-the-bot)
6. [Adding a New EA Account](#6-adding-a-new-ea-account)
7. [Managing Clients](#7-managing-clients)
8. [Configuring the Target Card](#8-configuring-the-target-card)
9. [Accounting Reports](#9-accounting-reports)
10. [Common Errors and Fixes](#10-common-errors-and-fixes)

---

## 1. Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11 or higher |
| Chromium (via Playwright) | installed separately (see below) |
| Internet access | required for EA API and Telegram |

The bot uses a real Chromium browser (headless) only for the initial login.
All subsequent API calls use lightweight async HTTP (httpx).

---

## 2. Installation

```bash
# 1. Clone / copy the project
cd fc_bot

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
playwright install chromium
```

---

## 3. Environment Configuration

Copy the template and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set every variable.  See the table below for a description
of each key:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | Yes | Comma-separated Telegram user IDs of admins |
| `EA_ACCOUNT_1_EMAIL` | Yes | EA account email |
| `EA_ACCOUNT_1_PASSWORD` | Yes | EA account password |
| `EA_ACCOUNT_1_OTP_KEY` | Yes | Base-32 TOTP secret from EA 2FA setup |
| `DB_PATH` | No | SQLite file path (default: `data/fc_bot.db`) |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |
| `MAX_CLIENTS_PER_ACCOUNT` | No | Max clients per EA account (default: `5`) |
| `REQUEST_DELAY_MIN` | No | Min seconds between HTTP requests (default: `0.5`) |
| `REQUEST_DELAY_MAX` | No | Max seconds between HTTP requests (default: `2.0`) |

### Getting the OTP setup key

1. Log into your EA account at [ea.com](https://www.ea.com)
2. Go to **Security** → **Login verification**
3. Enable **Authenticator App**
4. EA shows a QR code **and** a text key underneath it
5. Copy the **text key** (looks like `JBSWY3DPEHPK3PXP`) — this is your `OTP_KEY`
6. Also add this key to your authenticator app so you can log in manually

---

## 4. First-Time Setup

Run the bot once to initialise the database and log all EA accounts in:

```bash
python main.py
```

The startup sequence:
1. Creates `data/fc_bot.db` with all tables
2. Seeds EA accounts from `.env`
3. For each account: loads saved session or runs Playwright login
4. Playwright opens a headless Chromium browser, logs in, handles 2FA
5. Saves the session token to the database
6. Starts worker queues and the scheduler
7. Starts Telegram polling

A successful start looks like:

```
2024-01-15 10:00:00 [INFO] __main__: === FC Bot starting (all phases) ===
2024-01-15 10:00:00 [INFO] db.database: Database schema ready
2024-01-15 10:00:01 [INFO] queue.manager: QueueManager starting…
2024-01-15 10:00:01 [INFO] auth.login: Starting login for account 1 (you@example.com)
2024-01-15 10:00:15 [INFO] auth.login: Login successful for account 1
2024-01-15 10:00:15 [INFO] queue.worker: Worker account=1 started
2024-01-15 10:00:15 [INFO] queue.manager: QueueManager ready: 1 worker(s), 0 order(s) re-queued
2024-01-15 10:00:15 [INFO] __main__: Bot polling started — press Ctrl+C to stop
```

---

## 5. Running the Bot

### Normal start

```bash
python main.py
```

### As a background process (Linux)

```bash
nohup python main.py > logs/fc_bot.log 2>&1 &
echo $! > bot.pid
```

To stop:

```bash
kill $(cat bot.pid)
```

### With systemd (Linux server)

Create `/etc/systemd/system/fcbot.service`:

```ini
[Unit]
Description=FC Card Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/fc_bot
EnvironmentFile=/home/ubuntu/fc_bot/.env
ExecStart=/home/ubuntu/fc_bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fcbot
sudo systemctl start fcbot
sudo systemctl status fcbot
```

---

## 6. Adding a New EA Account

1. **Add the account to `.env`:**

   ```dotenv
   EA_ACCOUNT_2_EMAIL=account2@example.com
   EA_ACCOUNT_2_PASSWORD=password2
   EA_ACCOUNT_2_OTP_KEY=BASE32KEYFORACCCOUNT2
   ```

2. **Restart the bot.**  On startup, `QueueManager.start()` detects the new
   account, runs Playwright login, and starts a worker for it.

3. **Verify the worker started** in the logs:

   ```
   [INFO] queue.manager: Worker started for account 2 (account2@example.com)
   ```

The new account is now available to accept clients.  Up to
`MAX_CLIENTS_PER_ACCOUNT` clients can be assigned to it via `/addclient`.

---

## 7. Managing Clients

All client management is done via Telegram commands sent by an admin.

### Register a new client

```
/addclient 123456789
```

The bot assigns the client to the least-loaded active EA account (the one
with the fewest current clients, as long as it is below the
`MAX_CLIENTS_PER_ACCOUNT` limit).

Reply:
```
✅ Client 123456789 registered.
Assigned to account: account1@example.com
```

### Remove a client

```
/removeclient 123456789
```

The bot sends a confirmation keyboard before removing.  Existing orders for
the client are **not** cancelled — they continue to completion.

### List all clients

```
/clients
```

Shows each client's Telegram ID, assigned account email, and latest order
status.

---

## 8. Configuring the Target Card

All card configuration applies to one **active card** at a time.  Setting a
new card with `/setcard` deactivates the previous one.

### Set the target card

```
/setcard 70000000001 Mbappé OVR 99
```

- First argument: EA resource ID (from the transfer market URL or data tools)
- Remaining text: card display name (for reports and notifications)

### Set prices

```
/setbuyprice 4800        — max price to search and buy at
/setlistprice 5500       — buy-now price when listing
/setstartbid 4500        — opening auction bid (must be below list price)
/setmaxcards 200         — maximum cards per single order
```

### Typical workflow for a new card

```
/setcard 70000000001 Mbappe 99
/setbuyprice 4800
/setlistprice 5500
/setstartbid 4300
/setmaxcards 200
```

After this, clients can place orders immediately.

---

## 9. Accounting Reports

### Automatic report (every 9 hours)

The scheduler fires automatically.  Each completed, unaccounted order
generates one report message sent to all admins in this format:

```
📊 Accounting Report

Client: 123456789
Card: Mbappe 99
Cards bought: 47
Avg bought price: 4,750
List price: 5,500
Profit per card: 475
Total profit: 22.33
Completed: 2024-01-15 18:30 UTC
```

### Manual report

```
/report
```

Runs the accounting immediately.  Processes all completed orders since the
last report and marks them so they are not counted again.

---

## 10. Common Errors and Fixes

### `No valid session — logging in…` on every startup

The session TTL is 1 hour.  If the bot was stopped for more than 1 hour,
it re-logs in on next startup automatically.  This is expected behaviour.

### `Login FAILED — check credentials / OTP key`

Causes and fixes:

| Cause | Fix |
|---|---|
| Wrong password in `.env` | Update `EA_ACCOUNT_N_PASSWORD` |
| OTP key is wrong or has spaces | Re-copy the key from EA (no spaces) |
| EA account requires a captcha | Log in manually once to clear, then restart bot |
| EA changed their login page HTML | Update selectors in `auth/login.py` |

### `Session expired (401)` in logs

Normal — the worker automatically re-logs in and continues.  If it happens
repeatedly (more than 3 times per hour), EA may have changed the session
format.  Check `auth/login.py` for `_capture_ut_session`.

### `All EA accounts are at maximum capacity`

All `EA_ACCOUNT_N_*` accounts already have `MAX_CLIENTS_PER_ACCOUNT`
clients each.  Either:
- Add a new EA account (see section 6)
- Increase `MAX_CLIENTS_PER_ACCOUNT` in `.env` (not recommended above 10)

### `Order #N failed: no listings found after 5 retries`

The card at the configured price has no listings on the transfer market.
Ask an admin to increase `/setbuyprice`.

### `Order #N: item bought but listing failed`

The card was purchased and is now in the account's item pile (unassigned
items).  The admin receives a Telegram notification.  Fix:
1. Log into the FC Web App manually
2. Go to **Items** → **Transfer List**
3. List the card at the correct price

### Scheduler not firing

APScheduler logs the job start:
```
[INFO] scheduler.runner: Scheduled accounting job triggered
```
If you don't see this after 9 hours:
1. Check `logs/fc_bot.log` for `APScheduler` errors
2. Ensure the bot process is still running (`ps aux | grep main.py`)

### `worker.queue` grows indefinitely

Symptom: clients place orders but they queue up without processing.
Cause: the account's worker task crashed silently.
Fix: restart the bot — on startup all pending orders are re-queued and
workers are recreated fresh.

---

## File Structure Reference

```
fc_bot/
├── main.py              — entry point
├── config.py            — reads .env
├── requirements.txt
├── .env                 — secrets (never commit)
├── .env.example         — template
├── data/
│   └── fc_bot.db        — SQLite database (auto-created)
├── logs/
│   └── fc_bot.log       — rotating log (auto-created)
├── auth/
│   ├── login.py         — Playwright login flow
│   ├── otp.py           — TOTP generation
│   └── session.py       — session storage and validation
├── market/
│   ├── buyer.py         — search + buy
│   ├── lister.py        — list on transfer market
│   ├── tradepile.py     — read tradepile
│   └── models.py        — CardListing, BuyResult, ListResult
├── bot/
│   ├── __init__.py      — Bot + Dispatcher setup
│   ├── router_admin.py  — admin commands
│   ├── router_client.py — client commands
│   ├── notifications.py — outbound messages
│   ├── middlewares.py   — IsAdmin, IsClient filters
│   └── keyboards.py     — inline keyboards
├── queue/
│   ├── worker.py        — per-account order processor
│   └── manager.py       — worker lifecycle
├── scheduler/
│   ├── accounting.py    — accounting logic
│   └── runner.py        — APScheduler job
├── db/
│   └── database.py      — all DB queries
├── utils/
│   ├── logger.py        — logging setup
│   └── delays.py        — human_delay()
├── docs/
│   └── RUNBOOK.md       — this file
└── test_e2e.py          — manual end-to-end smoke test
```
