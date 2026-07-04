import logging
import time
import aiosqlite

from config import DB_PATH
from utils.redact import redact_email

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ea_accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT    NOT NULL UNIQUE,
    password            TEXT    NOT NULL,
    backup_code         TEXT,
    status              TEXT    NOT NULL DEFAULT 'active',
    is_logged_in        INTEGER NOT NULL DEFAULT 0,
    first_login_done    INTEGER NOT NULL DEFAULT 0,
    profile_path        TEXT,
    session_token       TEXT,
    session_data        TEXT,
    session_created_at  REAL,
    created_at          REAL    NOT NULL DEFAULT (unixepoch()),
    updated_at          REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name     TEXT    NOT NULL,
    card_id       TEXT,
    resource_id   INTEGER,
    min_rating    INTEGER,
    max_rating    INTEGER,
    rarity_ids    TEXT    NOT NULL DEFAULT '1',
    lev           TEXT    NOT NULL DEFAULT 'gold',
    buy_price     INTEGER NOT NULL DEFAULT 0,
    buy_price_min INTEGER NOT NULL DEFAULT 0,
    buy_price_max INTEGER NOT NULL DEFAULT 0,
    list_price    INTEGER NOT NULL DEFAULT 0,
    start_bid     INTEGER NOT NULL DEFAULT 150,
    max_cards     INTEGER NOT NULL DEFAULT 200,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES ea_accounts(id),
    card_id       INTEGER NOT NULL REFERENCES cards(id),
    quantity      INTEGER NOT NULL,
    order_amount  INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'pending',
    accounted     INTEGER NOT NULL DEFAULT 0,
    ordered_by    INTEGER,
    created_at    REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    card_name       TEXT    NOT NULL,
    player_name     TEXT,
    position        TEXT,
    bought_price    INTEGER NOT NULL,
    listed_price    INTEGER NOT NULL,
    buynow_price    INTEGER NOT NULL,
    listed_at       REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS dsfut_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id      INTEGER UNIQUE,
    trade_id            TEXT,
    name                TEXT,
    rating              INTEGER,
    position            TEXT,
    start_price         INTEGER,
    buy_now_price       INTEGER,
    amount              REAL,
    net_price           REAL,
    expires             INTEGER,
    console             TEXT,
    account_email       TEXT,
    account_password    TEXT,
    account_backup_code TEXT,
    ea_account_id       INTEGER REFERENCES ea_accounts(id),
    status              TEXT    NOT NULL DEFAULT 'new',
    raw_json            TEXT,
    created_at          REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_dsfut_orders_status
    ON dsfut_orders (status, created_at);
