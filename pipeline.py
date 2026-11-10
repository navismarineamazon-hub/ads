# -*- coding: utf-8 -*-
"""数据管道：把 rules.py 已经拉到的报表行落一份到 SQLite，供历史/多维分析查询。

只负责"存"，不碰"拉"：`store_reports()` 接收 rules.pull_reports() 已经返回的
`reports` 字典（内存里的中文键 list[dict]），按 digest/start/end 幂等 upsert 进对应
的 fact_* 表 —— 不重新调用 Amazon API，零额外请求、零额外等待。这里不提供任何读路径：
rules.py 每次都直接向 Amazon 要新报告，不读 DB 也不读 JSON 缓存，保证广告指标永远最新；
这份 SQLite 历史纯粹给人工查询/事后分析用。

`enqueue_fetch_tasks()` / `run_worker()` 是任务队列骨架，暂未接入 rules.py 的主流程。

digest / 日期范围的计算公式特意跟 rules.py:pull_reports() 保持一致（同样的
sha1([报告类型, 维度, 列, start, end, 时间单位, 过滤器])[:12]），这样同一次拉取在
JSON 缓存和 DB 里用的是同一个"身份证号"，方便后续核对两边数据是否一致。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def open_conn(cfg: dict, here: Path):
    """管道.双写 开着就开一个 SQLite 连接给调用方复用；关着返回 None。
    打不开(损坏/占用等)只打警告不中断 —— 数据管道从来不是建议生成/执行账本的硬依赖。
    rules.py 和 execute.py 共用这一份，各自传自己的 HERE（测试里会被 patch 到临时目录）。"""
    pipe_cfg = cfg.get("管道") or {}
    if not pipe_cfg.get("双写"):
        return None
    try:
        import db
        db_path = pipe_cfg.get("db_path")
        return db.connect((here / db_path).resolve() if db_path else None)
    except Exception as exc:
        print(f"[管道][警告] 打开 SQLite 失败，本次跳过数据管道: {exc}")
        return None
from datetime import date, timedelta

from ads_api import AdsClient


def iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _date_range(spec: dict) -> tuple[str, str]:
    if spec.get("_start") and spec.get("_end"):
        return spec["_start"], spec["_end"]
    lag = int(spec.get("归因等待天数", 0))
    return iso(int(spec["天数"]) + lag), iso(1 + lag)


def _digest(spec: dict, start: str, end: str) -> str:
    dims = spec["维度"]
    cols = list(spec["列"]) if spec.get("列") else _build_columns(dims, spec.get("指标") or [])
    return hashlib.sha1(json.dumps(
        [spec["报告类型"], dims, cols, start, end, spec.get("时间单位", "SUMMARY"), spec.get("过滤器")],
        sort_keys=True).encode()).hexdigest()[:12]


# 跟 rules.py 的 DIMENSION_COLUMNS / METRIC_MAP 保持同步；只在"列"缺省时才用得到，
# 当前 config.yaml 里除了 位置表现 都显式给了"列"，这里只是兜底别炸。
_DIMENSION_COLUMNS = {
    "campaign": ["campaignId", "campaignName"], "adGroup": ["adGroupId", "adGroupName"],
    "targeting": ["targeting"], "searchTerm": ["searchTerm"], "asin": ["asin"],
    "campaignPlacement": [],
}


def _build_columns(dims: list[str], metrics: list[str]) -> list[str]:
    cols: list[str] = []
    for d in dims:
        for c in _DIMENSION_COLUMNS.get(d, []):
            if c not in cols:
                cols.append(c)
    for m in metrics:
        if m not in cols:
            cols.append(m)
    return cols


def _table_for(spec_name: str) -> str:
    base = spec_name[:-2] if spec_name.endswith("历史") else spec_name
    return {
        "搜索词": "fact_search_term",
        "广告活动": "fact_campaign",
        "AUTO投放": "fact_targeting",
        "BROAD投放": "fact_targeting",
        "商品定位投放": "fact_targeting",
        "位置表现": "fact_placement",
    }[base]


def _row_search_term(r: dict) -> dict:
    return {"campaign_id": str(r.get("campaignId", "")), "campaign_name": r.get("campaignName"),
            "ad_group_id": str(r.get("adGroupId", "")), "ad_group_name": r.get("adGroupName"),
            "keyword": r.get("keyword"), "match_type": r.get("matchType"),
            "search_term": r.get("searchTerm"), "impressions": r.get("impressions"),
            "clicks": r.get("clicks"), "cost": r.get("cost"), "sales7d": r.get("sales7d"),
            "purchases7d": r.get("purchases7d")}


def _row_campaign(r: dict) -> dict:
    return {"campaign_id": str(r.get("campaignId", "")), "campaign_name": r.get("campaignName"),
            "impressions": r.get("impressions"), "clicks": r.get("clicks"), "cost": r.get("cost"),
            "sales7d": r.get("sales7d"), "purchases7d": r.get("purchases7d"),
            "campaign_budget_amount": r.get("campaignBudgetAmount"),
            "campaign_status": r.get("campaignStatus")}


def _row_targeting(r: dict) -> dict:
    return {"campaign_id": str(r.get("campaignId", "")), "campaign_name": r.get("campaignName"),
            "ad_group_id": str(r.get("adGroupId", "")), "ad_group_name": r.get("adGroupName"),
            "keyword_id": str(r.get("keywordId", "")), "keyword_bid": r.get("keywordBid"),
            "keyword_type": r.get("keywordType"), "targeting": r.get("targeting"),
            "keyword": r.get("keyword"), "match_type": r.get("matchType"),
            "ad_keyword_status": r.get("adKeywordStatus"), "impressions": r.get("impressions"),
            "clicks": r.get("clicks"), "cost": r.get("cost"), "sales7d": r.get("sales7d"),
            "purchases7d": r.get("purchases7d")}


def _row_placement(r: dict) -> dict:
    return {"campaign_id": str(r.get("campaignId", "")), "campaign_name": r.get("campaignName"),
            "placement": r.get("placement"), "impressions": r.get("impressions"),
            "clicks": r.get("clicks"), "cost": r.get("cost"), "sales7d": r.get("sales7d"),
            "purchases7d": r.get("purchases7d")}


_ROW_MAPPERS = {
    "fact_search_term": _row_search_term, "fact_campaign": _row_campaign,
    "fact_targeting": _row_targeting, "fact_placement": _row_placement,
}


def store_reports(conn: sqlite3.Connection, profile_id: str, specs: list[dict],
                  reports: dict[str, list[dict]]) -> None:
    """把 rules.pull_reports() 已经拉到的 reports 原样落一份到 SQLite（不重新调 API）。"""
    pulled_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    for spec in specs:
        name = spec["名称"]
        rows = reports.get(name) or []
        start, end = _date_range(spec)
        digest = _digest(spec, start, end)
        report_days = int(spec.get("天数", 1))
        table = _table_for(name)
        mapper = _ROW_MAPPERS[table]
        extra = {"profile_id": profile_id, "report_days": report_days,
                 "start_date": start, "end_date": end, "digest": digest, "pulled_at": pulled_at}
        if table == "fact_targeting":
            extra["spec_name"] = name[:-2] if name.endswith("历史") else name
        mapped = []
        for r in rows:
            row = mapper(r)
            row.update(extra)
            mapped.append(row)
        if mapped:
            cols = list(mapped[0].keys())
            placeholders = ", ".join("?" for _ in cols)
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
            conn.executemany(sql, [[row[c] for c in cols] for row in mapped])
        conn.execute(
            "INSERT INTO fetch_tasks (profile_id, spec_name, digest, start_date, end_date, "
            "report_days, status, attempts, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'DONE', 0, ?) "
            "ON CONFLICT(profile_id, spec_name, digest) DO UPDATE SET "
            "status='DONE', updated_at=excluded.updated_at",
            (profile_id, name, digest, start, end, report_days, pulled_at))
        print(f"[管道] {name}: {len(mapped)} 行写入 {table}")
    conn.commit()


def sync_ledger(conn: sqlite3.Connection, profile_id: str, ledger: dict) -> None:
    """Phase 3 数据管道：把 execute.py 的 execution_ledger.json 镜像进 action_ledger 表。

    JSON ledger 永远是唯一真相源（execute.py 的安全校验/防重放逻辑一个字不改），这里只是
    每次 save_ledger() 落盘时，把当前完整的 ledger 快照同步进 DB：
      - success 里的条目 -> status='SUCCESS'
      - pending 里的条目 -> status='PENDING'
      - DB 里上次还是 PENDING、但这次快照里 success/pending 两边都找不到了的
        -> 说明刚被判定为确认失败并从 pending 弹出了（JSON 那边直接消失不留痕迹），
           这里记成 status='FAILED'，纯增量的审计信息，不影响任何现有判断逻辑。
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    seen_ids: set[str] = set()

    def upsert(action_id: str, status: str, row: dict, event_time: str, file: str | None) -> None:
        seen_ids.add(action_id)
        conn.execute(
            "INSERT INTO action_ledger (action_id, profile_id, status, action_type, level, "
            "campaign_id, target_id, file, row_json, event_time, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(action_id) DO UPDATE SET status=excluded.status, file=excluded.file, "
            "row_json=excluded.row_json, event_time=excluded.event_time, synced_at=excluded.synced_at",
            (action_id, profile_id, status, row.get("动作"), row.get("层级"),
             str(row.get("campaignId") or ""), str(row.get("targetId") or ""),
             file, json.dumps(row, ensure_ascii=False), event_time, now))

    for action_id, entry in (ledger.get("success") or {}).items():
        upsert(action_id, "SUCCESS", entry.get("row") or {}, entry.get("time", now), entry.get("file"))
    for action_id, entry in (ledger.get("pending") or {}).items():
        upsert(action_id, "PENDING", entry.get("row") or {}, entry.get("time", now), None)

    stale = conn.execute(
        "SELECT action_id FROM action_ledger WHERE profile_id=? AND status='PENDING'",
        (profile_id,)).fetchall()
    for row in stale:
        if row["action_id"] not in seen_ids:
            conn.execute("UPDATE action_ledger SET status='FAILED', synced_at=? WHERE action_id=?",
                        (now, row["action_id"]))
    conn.commit()


