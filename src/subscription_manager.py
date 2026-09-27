"""
钱包订阅管理器
--------------
基于 SQLite 的轻量级订阅系统，支持：
- /watch <address>  订阅钱包监控
- /unwatch <address> 取消订阅
- /status           查看订阅列表
- 被动检测到该地址被夹时自动推送通知
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "subscriptions.db",
)


class SubscriptionManager:
    """钱包订阅管理器"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
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
    # 订阅 / 取消订阅
    # ------------------------------------------------------------------
    def subscribe(self, chat_id: str, address: str,
                  ens_name: Optional[str] = None) -> str:
        """订阅一个钱包地址，返回提示消息"""
        address = address.strip().lower()
        if not address.startswith("0x") or len(address) != 42:
            return "❌ 地址格式无效，请输入 0x 开头的 42 位钱包地址"

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
            return f"✅ 已订阅 {label}，当该地址被夹时将自动推送告警"
        except Exception as e:
            logger.error("订阅失败: %s", e)
            return f"❌ 订阅失败: {e}"

    def unsubscribe(self, chat_id: str, address: str) -> str:
        """取消订阅"""
        address = address.strip().lower()
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=? AND address=?",
                (str(chat_id), address),
            )
            if cur.rowcount > 0:
                return f"✅ 已取消订阅 {address[:10]}..."
            return f"⚠️ 未找到 {address[:10]}... 的订阅记录"

    def get_subscriptions(self, chat_id: str) -> List[dict]:
        """获取某个聊天/群的所有订阅"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE chat_id=? ORDER BY created_at DESC",
                (str(chat_id),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 被动检测：检查地址是否被订阅
    # ------------------------------------------------------------------
    def find_subscribers(self, address: str) -> List[dict]:
        """查找订阅了某个地址的所有 chat_id"""
        address = address.strip().lower()
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE address=?",
                (address,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_subscribers_by_ens(self, ens_name: str) -> List[dict]:
        """通过 ENS 名称查找订阅者"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE ens_name=? COLLATE NOCASE",
                (ens_name.lower(),),
            ).fetchall()
            return [dict(r) for r in rows]

    def format_status(self, chat_id: str) -> str:
        """格式化订阅列表用于 Bot 命令回复"""
        subs = self.get_subscriptions(chat_id)
        if not subs:
            return "📋 你还没有订阅任何钱包\n\n使用 /watch <地址> 订阅"

        lines = [f"📋 你的订阅列表（共 {len(subs)} 个）\n"]
        for i, s in enumerate(subs, 1):
            addr = s["address"]
            label = s.get("ens_name") or f"{addr[:10]}...{addr[-6:]}"
            lines.append(f"{i}. {label}")
        lines.append("\n使用 /unwatch <地址> 取消订阅")
        return "\n".join(lines)

    def increment_stat(self, key: str):
        """递增统计计数器"""
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