"""

# Additive migrations for databases created before new columns were added.
_MIGRATIONS = [
    "ALTER TABLE cards ADD COLUMN start_bid   INTEGER NOT NULL DEFAULT 150",
    "ALTER TABLE cards ADD COLUMN is_active   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN order_amount INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN accounted   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN ordered_by  INTEGER",
    "ALTER TABLE cards ADD COLUMN resource_id   INTEGER",
    "ALTER TABLE cards ADD COLUMN min_rating    INTEGER",
    "ALTER TABLE cards ADD COLUMN max_rating    INTEGER",
    "ALTER TABLE cards ADD COLUMN rarity_ids    TEXT NOT NULL DEFAULT '1'",
    "ALTER TABLE cards ADD COLUMN lev           TEXT NOT NULL DEFAULT 'gold'",
    "ALTER TABLE cards ADD COLUMN buy_price_min INTEGER DEFAULT 700",
    "ALTER TABLE cards ADD COLUMN buy_price_max INTEGER DEFAULT 850",
    "ALTER TABLE cards ADD COLUMN buy_price INTEGER NOT NULL DEFAULT 0",
    "UPDATE cards SET buy_price_max = buy_price WHERE buy_price_max = 0 AND buy_price > 0",
    "ALTER TABLE transactions ADD COLUMN player_name TEXT",
    "ALTER TABLE transactions ADD COLUMN position TEXT",
]


async def _table_columns(db: aiosqlite.Connection, table: str) -> list[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        return [row[1] for row in await cur.fetchall()]


async def _migrate_legacy(db: aiosqlite.Connection) -> None:
    """
    Rebuild tables whose old shape blocks the new model:
      - ea_accounts had `otp_key NOT NULL` (OTP removed) — rebuild without it.
      - orders had `client_id NOT NULL` (clients removed) — rebuild without it;
        old orders keep running with ordered_by = NULL (no client notification).
      - clients table is dropped entirely.
    """
    def _col(cols: list[str], name: str, default_sql: str) -> str:
        # Old DBs may predate a column entirely — fall back to a literal.
        return name if name in cols else default_sql

    acct_cols = await _table_columns(db, "ea_accounts")
    if "otp_key" in acct_cols:
        logger.info("Migrating legacy ea_accounts table (dropping otp_key)…")
        now = time.time()
        await db.execute("ALTER TABLE ea_accounts RENAME TO ea_accounts_old")
        await db.executescript(_SCHEMA)  # recreate ea_accounts with new shape
        await db.execute(
            f"""
            INSERT INTO ea_accounts
                (id, email, password, backup_code, status, first_login_done,
                 profile_path, session_token, session_data, session_created_at,
                 created_at, updated_at)
            SELECT id, email,
                   COALESCE({_col(acct_cols, 'password', 'NULL')}, ''),
                   {_col(acct_cols, 'backup_code', 'NULL')},
                   status,
                   COALESCE({_col(acct_cols, 'first_login_done', 'NULL')}, 0),
                   {_col(acct_cols, 'profile_path', 'NULL')},
                   session_token, session_data, session_created_at,
                   ?, ?
              FROM ea_accounts_old
            """,
            (now, now),
        )
        await db.execute("DROP TABLE ea_accounts_old")

    order_cols = await _table_columns(db, "orders")
    if "client_id" in order_cols:
        logger.info("Migrating legacy orders table (dropping client_id)…")
        await db.execute("ALTER TABLE orders RENAME TO orders_old")
        await db.executescript(_SCHEMA)  # recreate orders with new shape
        await db.execute(
            f"""
            INSERT INTO orders
                (id, account_id, card_id, quantity, order_amount, status,
                 accounted, ordered_by, created_at)
            SELECT id, account_id, card_id, quantity,
                   COALESCE({_col(order_cols, 'order_amount', 'NULL')}, 0),
                   status,
                   COALESCE({_col(order_cols, 'accounted', 'NULL')}, 0),
                   NULL, created_at
              FROM orders_old
            """
        )
        await db.execute("DROP TABLE orders_old")

    await db.execute("DROP TABLE IF EXISTS clients")


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create tables, migrate legacy shapes, run additive migrations."""
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL survives in the DB file and lets the DSFUT poller, the bot and
        # any external consumer of dsfut_orders write concurrently without
        # "database is locked" errors.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(_SCHEMA)
        await _migrate_legacy(db)
        for stmt in _MIGRATIONS:
            try:
                await db.execute(stmt)
            except Exception:
                pass  # column already exists
        await db.commit()
    logger.info("Database schema ready")


# ---------------------------------------------------------------------------
# EA Account helpers
# ---------------------------------------------------------------------------


async def add_account(email: str, password: str, backup_code: str) -> tuple[bool, str]:
    """
    Register a new EA account (used by the /addaccount conversation).
    Returns (True, account_id_str) on success, (False, reason) on failure.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM ea_accounts WHERE email = ?", (email,)
        ) as cur:
            if await cur.fetchone():
                return False, "already_exists"

        cur = await db.execute(
            "INSERT INTO ea_accounts (email, password, backup_code) VALUES (?, ?, ?)",
            (email, password, backup_code),
        )
        new_id = cur.lastrowid
        await db.commit()
    logger.info("Account added via bot: id=%d email=%s", new_id, redact_email(email))
    return True, str(new_id)


async def update_account_credentials(
    email: str, password: str, backup_code: str
) -> int | None:
    """
    Refresh password/backup code of an existing account (a re-popped DSFUT
    order may carry newer credentials). Returns the account id, or None if
    no account with that email exists.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM ea_accounts WHERE email = ?", (email,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        account_id = row[0]
        await db.execute(
            "UPDATE ea_accounts SET password = ?, backup_code = ?, "
            "updated_at = unixepoch() WHERE id = ?",
            (password, backup_code, account_id),
        )
        await db.commit()
    logger.info("Account %d credentials refreshed (email=%s)", account_id, redact_email(email))
    return account_id


