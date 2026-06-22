================================================================================
                          FC BOT — EA FC ULTIMATE TEAM TRADING AUTOMATION
================================================================================

OVERVIEW
--------
FC Bot is a Telegram-based automation system for the EA Sports FC Companion
Web App (Ultimate Team transfer market). It allows registered clients to place
coin-amount orders for player cards, which the bot automatically purchases from
the transfer market and relists for resale — managing the entire buy/list cycle
per EA account through an asynchronous queue system with session management,
price escalation logic, and periodic profit accounting.

The bot is designed for multi-account operation: one pool of EA accounts serves
many Telegram clients, with each account handling its clients one order at a
time.


WHO IS THIS FOR?
----------------
- Operators who run EA FC coin services and want to automate repetitive
  buy/list trading cycles.
- Developers looking for a reference implementation of EA FC Companion API
  integration with Playwright-based 2FA login.


KEY FEATURES
------------
  - Automated login      : Full Playwright-driven EA login flow including
                           email, password, and TOTP 2FA — no manual steps.
  - Session management   : Sessions are cached and reused; expired sessions
                           (HTTP 401) automatically trigger a full re-login
                           without dropping the order.
  - Transfer market API  : Searches, buys, moves to tradepile, and lists cards
                           via EA's internal httpx-based API.
  - Price escalation     : If no cards are found within the configured price
                           range, the max buy price is incrementally raised
                           (by 50 coins per step) up to a hard ceiling of 950
                           coins, before aborting the order.
  - Smart card locking   : Once a specific player card is found, the bot locks
                           onto that card's resourceId and buys as many copies
                           as possible before switching to another card in the
                           same rating band.
  - Stale-listing guard  : Trade IDs that have already been purchased or come
                           back unavailable are tracked per order so the bot
                           never re-bids on a stale market listing.
  - Crash recovery       : Transaction records are written per listed card.
                           On restart, pending/in-progress orders are re-queued
                           and resume exactly where they left off.
  - Load-balanced accounts: New clients are automatically assigned to the EA
                           account with the fewest existing clients.
  - Per-card notifications: The client receives a Telegram message each time a
                           card is successfully listed — not just at the end.
  - 9-hour accounting    : An APScheduler job runs every 9 hours to compute
                           and send profit reports to all admins.
  - Multi-account pool   : Unlimited EA accounts can be added via .env triplets;
                           each runs its own independent worker.


SYSTEM REQUIREMENTS
-------------------
  - Python 3.11 or later
  - Playwright (Chromium) for browser-based EA login
  - A Telegram bot token (from @BotFather)
  - One or more EA accounts with 2FA enabled (TOTP/authenticator app)
  - EA FC Companion Web App access on those accounts


INSTALLATION
------------
1. Clone the repository:

       git clone <repo-url>
       cd dooste-koni

2. Create and activate a virtual environment:

       python -m venv venv
       venv\Scripts\activate        # Windows
       source venv/bin/activate     # Linux / macOS

3. Install Python dependencies:

       pip install -r requirements.txt

4. Install Playwright browsers:

       playwright install chromium

5. Copy the environment template and fill in your values:

       copy .env.example .env      # Windows
       cp .env.example .env        # Linux / macOS

   Then open .env and configure it (see CONFIGURATION below).

6. Run the bot:

       python main.py


