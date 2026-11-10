# -*- coding: utf-8 -*-
"""Phase 1 数据管道：SQLite 连接 + schema 初始化。

标准库 sqlite3，不引入 ORM。data/ads.db 默认路径，可通过 config.yaml 的
管道.db_path 覆盖。WAL 模式允许多进程并发读 + 单写者，适合本地计划任务场景。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = CODE_DIR / "schema.sql"          # 代码自带的表结构，不随站点变

# 多站点：见 rules.py 里同名注释。只影响默认的 data/ads.db 路径(没人传 db_path 时)。
HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else CODE_DIR


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else HERE / "data" / "ads.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()