async def get_account_by_id(account_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, email, password, backup_code, status, is_logged_in,
                   first_login_done, profile_path, created_at, updated_at
              FROM ea_accounts
             WHERE id = ?
            """,
            (account_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_accounts() -> list[dict]:
    """All active accounts (used by the /accounts login picker)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, email, password, backup_code, status, is_logged_in,
                   first_login_done, profile_path
              FROM ea_accounts
             WHERE status != 'disabled'
             ORDER BY id
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_logged_in_accounts() -> list[dict]:
    """Accounts the admin has logged in (restored on startup; order targets)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, email, password, backup_code, status, is_logged_in,
                   first_login_done, profile_path
              FROM ea_accounts
             WHERE is_logged_in = 1 AND status != 'disabled'
             ORDER BY id
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_logged_in(account_id: int, logged_in: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET is_logged_in = ?, updated_at = unixepoch() WHERE id = ?",
            (1 if logged_in else 0, account_id),
        )
        await db.commit()


async def clear_backup_code(account_id: int) -> None:
    """Backup codes are single-use — wipe after the first successful login."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET backup_code = NULL, updated_at = unixepoch() WHERE id = ?",
            (account_id,),
        )
        await db.commit()


async def mark_first_login_done(account_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET first_login_done = 1, updated_at = unixepoch() WHERE id = ?",
            (account_id,),
        )
        await db.commit()


async def set_profile_path(account_id: int, profile_path: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET profile_path = ?, updated_at = unixepoch() WHERE id = ?",
            (profile_path, account_id),
        )
        await db.commit()


async def set_account_status(account_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET status = ?, updated_at = unixepoch() WHERE id = ?",
            (status, account_id),
        )
        await db.commit()


async def disable_account(account_id: int) -> str:
    """Soft-disable an account. Returns 'ok' or 'not_found'."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM ea_accounts WHERE id = ?", (account_id,)
        ) as cur:
            if not await cur.fetchone():
                return "not_found"
        await db.execute(
            "UPDATE ea_accounts SET status = 'disabled', is_logged_in = 0, "
            "updated_at = unixepoch() WHERE id = ?",
            (account_id,),
        )
        await db.commit()
    logger.info("Account %d disabled via bot", account_id)
    return "ok"


async def list_accounts_overview() -> list[dict]:
    """All accounts with login state — for the /accounts listing."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, email, status, is_logged_in, first_login_done,
                   created_at, updated_at
              FROM ea_accounts
             ORDER BY id
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Card configuration
# ---------------------------------------------------------------------------


async def set_card(
    card_name: str,
    min_rating: int,
    max_rating: int,
    buy_price_min: int,
    buy_price_max: int,
    list_price: int,
) -> int:
    """
    Deactivate all existing cards and insert a new active one.

    start_bid is calculated automatically as floor(list_price * 0.95).
    rarity_ids and lev default to '1' / 'gold' (Rare Gold category).
    Returns the new card's row id.
    """
    start_bid = (int(list_price * 0.95) // 100) * 100
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE cards SET is_active = 0")
        cur = await db.execute(
            """
            INSERT INTO cards
                (card_name, card_id, min_rating, max_rating, rarity_ids, lev,
                 buy_price, buy_price_min, buy_price_max, list_price, start_bid, max_cards, is_active)
            VALUES (?, ?, ?, ?, '1', 'gold', ?, ?, ?, ?, ?, 200, 1)
            """,
            (card_name, card_name, min_rating, max_rating,
             buy_price_max, buy_price_min, buy_price_max, list_price, start_bid),
        )
        new_id = cur.lastrowid
        await db.commit()
    logger.info(
        "Card set: id=%d %s rating=%d-%d buy=%d-%d list=%d start_bid=%d",
        new_id, card_name, min_rating, max_rating,
        buy_price_min, buy_price_max, list_price, start_bid,
    )
    return new_id


async def list_all_cards() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, card_name, min_rating, max_rating, rarity_ids, lev,
                   buy_price_min, buy_price_max, list_price, start_bid, is_active
              FROM cards
             ORDER BY id DESC
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def deactivate_card(card_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE cards SET is_active = 0 WHERE id = ?", (card_id,)
        )
        await db.commit()
    return cur.rowcount > 0


async def get_active_card() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cards WHERE is_active = 1 LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


async def create_order(
    account_id: int,
    ordered_by: int,
    order_amount: int,
) -> dict | None:
    """
    Create a pending order on *account_id*, placed by admin *ordered_by*
    (Telegram ID — receives the per-card and completion notifications).

    Derives quantity from the active card's list_price and max_cards.
    Returns the new order as a dict, or None on failure.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT id, list_price, max_cards FROM cards WHERE is_active = 1 LIMIT 1"
        ) as cur:
            card = await cur.fetchone()
        if not card or card["list_price"] <= 0:
            return None

        quantity = min(order_amount // card["list_price"], card["max_cards"])
        if quantity < 1:
            return None

        cur = await db.execute(
            """
            INSERT INTO orders
                (account_id, card_id, quantity, order_amount, status, ordered_by)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (account_id, card["id"], quantity, order_amount, ordered_by),
        )
        order_id = cur.lastrowid
        await db.commit()

        async with db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()

    return dict(row) if row else None


