================================================================================
                          FC BOT — EA FC ULTIMATE TEAM TRADING AUTOMATION
================================================================================

OVERVIEW
--------
FC Bot is a Telegram-based automation system for the EA Sports FC Companion
Web App (Ultimate Team transfer market). An admin manages a pool of EA
accounts entirely from Telegram: accounts are added conversationally, logged
in and out with clickable buttons, and orders are placed with a simple
/order command — the bot buys cards from the transfer market and relists
them, managing the whole buy/list cycle through an asynchronous queue with
persistent browser sessions, price escalation logic, and periodic profit
accounting.

Multiple EA accounts can be logged in simultaneously; the admin chooses
which account processes each order.


KEY FEATURES
------------
  - Admin-only operation  : No client management — the admin drives
                            everything from Telegram.
  - Conversational setup  : /addaccount asks for email, password, and backup
                            code step by step; credential messages are
                            deleted from the chat immediately.
  - Clickable login flow  : /accounts lists accounts as buttons; tap one,
                            confirm, and the bot logs in. /logout mirrors
                            the same flow for logging out.
  - Backup-code 2FA       : First login uses the (single-use) backup code
                            once, then persists the browser profile. Later
                            logins restore silently — no code needed.
  - Persistent browsers   : Each account owns a persistent Chrome profile
                            (data/profiles/{id}); admin-chosen accounts stay
                            logged in and are auto-restored after a restart.
  - Session management    : Expired sessions (HTTP 401) trigger an automatic
                            password-only re-login using the remembered
                            device — backup codes are never auto-respent.
  - Transfer market API   : Searches, buys, moves to tradepile, and lists
                            cards via EA's internal httpx-based API.
  - Multi-account orders  : When several accounts are logged in, /order asks
                            which account should process the order.
  - Price escalation      : If no cards are found in the configured range,
                            the max buy price is raised by 50 coins per step
                            up to a hard ceiling of 950 coins before aborting.
  - Smart card locking    : Locks onto a card's resourceId and buys as many
                            copies as possible before switching.
  - Stale-listing guard   : Already-bought/unavailable trade IDs are never
                            re-bid within an order.
  - Crash recovery        : Pending orders are re-queued when their account
                            logs (back) in and resume from the last listed
                            card. Orders placed while an account is logged
                            out wait in the DB until it logs in.
  - Browser health checks : Crashed browsers are relaunched automatically
                            from the same on-disk profile.
  - 9-hour accounting     : An APScheduler job sends profit reports to all
                            admins every 9 hours (or on demand via /report).


SYSTEM REQUIREMENTS
-------------------
  - Python 3.11 or later
  - Playwright (Chromium) for browser-based EA login
  - A Telegram bot token (from @BotFather)
  - EA accounts with 2FA backup codes available


INSTALLATION
------------
1. Clone the repository and enter it.

2. Create and activate a virtual environment:

       python -m venv venv
       venv\Scripts\activate        # Windows
       source venv/bin/activate     # Linux / macOS

3. Install dependencies:

       pip install -r requirements.txt
       playwright install chromium

4. Copy the environment template and fill in your values:

       copy .env.example .env      # Windows
       cp .env.example .env        # Linux / macOS

5. Run the bot:

       python main.py

6. In Telegram, send /addaccount to the bot and follow the prompts.


CONFIGURATION (.env)
--------------------
  TELEGRAM_BOT_TOKEN     Bot token from @BotFather
  ADMIN_IDS              Comma-separated Telegram user IDs of admins
                         (get yours from @userinfobot)

  DB_PATH                SQLite database file. Default: data/fc_bot.db
  LOG_LEVEL              DEBUG / INFO / WARNING / ERROR. Default: INFO

  PROFILES_DIR           Persistent Chrome profiles dir. Default: data/profiles
  BROWSER_HEADLESS       true/false — show browser windows. Default: false
  BROWSER_HEALTH_CHECK_INTERVAL_S
                         Seconds between browser health checks. Default: 300

  REQUEST_DELAY_MIN/MAX  Random pause between EA API requests. Default 0.5–2.0

  EA account credentials are NOT stored in .env — accounts are added from
  Telegram via /addaccount and stored in the database.