CONFIGURATION (.env)
--------------------
All settings live in the .env file. Copy .env.example to get started.

  TELEGRAM_BOT_TOKEN          Bot token from @BotFather
  ADMIN_IDS                   Comma-separated Telegram user IDs of admins
                              (e.g. 123456789,987654321). Get your ID from
                              @userinfobot on Telegram.

  EA_ACCOUNT_1_EMAIL          Email address for EA account #1
  EA_ACCOUNT_1_PASSWORD       Password for EA account #1
  EA_ACCOUNT_1_OTP_KEY        Base-32 TOTP secret for EA account #1
                              (shown during 2FA setup — no spaces or dashes)
                              Example: JBSWY3DPEHPK3PXP

  To add more EA accounts, repeat as EA_ACCOUNT_2_*, EA_ACCOUNT_3_*, etc.
  The bot auto-detects how many accounts are configured.

  DB_PATH                     Path to the SQLite database file.
                              Default: data/fc_bot.db (created automatically)

  LOG_LEVEL                   Logging verbosity: DEBUG / INFO / WARNING / ERROR
                              Default: INFO

  MAX_CLIENTS_PER_ACCOUNT     Maximum clients per EA account before a new one
                              is required. Default: 5

  REQUEST_DELAY_MIN           Minimum delay (seconds) between API requests.
                              Default: 0.5
  REQUEST_DELAY_MAX           Maximum delay (seconds) between API requests.
                              Default: 2.0

  PLAYERS_JSON_FILE           (Optional) Local path to EA's players.json file
                              for resolving player names from market data.
                              EA's CDN is bot-protected so the file should be
                              downloaded manually via browser DevTools.
                              Default auto-path: market/players.json
  PLAYERS_JSON_URL            (Optional) CDN URL for players.json fallback.


HOW IT WORKS — DATA FLOW
-------------------------
1. ADMIN SETUP
   An admin registers clients and configures which cards to trade:

       /addclient {telegram_user_id}
           Registers a client and assigns them to the least-loaded EA account.

       /setcard "Card Name" {min_rating} {max_rating} {buy_min} {buy_max} {list_price}
           Configures which card to trade and at what prices.

       /listcards          — List all configured cards
       /removecard {id}    — Remove a card
       /clients            — List all registered clients
       /report             — Trigger an immediate accounting report

2. CLIENT ORDER
   A registered client places an order with a budget in coins:

       /order 100k         — Order with 100,000 coins budget
       /order 500000       — Same, using raw number

   The bot calculates how many cards fit the budget (budget / list_price),
   validates the order, and enqueues it to the client's assigned EA account.

3. AUTOMATED TRADING CYCLE (per card)
   For each card in the order, the OrderWorker:

     a. Searches the EA transfer market for cards within the configured
        rating range and price range.
     b. Locks onto the cheapest matching card and buys it (buy-now).
     c. Moves the card to the tradepile.
     d. Lists it at the configured resale price and start bid.
     e. Saves a transaction record to the database.
     f. Sends the client a Telegram notification ("Card X listed — 3/10").

   This repeats until all cards in the order are listed.

4. COMPLETION & ACCOUNTING
   When an order is fully filled:
     - The client receives a summary with all player names, bought prices,
       and listed prices.
     - Every 9 hours (or on /report), admins receive profit reports for all
       completed but not-yet-reported orders.


TELEGRAM COMMANDS
-----------------
  CLIENT COMMANDS
  ---------------
  /start              Show welcome message and instructions
  /order {amount}     Place an order. Amount can be: 100k, 1m, or a plain
                      integer like 500000.

  ADMIN COMMANDS
  --------------
  /addclient {id}                     Register a Telegram user as a client
  /setcard "Name" r_min r_max         Configure a card to trade:
            buy_min buy_max list_price   - Name: display name (quoted)
                                         - r_min/r_max: rating range
                                         - buy_min/buy_max: buy price range
                                         - list_price: resale price
  /listcards                          List all configured cards
  /removecard {id}                    Delete a card configuration
  /clients                            List all registered clients
  /report                             Trigger immediate accounting report


PROJECT STRUCTURE
-----------------
  main.py                 Entry point — starts all subsystems
  config.py               Loads .env and exports all settings
  .env.example            Configuration template

  auth/
    login.py              Playwright-based EA login with TOTP 2FA
    session.py            SessionData: headers, cookies, TTL validation
    otp.py                TOTP code generation (pyotp wrapper)

  market/
    buyer.py              search_card() and buy_card() via EA transfer market API
    lister.py             move_to_tradepile() and list_card() via auction API
    player_names.py       Resolves player IDs to names from players.json
    models.py             CardListing, BuyResult, ListResult dataclasses

  bot/
    router_admin.py       All admin Telegram commands
    router_client.py      Client commands (/start, /order)
    notifications.py      Outbound message functions to clients and admins
    middlewares.py        IsAdmin / IsClient access guards
    keyboards.py          Inline keyboard helpers

  order_queue/
    manager.py            QueueManager — spawns workers, routes orders
    worker.py             OrderWorker — the core buy/list loop per EA account

  db/
    database.py           SQLite schema and all CRUD operations

  scheduler/
    runner.py             APScheduler setup (9-hour job)
    accounting.py         Profit calculation and admin reporting

  utils/
    logger.py             File + console logging setup
    delays.py             Human-like randomized request pacing

  logs/                   Runtime log output (created automatically)
  data/                   SQLite database file (created automatically)


