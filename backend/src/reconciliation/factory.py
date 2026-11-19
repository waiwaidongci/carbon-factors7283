"""归因存储工厂：生产 PostgreSQL，本地/测试可切换 SQLite。

PostgreSQL 与业务库共用 DATABASE_URL（psycopg 连接）；
RECON_SQLITE_URL 指向本地文件时走 SQLite，便于无外部依赖自测。
"""
from __future__ import annotations

import os
import sqlite3
from functools import lru_cache

from src.reconciliation.store import SqlStore


@lru_cache(maxsize=1)
def get_store() -> SqlStore:
    sqlite_url = os.getenv('RECON_SQLITE_URL')
    if sqlite_url:
        def connect():
            conn = sqlite3.connect(sqlite_url, timeout=30)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=30000')
            return conn
        return SqlStore(connect, dialect='sqlite')

    import psycopg  # 仅在生产环境需要
    from src.config.settings import DATABASE_URL

    def connect():
        return psycopg.connect(DATABASE_URL.replace('+psycopg', ''), autocommit=False)

    return SqlStore(connect, dialect='postgres')
