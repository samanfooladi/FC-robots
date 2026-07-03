import logging
import time
import aiosqlite

from config import DB_PATH, EA_ACCOUNTS, MAX_CLIENTS_PER_ACCOUNT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ea_accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT    NOT NULL UNIQUE,
    otp_key             TEXT    NOT NULL,
    session_token       TEXT,
    session_data        TEXT,
    session_created_at  REAL,
    status              TEXT    NOT NULL DEFAULT 'active',
    password            TEXT,
    backup_code         TEXT,
    profile_path        TEXT,
    auth_method         TEXT    NOT NULL DEFAULT 'totp',
    first_login_done    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id         INTEGER NOT NULL UNIQUE,
    assigned_account_id INTEGER REFERENCES ea_accounts(id),
    created_at          REAL    NOT NULL DEFAULT (unixepoch())
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
    buy_price_min INTEGER NOT NULL DEFAULT 0,
    buy_price_max INTEGER NOT NULL DEFAULT 0,
    list_price    INTEGER NOT NULL DEFAULT 0,
    start_bid     INTEGER NOT NULL DEFAULT 150,
    max_cards     INTEGER NOT NULL DEFAULT 200,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     INTEGER NOT NULL REFERENCES clients(id),
    account_id    INTEGER NOT NULL REFERENCES ea_accounts(id),
    card_id       INTEGER NOT NULL REFERENCES cards(id),
    quantity      INTEGER NOT NULL,
    order_amount  INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'pending',
    accounted     INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    card_name       TEXT    NOT NULL,
    bought_price    INTEGER NOT NULL,
    listed_price    INTEGER NOT NULL,
    buynow_price    INTEGER NOT NULL,
    listed_at       REAL    NOT NULL DEFAULT (unixepoch())
);
"""

# Additive migrations for databases created before new columns were added.
_MIGRATIONS = [
    "ALTER TABLE cards ADD COLUMN start_bid   INTEGER NOT NULL DEFAULT 150",
    "ALTER TABLE cards ADD COLUMN is_active   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN order_amount INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN accounted   INTEGER NOT NULL DEFAULT 0",
    # Rating / rarity filter columns
    "ALTER TABLE cards ADD COLUMN resource_id   INTEGER",
    "ALTER TABLE cards ADD COLUMN min_rating    INTEGER",
    "ALTER TABLE cards ADD COLUMN max_rating    INTEGER",
    "ALTER TABLE cards ADD COLUMN rarity_ids    TEXT NOT NULL DEFAULT '1'",
    "ALTER TABLE cards ADD COLUMN lev           TEXT NOT NULL DEFAULT 'gold'",
    "ALTER TABLE cards ADD COLUMN buy_price_min INTEGER DEFAULT 700",
    "ALTER TABLE cards ADD COLUMN buy_price_max INTEGER DEFAULT 850",
    # Legacy buy_price column — set_card still writes it, but _SCHEMA no longer
    # defines it, so databases created from the current schema were missing it
    # and every /setcard INSERT failed (leaving the old card config active).
    "ALTER TABLE cards ADD COLUMN buy_price INTEGER NOT NULL DEFAULT 0",
    # Back-fill buy_price_max from the legacy buy_price column for existing rows
    "UPDATE cards SET buy_price_max = buy_price WHERE buy_price_max = 0 AND buy_price > 0",
    # Player name per transaction
    "ALTER TABLE transactions ADD COLUMN player_name TEXT",
    # Player position (e.g. CB, ST) resolved from players.json at list time
    "ALTER TABLE transactions ADD COLUMN position TEXT",
    # Browser pool / persistent-profile login (phase 2 foundation)
    "ALTER TABLE ea_accounts ADD COLUMN password TEXT",
    "ALTER TABLE ea_accounts ADD COLUMN backup_code TEXT",
    "ALTER TABLE ea_accounts ADD COLUMN profile_path TEXT",
    "ALTER TABLE ea_accounts ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'totp'",
    "ALTER TABLE ea_accounts ADD COLUMN first_login_done INTEGER NOT NULL DEFAULT 0",
]

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create tables, run additive migrations, seed EA accounts."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                await db.execute(stmt)
            except Exception:
                pass  # column already exists
        await db.commit()
    logger.info("Database schema ready")
    await _sync_accounts()


async def _sync_accounts() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for acct in EA_ACCOUNTS:
            backup_code = acct.get("backup_code") or None
            auth_method = "backup_code" if (backup_code and not acct["otp_key"]) else "totp"
            await db.execute(
                """
                INSERT INTO ea_accounts (email, otp_key, password, backup_code, auth_method)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    otp_key     = excluded.otp_key,
                    password    = excluded.password,
                    backup_code = excluded.backup_code,
                    auth_method = excluded.auth_method
                """,
                (acct["email"], acct["otp_key"], acct["password"], backup_code, auth_method),
            )
        await db.commit()
    logger.info("Synced %d EA account(s) to database", len(EA_ACCOUNTS))


# ---------------------------------------------------------------------------
# EA Account helpers
# ---------------------------------------------------------------------------


async def get_account_db_id(email: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM ea_accounts WHERE email = ?", (email,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _get_least_loaded_account() -> int | None:
    """Return the id of the active account with the fewest clients, or None if all full."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT a.id, COUNT(c.id) AS client_count
              FROM ea_accounts a
         LEFT JOIN clients c ON c.assigned_account_id = a.id
             WHERE a.status = 'active'
          GROUP BY a.id
            HAVING client_count < ?
          ORDER BY client_count ASC
             LIMIT 1
            """,
            (MAX_CLIENTS_PER_ACCOUNT,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------


async def add_client(telegram_id: int) -> tuple[bool, str]:
    """
    Register a Telegram user as a client and assign them to the least-loaded
    EA account.

    Returns (True, account_email) on success, (False, reason) on failure.
    """
    account_id = await _get_least_loaded_account()
    if account_id is None:
        return False, "all_accounts_full"

    async with aiosqlite.connect(DB_PATH) as db:
        # Check already registered
        async with db.execute(
            "SELECT id FROM clients WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            if await cur.fetchone():
                return False, "already_registered"

        await db.execute(
            "INSERT INTO clients (telegram_id, assigned_account_id) VALUES (?, ?)",
            (telegram_id, account_id),
        )
        await db.commit()

        async with db.execute(
            "SELECT email FROM ea_accounts WHERE id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()

    return True, row[0] if row else ""


async def remove_client(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM clients WHERE telegram_id = ?", (telegram_id,)
        )
        await db.commit()
    return cur.rowcount > 0


async def get_client_by_telegram_id(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT c.id, c.telegram_id, c.assigned_account_id, c.created_at,
                   a.email AS account_email
              FROM clients c
              JOIN ea_accounts a ON a.id = c.assigned_account_id
             WHERE c.telegram_id = ?
            """,
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_all_clients() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT c.telegram_id,
                   a.email AS account_email,
                   (
                       SELECT o.status
                         FROM orders o
                        WHERE o.client_id = c.id
                        ORDER BY o.created_at DESC
                        LIMIT 1
                   ) AS latest_order_status
              FROM clients c
              JOIN ea_accounts a ON a.id = c.assigned_account_id
             ORDER BY c.created_at
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