DATABASE SCHEMA
---------------
  ea_accounts     EA credentials and cached session data per account
  clients         Registered Telegram users, assigned account, configured card
  cards           Card specs: name, rating range, price range, list price
  orders          Client orders: status (pending → in_progress → done/failed),
                  quantity, total budget
  transactions    One row per successfully listed card: player name, bought
                  price, listed price, order ID


RELIABILITY & SAFETY FEATURES
------------------------------
  - Session auto-refresh    : Any HTTP 401 from EA triggers a full re-login
                              before retrying the current operation.
  - Price escalation cap    : Max buy price raises by 50 coins per 3 missed
                              searches, hard ceiling at 950 coins. Order
                              aborts rather than overpaying.
  - Stale listing guard     : Already-bought trade IDs are tracked in memory
                              per order and skipped in future searches.
  - "Never lose a card"     : A bought-but-unlisted card is held and retried
                              for move/list before moving on — orders never
                              silently deliver fewer cards than requested.
  - Crash recovery          : On restart, in-progress orders are re-queued
                              and resume from the last listed card count.
  - Human-like delays       : Randomized 0.5–2.0s pauses between API calls
                              to reduce detection risk.
  - Admin alerts            : Session failures, buy aborts, and listing errors
                              all generate immediate Telegram alerts to admins.
  - Sequential per account  : Each EA account has exactly one worker processing
                              one order at a time — no race conditions.


DEPENDENCIES
------------
  aiogram>=3.7.0          Telegram bot framework (async)
  playwright>=1.40.0      Browser automation for EA login
  httpx>=0.27.0           Async HTTP client for EA API calls
  pyotp>=2.9.0            TOTP 2FA code generation
  aiosqlite>=0.20.0       Async SQLite database access
  python-dotenv>=1.0.0    .env file loading
  apscheduler>=3.10.4     Background job scheduling (accounting)
  pydantic>=2.5.0         Data validation


STARTUP SEQUENCE
----------------
When python main.py is run:

  1. Logging initialized (console + logs/fc_bot.log)
  2. SQLite database created / migrated; EA accounts synced from .env
  3. QueueManager starts one OrderWorker per configured EA account;
     sessions are loaded from DB or a fresh login is performed;
     any pending/in-progress orders from a previous run are re-queued.
  4. APScheduler started (9-hour accounting job)
  5. Telegram bot command menu registered
  6. Telegram polling starts (blocking)
  On Ctrl+C: workers cancelled, scheduler stopped, HTTP session closed.


LIMITATIONS & NOTES
-------------------
  - This bot interacts with EA's internal Companion Web App API. EA may change
    their API endpoints or authentication flow at any time, which may require
    code updates.
  - Player name resolution depends on EA's players.json file. Because EA's CDN
    blocks automated downloads, you should capture the file manually via browser
    DevTools (Network tab) and place it at market/players.json or configure
    PLAYERS_JSON_FILE in .env. Without it, card names fall back to the
    configured display name.
  - EA enforces tradepile size limits per account. Ensure accounts have
    sufficient tradepile capacity for the order sizes your clients place.
  - The bot is designed for personal/commercial use by its operator. Usage of
    EA's API outside of their official apps may violate EA's Terms of Service.
    Use at your own discretion and risk.


GETTING YOUR EA OTP KEY
-----------------------
When setting up 2FA on an EA account:
  1. Enable 2FA in EA account security settings.
  2. When shown the QR code or secret key, copy the Base-32 secret key
     (looks like: JBSWY3DPEHPK3PXP). This is the value for OTP_KEY.
  3. If you only have the QR code, scan it with any authenticator app that
     shows the underlying secret (e.g. andOTP, Aegis Authenticator).


SUPPORT
-------
  For issues, open a GitHub issue or contact the project maintainer.

================================================================================