async def update_order_status(order_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
        await db.commit()


async def get_order_by_id(order_id: int) -> dict | None:
    """Return the raw orders row for an order (used by manager.add_order)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_pending_orders() -> list[dict]:
    """Every order in pending or in_progress state, oldest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, account_id
              FROM orders
             WHERE status IN ('pending', 'in_progress')
             ORDER BY created_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_pending_orders_for_account(account_id: int) -> list[dict]:
    """
    Pending/in-progress orders for one account, oldest first.
    Used to re-queue surviving orders when the admin logs that account in.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, account_id
              FROM orders
             WHERE account_id = ?
               AND status IN ('pending', 'in_progress')
             ORDER BY created_at ASC
            """,
            (account_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_order_with_card(order_id: int) -> dict | None:
    """
    Return a full order row joined with its card config plus the ordering
    admin's Telegram ID.  This is the primary data source for the worker.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                o.id            AS order_id,
                o.account_id,
                o.quantity,
                o.order_amount,
                o.status,
                o.ordered_by,
                cd.card_name,
                cd.resource_id,
                cd.min_rating,
                cd.max_rating,
                cd.rarity_ids,
                cd.lev,
                cd.buy_price_min,
                cd.buy_price_max,
                cd.list_price,
                cd.start_bid
              FROM orders o
              JOIN cards   cd ON cd.id = o.card_id
             WHERE o.id = ?
            """,
            (order_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


async def save_transaction(
    order_id: int,
    card_name: str,
    player_name: str,
    bought_price: int,
    listed_price: int,
    buynow_price: int,
    position: str | None = None,
) -> None:
    """Persist one completed buy+list cycle to the transactions table."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO transactions
                (order_id, card_name, player_name, bought_price, listed_price, buynow_price, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, card_name, player_name, bought_price, listed_price, buynow_price, position),
        )
        await db.commit()


async def get_transactions_for_order(order_id: int) -> list[dict]:
    """Return all transaction rows for *order_id*, oldest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transactions WHERE order_id = ? ORDER BY listed_at ASC",
            (order_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_transactions_for_order(order_id: int) -> int:
    """How many cards have already been bought for this order (restart safety)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE order_id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Scheduler / accounting
# ---------------------------------------------------------------------------


async def get_unaccounted_orders() -> list[dict]:
    """
    Return completed (status='done'), not-yet-accounted orders joined with
    their transaction aggregates.  The returned dicts match exactly the
    structure expected by bot.notifications.send_accounting_report().
    Oldest first so reports arrive in chronological order.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                o.id                             AS order_id,
                o.ordered_by                     AS telegram_id,
                cd.card_name,
                COUNT(t.id)                      AS card_count,
                CAST(AVG(t.bought_price) AS INT) AS avg_bought_price,
                MAX(t.listed_price)              AS listed_price,
                o.order_amount,
                o.created_at                     AS completed_at
              FROM orders o
              JOIN cards        cd ON cd.id = o.card_id
              JOIN transactions t  ON t.order_id = o.id
             WHERE o.status     = 'done'
               AND o.accounted  = 0
          GROUP BY o.id
          ORDER BY o.created_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_order_accounted(order_id: int) -> None:
    """Set accounted=1 so this order is never included in future reports."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET accounted = 1 WHERE id = ?", (order_id,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# DSFUT orders
#
# Rows are written by dsfut.poller and consumed by a separate downstream
# process. Status lifecycle: 'new' → 'claimed' (downstream) / 'deleted'
# (manual review). Credentials live only in this table — never in logs.
# ---------------------------------------------------------------------------


async def insert_dsfut_order(
    *,
    transaction_id: int | None,
    trade_id: str,
    name: str,
    rating: int | None,
    position: str,
    start_price: int | None,
    buy_now_price: int | None,
    amount: float | None,
    net_price: float | None,
    expires: int | None,
    console: str,
    account_email: str,
    account_password: str,
    account_backup_code: str,
    raw_json: str,
) -> int | None:
    """
    Store one popped order with status 'new'.
    Returns the new row id, or None when this transaction_id already exists
    (duplicate pop after a restart).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO dsfut_orders
                (transaction_id, trade_id, name, rating, position,
                 start_price, buy_now_price, amount, net_price, expires,
                 console, account_email, account_password,
                 account_backup_code, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (transaction_id, trade_id, name, rating, position,
             start_price, buy_now_price, amount, net_price, expires,
             console, account_email, account_password,
             account_backup_code, raw_json),
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
        new_id = cur.lastrowid
    logger.info(
        "DSFUT order stored: id=%d transaction=%s player=%s account=%s",
        new_id, transaction_id, name, redact_email(account_email),
    )
    return new_id


async def insert_dsfut_stub_order(
    *,
    order_id: str | None,
    platform: str,
    coins: int | None,
    amount: float | None,
    raw_json: str,
) -> int | None:
    """
    Record a picked-up order with only what the board row exposed (no
    credentials yet — that is a later step). The full scraped row is kept in
    raw_json so the follow-up detail step never depends on this column mapping.

    Uses the site order id as transaction_id (when numeric) so a re-pick of the
    same order is ignored rather than duplicated. Returns the new row id, or
    None if it was already stored.
    """
    tx_id = int(order_id) if order_id is not None and str(order_id).isdigit() else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO dsfut_orders
                (transaction_id, trade_id, buy_now_price, net_price,
                 console, status, raw_json)
            VALUES (?, ?, ?, ?, ?, 'new', ?)
            """,
            (tx_id, str(order_id) if order_id is not None else None,
             coins, amount, platform, raw_json),
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
        new_id = cur.lastrowid
    logger.info(
        "DSFUT stub order stored: id=%d order=%s platform=%s coins=%s",
        new_id, order_id, platform, coins,
    )
    return new_id


async def link_dsfut_order_account(order_id: int, ea_account_id: int) -> None:
    """Point a stored DSFUT order at the ea_accounts row created for it."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(
            "UPDATE dsfut_orders SET ea_account_id = ? WHERE id = ?",
            (ea_account_id, order_id),
        )
        await db.commit()


async def claim_next_dsfut_order() -> dict | None:
    """
    Atomically claim the oldest 'new' DSFUT order: status 'new' → 'claimed'
    and the full row is returned. Executed as a single UPDATE … RETURNING
    statement, so two concurrent consumers can never grab the same row
    (SQLite allows one writer at a time; WAL + busy_timeout make the loser
    wait instead of erroring). Returns None when nothing is pending.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000")
        async with db.execute(
            """
            UPDATE dsfut_orders
               SET status = 'claimed'
             WHERE id = (SELECT id FROM dsfut_orders
                          WHERE status = 'new'
                          ORDER BY created_at ASC, id ASC
                          LIMIT 1)
            RETURNING *
            """
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
    return dict(row) if row else None


async def mark_dsfut_order_deleted(order_id: int) -> bool:
    """Manual review: never let a consumer touch this row again."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        cur = await db.execute(
            "UPDATE dsfut_orders SET status = 'deleted' WHERE id = ?",
            (order_id,),
        )
        await db.commit()
    return cur.rowcount > 0


async def list_dsfut_orders(
    include_deleted: bool = False, limit: int = 100
) -> list[dict]:
    """Recent DSFUT orders, newest first. 'deleted' rows hidden by default."""
    where = "" if include_deleted else "WHERE status != 'deleted'"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT id, transaction_id, trade_id, name, rating, position,
                   start_price, buy_now_price, amount, net_price, expires,
                   console, account_email, ea_account_id, status, created_at
              FROM dsfut_orders
              {where}
             ORDER BY created_at DESC, id DESC
             LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
