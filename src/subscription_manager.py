"""
Wallet Subscription Manager
--------------------------
Lightweight SQLite-based subscription system supporting:
- /watch <address>   Subscribe to wallet monitoring
- /unwatch <address> Unsubscribe
- /status            View subscription list
- Passive auto-notification when a subscribed address is sandwiched
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import List, Optional

from .i18n import t

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "subscriptions.db",
)


class SubscriptionManager:
    """Wallet subscription manager."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database tables."""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     TEXT    NOT NULL,
                    address     TEXT    NOT NULL,
                    ens_name    TEXT,
                    created_at  TEXT    NOT NULL,
                    UNIQUE(chat_id, address)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------
    def subscribe(self, chat_id: str, address: str,
                  ens_name: Optional[str] = None) -> str:
        """Subscribe to a wallet address. Returns a user-facing message."""
        address = address.strip().lower()
        if not address.startswith("0x") or len(address) != 42:
            return f"❌ {t('en', 'subscription.invalid_address')}"

        created = datetime.now().isoformat()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO subscriptions "
                    "(chat_id, address, ens_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (str(chat_id), address, ens_name, created),
                )
            label = f"{ens_name} ({address[:8]}...)" if ens_name else f"{address[:10]}..."
            return f"✅ {t('en', 'subscription.subscribed_prefix')} {label} — {t('en', 'subscription.subscribed')}"
        except Exception as e:
            logger.error("Subscription failed: %s", e)
            return f"❌ {t('en', 'subscription.subscribe_failed')}: {e}"

    def unsubscribe(self, chat_id: str, address: str) -> str:
        """Unsubscribe from a wallet address."""
        address = address.strip().lower()
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=? AND address=?",
                (str(chat_id), address),
            )
            if cur.rowcount > 0:
                return f"✅ {t('en', 'subscription.unsubscribed')} {address[:10]}..."
            return f"⚠️ {t('en', 'subscription.not_found')}"

    def get_subscriptions(self, chat_id: str) -> List[dict]:
        """Get all subscriptions for a given chat/group."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE chat_id=? ORDER BY created_at DESC",
                (str(chat_id),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Passive detection: check if an address is subscribed
    # ------------------------------------------------------------------
    def find_subscribers(self, address: str) -> List[dict]:
        """Find all chat_ids subscribed to a given address."""
        address = address.strip().lower()
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE address=?",
                (address,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_subscribers_by_ens(self, ens_name: str) -> List[dict]:
        """Find subscribers by ENS name."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE ens_name=? COLLATE NOCASE",
                (ens_name.lower(),),
            ).fetchall()
            return [dict(r) for r in rows]

    def format_status(self, chat_id: str) -> str:
        """Format subscription list for Bot command reply."""
        subs = self.get_subscriptions(chat_id)
        if not subs:
            return f"📋 {t('en', 'subscription.status_empty')}"

        lines = [f"📋 {t('en', 'subscription.status_header')} ({len(subs)})\n"]
        for i, s in enumerate(subs, 1):
            addr = s["address"]
            label = s.get("ens_name") or f"{addr[:10]}...{addr[-6:]}"
            lines.append(f"{i}. {label}")
        lines.append(f"\n{t('en', 'subscription.status_footer')}")
        return "\n".join(lines)

    def increment_stat(self, key: str):
        """Increment a statistics counter."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO stats (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
                (key,),
            )

    def get_stat(self, key: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM stats WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else 0
