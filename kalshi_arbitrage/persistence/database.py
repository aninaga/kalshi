"""
Database Layer

SQLite-based persistence for trades, opportunities, and system state.
Production systems should upgrade to PostgreSQL.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """Represents an executed trade."""
    id: Optional[int]
    execution_id: str
    opportunity_id: str
    platform: str
    market_id: str
    side: str
    action: str
    size: int
    price: float
    filled_size: int
    avg_price: float
    status: str
    pnl: float
    created_at: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Opportunity:
    """Represents a detected opportunity."""
    id: Optional[int]
    opportunity_id: str
    arb_type: str
    platform: str
    market_id: str
    market_title: str
    profit_pct: float
    total_cost: float
    status: str  # detected, executed, missed, expired
    execution_id: Optional[str]
    detected_at: str
    executed_at: Optional[str]


class Database:
    """
    SQLite database for arbitrage system persistence.

    Tables:
    - trades: Executed trade records
    - opportunities: Detected arbitrage opportunities
    - state: System state (balances, stats)
    - alerts: Alert history
    """

    def __init__(self, db_path: str = "market_data/arbitrage.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        logger.info(f"Database initialized: {db_path}")

    @contextmanager
    def _get_conn(self):
        """Get database connection with context management."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database schema."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Trades table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL,
                    opportunity_id TEXT,
                    platform TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    price REAL NOT NULL,
                    filled_size INTEGER DEFAULT 0,
                    avg_price REAL,
                    status TEXT DEFAULT 'pending',
                    pnl REAL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(execution_id, platform)
                )
            """)

            # Opportunities table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id TEXT UNIQUE NOT NULL,
                    arb_type TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_title TEXT,
                    profit_pct REAL NOT NULL,
                    total_cost REAL,
                    status TEXT DEFAULT 'detected',
                    execution_id TEXT,
                    detected_at TEXT NOT NULL,
                    executed_at TEXT
                )
            """)

            # State table (key-value store)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Alerts table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_type TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data TEXT,
                    acknowledged INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            # Performance metrics table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    tags TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # Create indexes
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_execution
                ON trades(execution_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_opportunities_status
                ON opportunities(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_name_time
                ON metrics(metric_name, created_at)
            """)

    # Trade methods
    def save_trade(self, trade: Trade) -> int:
        """Save a trade record."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO trades
                (execution_id, opportunity_id, platform, market_id, side,
                 action, size, price, filled_size, avg_price, status, pnl, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.execution_id, trade.opportunity_id, trade.platform,
                trade.market_id, trade.side, trade.action, trade.size,
                trade.price, trade.filled_size, trade.avg_price,
                trade.status, trade.pnl, trade.created_at
            ))
            return cursor.lastrowid

    def get_trades(
        self,
        limit: int = 100,
        status: Optional[str] = None
    ) -> List[Trade]:
        """Get recent trades."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            if status:
                cursor.execute("""
                    SELECT * FROM trades
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (status, limit))
            else:
                cursor.execute("""
                    SELECT * FROM trades
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))

            return [Trade(**dict(row)) for row in cursor.fetchall()]

    # Opportunity methods
    def save_opportunity(self, opp: Opportunity) -> int:
        """Save an opportunity record."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO opportunities
                (opportunity_id, arb_type, platform, market_id, market_title,
                 profit_pct, total_cost, status, execution_id, detected_at, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                opp.opportunity_id, opp.arb_type, opp.platform, opp.market_id,
                opp.market_title, opp.profit_pct, opp.total_cost, opp.status,
                opp.execution_id, opp.detected_at, opp.executed_at
            ))
            return cursor.lastrowid

    def update_opportunity_status(
        self,
        opportunity_id: str,
        status: str,
        execution_id: Optional[str] = None
    ):
        """Update opportunity status."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if execution_id:
                cursor.execute("""
                    UPDATE opportunities
                    SET status = ?, execution_id = ?, executed_at = ?
                    WHERE opportunity_id = ?
                """, (status, execution_id, datetime.now(timezone.utc).isoformat(), opportunity_id))
            else:
                cursor.execute("""
                    UPDATE opportunities
                    SET status = ?
                    WHERE opportunity_id = ?
                """, (status, opportunity_id))

    def get_opportunities(
        self,
        limit: int = 100,
        status: Optional[str] = None
    ) -> List[Opportunity]:
        """Get opportunities."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            if status:
                cursor.execute("""
                    SELECT * FROM opportunities
                    WHERE status = ?
                    ORDER BY detected_at DESC
                    LIMIT ?
                """, (status, limit))
            else:
                cursor.execute("""
                    SELECT * FROM opportunities
                    ORDER BY detected_at DESC
                    LIMIT ?
                """, (limit,))

            return [Opportunity(**dict(row)) for row in cursor.fetchall()]

    # State methods
    def set_state(self, key: str, value: Any):
        """Set a state value."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO state (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, json.dumps(value), datetime.now(timezone.utc).isoformat()))

    def get_state(self, key: str, default: Any = None) -> Any:
        """Get a state value."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM state WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row["value"])
            return default

    # Metrics methods
    def record_metric(self, name: str, value: float, tags: Optional[Dict] = None):
        """Record a metric value."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO metrics (metric_name, value, tags, created_at)
                VALUES (?, ?, ?, ?)
            """, (name, value, json.dumps(tags) if tags else None,
                  datetime.now(timezone.utc).isoformat()))

    def get_metrics(
        self,
        name: str,
        hours: int = 24,
        limit: int = 1000
    ) -> List[Dict]:
        """Get recent metrics."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM metrics
                WHERE metric_name = ?
                AND created_at >= datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT ?
            """, (name, f"-{hours} hours", limit))

            return [dict(row) for row in cursor.fetchall()]

    # Alert methods
    def save_alert(
        self,
        alert_type: str,
        priority: str,
        message: str,
        data: Optional[Dict] = None
    ) -> int:
        """Save an alert."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO alerts (alert_type, priority, message, data, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (alert_type, priority, message, json.dumps(data) if data else None,
                  datetime.now(timezone.utc).isoformat()))
            return cursor.lastrowid

    def get_alerts(self, unacknowledged_only: bool = False, limit: int = 50) -> List[Dict]:
        """Get alerts."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            if unacknowledged_only:
                cursor.execute("""
                    SELECT * FROM alerts
                    WHERE acknowledged = 0
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))
            else:
                cursor.execute("""
                    SELECT * FROM alerts
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))

            return [dict(row) for row in cursor.fetchall()]

    # Summary methods
    def get_daily_summary(self) -> Dict:
        """Get daily trading summary."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Trades today
            cursor.execute("""
                SELECT COUNT(*) as count,
                       SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled,
                       SUM(pnl) as total_pnl
                FROM trades
                WHERE created_at >= date('now')
            """)
            trades = dict(cursor.fetchone())

            # Opportunities today
            cursor.execute("""
                SELECT COUNT(*) as count,
                       SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) as executed,
                       AVG(profit_pct) as avg_profit_pct
                FROM opportunities
                WHERE detected_at >= date('now')
            """)
            opps = dict(cursor.fetchone())

            return {
                "trades": trades,
                "opportunities": opps,
                "date": datetime.now(timezone.utc).date().isoformat(),
            }