# ---------------------------------------------------------------------------
# Phase 2 预留：真正的任务队列驱动拉取（当前未接入 rules.py，先搭骨架）。
# ---------------------------------------------------------------------------

def enqueue_fetch_tasks(conn: sqlite3.Connection, profile_id: str, specs: list[dict]) -> None:
    for spec in specs:
        start, end = _date_range(spec)
        digest = _digest(spec, start, end)
        conn.execute(
            "INSERT OR IGNORE INTO fetch_tasks (profile_id, spec_name, digest, start_date, "
            "end_date, report_days, status) VALUES (?, ?, ?, ?, ?, ?, 'PENDING')",
            (profile_id, spec["名称"], digest, start, end, int(spec.get("天数", 1))))
    conn.commit()


def run_worker(conn: sqlite3.Connection, client: AdsClient, profile_id: str,
               specs_by_name: dict[str, dict], polling: dict | None = None) -> None:
    polling = polling or {}
    now = lambda: time.strftime("%Y-%m-%dT%H:%M:%S")

    pending = conn.execute(
        "SELECT * FROM fetch_tasks WHERE profile_id=? AND status='PENDING'", (profile_id,)).fetchall()
    for row in pending:
        spec = specs_by_name[row["spec_name"]]
        dims = spec["维度"]
        cols = list(spec["列"]) if spec.get("列") else _build_columns(dims, spec.get("指标") or [])
        try:
            rid = client.create_report(f"pro1-{row['spec_name']}-{row['start_date']}-{row['end_date']}",
                                       row["start_date"], row["end_date"], spec["报告类型"], dims, cols,
                                       spec.get("时间单位", "SUMMARY"), spec.get("过滤器"))
            conn.execute("UPDATE fetch_tasks SET amazon_report_id=?, status='POLLING', updated_at=? "
                        "WHERE task_id=?", (rid, now(), row["task_id"]))
            conn.commit()
        except Exception as exc:
            conn.execute("UPDATE fetch_tasks SET attempts=attempts+1, last_error=?, updated_at=? "
                        "WHERE task_id=?", (str(exc)[:500], now(), row["task_id"]))
            conn.commit()

    polling_rows = conn.execute(
        "SELECT * FROM fetch_tasks WHERE profile_id=? AND status='POLLING'", (profile_id,)).fetchall()
    if not polling_rows:
        return
    rid_to_tasks: dict[str, list[sqlite3.Row]] = {}
    for row in polling_rows:
        rid_to_tasks.setdefault(row["amazon_report_id"], []).append(row)
    try:
        urls = client.wait_reports(set(rid_to_tasks), timeout_s=int(polling.get("超时秒", 3600)),
                                   poll_s=int(polling.get("初始间隔秒", 30)),
                                   poll_max_s=int(polling.get("最大间隔秒", 120)))
    except Exception as exc:
        for row in polling_rows:
            conn.execute("UPDATE fetch_tasks SET attempts=attempts+1, last_error=?, updated_at=? "
                        "WHERE task_id=?", (str(exc)[:500], now(), row["task_id"]))
        conn.commit()
        return
    downloaded: dict[str, list] = {}
    for rid, tasks in rid_to_tasks.items():
        if rid not in urls:
            continue
        if rid not in downloaded:
            downloaded[rid] = client.download_report(urls[rid])
        for row in tasks:
            spec = specs_by_name[row["spec_name"]]
            rows = [dict(r, _report_days=row["report_days"]) for r in downloaded[rid]]
            store_reports(conn, profile_id, [spec], {row["spec_name"]: rows})
            conn.execute("UPDATE fetch_tasks SET status='DONE', updated_at=? WHERE task_id=?",
                        (now(), row["task_id"]))
    conn.commit()
