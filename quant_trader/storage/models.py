"""
数据持久化 - SQLite存储交易记录和净值
"""
import sqlite3
import datetime
from contextlib import contextmanager
from typing import Optional
import pandas as pd

from config.settings import DB_PATH
from utils.logger import log


class Database:
    """SQLite数据库"""

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path) if db_path else str(DB_PATH)
        self._init_db()

    @contextmanager
    def _get_conn(self):
        """获取数据库连接（自动关闭，修复连接泄漏）"""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """初始化表结构"""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # 交易记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    fees REAL,
                    realized_pnl REAL,
                    reason TEXT,
                    mode TEXT
                )
            """)

            # 每日净值表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_nav (
                    date TEXT PRIMARY KEY,
                    nav REAL NOT NULL,
                    cash REAL NOT NULL,
                    position_value REAL NOT NULL,
                    daily_return REAL,
                    drawdown REAL,
                    total_pnl REAL
                )
            """)

            # 持仓快照表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    shares INTEGER,
                    cost_price REAL,
                    current_price REAL,
                    market_value REAL,
                    profit_pct REAL
                )
            """)

        log.debug(f"数据库初始化完成: {self.db_path}")

    def save_trade(self, trade: dict):
        """保存交易记录"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (time, code, name, side, price, shares, amount, fees, realized_pnl, reason, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("time", datetime.datetime.now().isoformat()),
                trade.get("code", ""),
                trade.get("name", ""),
                trade.get("side", ""),
                trade.get("price", 0),
                trade.get("shares", 0),
                trade.get("amount", 0),
                trade.get("fees", 0),
                trade.get("realized_pnl", 0),
                trade.get("reason", ""),
                trade.get("mode", "paper"),
            ))

    def save_daily_nav(self, date: str, nav: float, cash: float, position_value: float,
                       daily_return: float = 0, drawdown: float = 0, total_pnl: float = 0):
        """保存每日净值"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO daily_nav (date, nav, cash, position_value, daily_return, drawdown, total_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (date, nav, cash, position_value, daily_return, drawdown, total_pnl))

    def save_positions(self, date: str, positions: dict):
        """保存持仓快照（同一日期+代码覆盖旧记录）"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            for code, pos in positions.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO position_snapshot (date, code, name, shares, cost_price, current_price, market_value, profit_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (date, code, pos.name, pos.shares, pos.cost_price,
                      pos.current_price, pos.market_value, pos.profit_pct))

    def get_trade_history(self, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """查询交易历史"""
        with self._get_conn() as conn:
            query = "SELECT * FROM trades WHERE 1=1"
            params = []
            if start_date:
                query += " AND time >= ?"
                params.append(start_date)
            if end_date:
                query += " AND time <= ?"
                params.append(end_date)
            query += " ORDER BY time DESC"
            df = pd.read_sql_query(query, conn, params=params)
        return df

    def get_nav_history(self, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """查询净值历史"""
        with self._get_conn() as conn:
            query = "SELECT * FROM daily_nav WHERE 1=1"
            params = []
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
            query += " ORDER BY date ASC"
            df = pd.read_sql_query(query, conn, params=params)
        return df