async def create_order(telegram_id: int, order_amount: int) -> dict | None:
    """
    Create a pending order for *telegram_id* spending *order_amount* coins.

    Derives quantity from the active card's list_price and max_cards:
    each card is listed at list_price, so that is what one card costs
    the client (e.g. order 7600 with list_price 3800 → 2 cards).
    Returns the new order as a dict, or None on failure.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT id, assigned_account_id FROM clients WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            client = await cur.fetchone()
        if not client:
            return None

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
                (client_id, account_id, card_id, quantity, order_amount, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (
                client["id"],
                client["assigned_account_id"],
                card["id"],
                quantity,
                order_amount,
            ),
        )
        order_id = cur.lastrowid
        await db.commit()

        async with db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()

    return dict(row) if row else None


async def get_active_order(telegram_id: int) -> dict | None:
    """Return a pending or in_progress order for this client, if any."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT o.*
              FROM orders o
              JOIN clients c ON c.id = o.client_id
             WHERE c.telegram_id = ?
               AND o.status IN ('pending', 'in_progress')
             LIMIT 1
            """,
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def update_order_status(order_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


async def get_accounting_report() -> list[dict]:
    """
    Return one row per completed order with aggregated transaction data.
    Used by both the /report command and the 9-hour scheduler.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                o.id                            AS order_id,
                c.telegram_id,
                cd.card_name,
                COUNT(t.id)                     AS card_count,
                CAST(AVG(t.bought_price) AS INT) AS avg_bought_price,
                t.listed_price,
                o.order_amount,
                o.created_at                    AS completed_at
              FROM orders o
              JOIN clients c      ON c.id  = o.client_id
              JOIN cards   cd     ON cd.id = o.card_id
              JOIN transactions t ON t.order_id = o.id
             WHERE o.status = 'done'
          GROUP BY o.id
          ORDER BY o.created_at DESC
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Queue / worker helpers
# ---------------------------------------------------------------------------


async def get_all_accounts() -> list[dict]:
    """Return all active EA accounts (credentials + profile state) for manager/pool startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, email, otp_key, password, backup_code,
                   profile_path, auth_method, first_login_done
              FROM ea_accounts
             WHERE status = 'active'
             ORDER BY id
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_first_login_done(account_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET first_login_done = 1 WHERE id = ?",
            (account_id,),
        )
        await db.commit()


async def set_profile_path(account_id: int, profile_path: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET profile_path = ? WHERE id = ?",
            (profile_path, account_id),
        )
        await db.commit()


async def set_account_status(account_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ea_accounts SET status = ? WHERE id = ?",
            (status, account_id),
        )
        await db.commit()


async def get_all_pending_orders() -> list[dict]:
    """
    Return every order in pending or in_progress state, oldest first.
    Used at startup to re-queue orders that survived a bot restart.
    """
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


async def get_order_by_id(order_id: int) -> dict | None:
    """Return the raw orders row for an order (used by manager.add_order)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_order_with_card(order_id: int) -> dict | None:
    """
    Return a full order row joined with its card config and the client's
    Telegram ID.  This is the primary data source for the worker.
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
                c.telegram_id   AS client_telegram_id,
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
              JOIN clients c  ON c.id  = o.client_id
              JOIN cards   cd ON cd.id = o.card_id
             WHERE o.id = ?
            """,
            (order_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


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
                c.telegram_id,
                cd.card_name,
                COUNT(t.id)                      AS card_count,
                CAST(AVG(t.bought_price) AS INT) AS avg_bought_price,
                MAX(t.listed_price)              AS listed_price,
                o.order_amount,
                o.created_at                     AS completed_at
              FROM orders o
              JOIN clients      c  ON c.id  = o.client_id
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