TELEGRAM COMMANDS (all admin-only)
----------------------------------
  ACCOUNTS
  --------
  /addaccount         Add an EA account. The bot asks step by step:
                        Enter your email:    → user@example.com
                        Enter your password: → (message auto-deleted)
                        Enter your backup code: → (message auto-deleted)
                        ✅ account created
  /accounts           List accounts as buttons (🟢 logged in / ⚪ out).
                      Tap one → "Login as X? Yes/No" → logs in and keeps
                      the account logged in until you log it out.
  /logout             Same pattern for logging out a logged-in account.
  /removeaccount {id} Disable an account (also logs it out).
  /cancel             Abort the current conversation (e.g. mid /addaccount).

  TRADING
  -------
  /setcard "Name" r_min r_max buy_min buy_max list_price
                      Configure which card to trade and at what prices.
  /listcards          List all configured cards
  /removecard {id}    Deactivate a card configuration
  /order {amount}     Place an order (100k, 1m, or plain integer).
                        - no account logged in → asks you to /accounts first
                        - one account logged in → goes straight to it
                        - several logged in → asks which account to use
  /report             Trigger an immediate accounting report


HOW IT WORKS — DATA FLOW
-------------------------
1. Admin adds accounts with /addaccount (stored in SQLite).
2. Admin logs chosen accounts in via /accounts. First login per account
   opens a Chrome window, enters email + password + backup code, ticks
   "remember this device", and saves the persistent profile. The backup
   code is wiped from the DB after use (it is single-use).
3. Admin configures the card with /setcard and places orders with /order.
4. The chosen account's worker searches the market, buys the cheapest
   matching card, moves it to the tradepile, lists it, records a
   transaction, and notifies the admin — repeating until the order is done.
5. Every 9 hours (or on /report), admins receive profit reports.


PROJECT STRUCTURE
-----------------
  main.py                 Entry point — starts all subsystems
  config.py               Loads .env and exports all settings

  auth/
    login.py              Playwright EA login: first_login (backup code),
                          restore_session, password_relogin
    session.py            SessionData: headers, cookies, TTL validation

  browser_pool/
    pool.py               BrowserPool — persistent Chrome contexts,
                          admin-driven login/logout, health checks
    profiles.py           Per-account profile directories

  market/
    buyer.py              search_card() / buy_card()
    lister.py             move_to_tradepile() / list_card()
    player_names.py       Player ID → name resolution (players.json)
    models.py             CardListing, BuyResult, ListResult dataclasses

  bot/
    router_admin.py       All Telegram commands (admin-only) incl. the
                          /addaccount FSM and clickable login/logout flows
    notifications.py      Outbound messages (cards listed, reports)
    middlewares.py        IsAdmin guard
    keyboards.py          Inline keyboards (account pickers, confirmations)

  order_queue/
    manager.py            QueueManager — worker lifecycle, order routing
    worker.py             OrderWorker — the buy/list loop per EA account

  db/
    database.py           SQLite schema, legacy migration, all CRUD

  scheduler/
    runner.py             APScheduler setup (9-hour job)
    accounting.py         Profit calculation and admin reporting

  utils/                  Logging + human-like delays
  logs/  data/            Runtime output (auto-created)


DATABASE SCHEMA
---------------
  ea_accounts     id, email, password, backup_code, status, is_logged_in,
                  first_login_done, profile_path, session columns,
                  created_at, updated_at
  cards           Card specs: name, rating range, price range, list price
  orders          account_id, card_id, quantity, order_amount, status,
                  ordered_by (admin Telegram ID), accounted, created_at
  transactions    One row per successfully listed card

  Legacy databases (with otp_key / clients / orders.client_id) are migrated
  automatically on first start.


SECURITY NOTES
--------------
  - EA passwords must be replayed to EA's login form, so they cannot be
    hash-stored. They are kept in the local SQLite file; encryption at rest
    (Fernet) is a planned enhancement.
  - Credential messages sent during /addaccount are deleted from the chat
    immediately after being read.
  - Backup codes are single-use: consumed on first login, then wiped from
    the database.
  - Only Telegram IDs listed in ADMIN_IDS can interact with the bot at all.


STARTUP SEQUENCE
----------------
When python main.py is run:

  1. Logging initialized (console + logs/fc_bot.log)
  2. SQLite database created / migrated
  3. BrowserPool restores every account that was logged in before the
     restart (persistent profiles — no credentials re-entered)
  4. QueueManager spawns a worker per restored account and re-queues their
     surviving pending orders
  5. APScheduler started (9-hour accounting job)
  6. Telegram command menu registered; polling starts
  On Ctrl+C: workers cancelled, browsers closed, scheduler stopped.


LIMITATIONS & NOTES
-------------------
  - EA may change their API or login flow at any time; selectors in
    auth/login.py may need updates. The backup-code screen selectors are
    best-effort until verified against a live account.
  - Player name resolution depends on EA's players.json (see
    PLAYERS_JSON_FILE note in market/player_names.py).
  - EA enforces tradepile size limits per account.
  - Using EA's API outside their official apps may violate EA's Terms of
    Service. Use at your own discretion and risk.

================================================================================
