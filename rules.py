# -*- coding: utf-8 -*-
"""拉取数据 → 按 config.yaml 规则评估 → 生成建议表 (CSV + Markdown)。

入口是 run.py (python run.py suggest), 不要直接跑这个文件。整轮的流程就是
suggest() 从上往下那几步: select → pull → load_context → evaluate → screen →
write_boards。--only <板块> 在第一步 select 就把规则和报表都缩到那一个板块。

只读不改: 本模块对广告账户只做查询和报告, 写操作全部在 execute.py 里且需 --go。

- 阈值放 baseline.yaml (按产品代码可覆盖), 通过变量名注入条件表达式。
- 搜索词行自动附加派生列 acos7d/cpc7d 与内置值 保护词(是否命中保护清单)。
- 未触发任何规则但有花费的搜索词进"观察名单", 只展示不动手。
"""
from __future__ import annotations

import copy
import csv
import ast
import functools
import hashlib
import json
import math
import os
import re
import statistics
import time
from datetime import date, timedelta
from pathlib import Path

import yaml
from optimization import (refresh_pool, sync_keywords, keyword_index, prepare_reports, aggregate,
                          evidence_gate, enrich_harvest, enrich_pt_harvest, cooldown, prune_outputs,
                          read_json, write_json, conf_numeric, compute_joint_bids,
                          ledger_index, history_feedback, filtered_list,
                          PLACEMENT_LABEL_MAP)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ads_api import AdsClient

# 多站点：run.py --site 美/英/加/... 会把 ADS_SITE_DIR 设成 sites/<站点>，这里所有
# 账户/产品相关路径(config.yaml、baseline.yaml、产品线.yaml、output/、data/...)就都
# 换到那个目录；不传 --site 时跟以前完全一样，用脚本自己所在目录。
HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else Path(__file__).resolve().parent

def _campaign_base_bid(cid: str, target_snap: dict, adgroup_snap: dict) -> float | None:
    """从快照估算活动的 base bid (AUTO 活动各 target 有效竞价的中位数，否则广告组默认竞价)。"""
    bids = [float(t.get("bid") or t.get("_effective_bid"))
            for t in target_snap.values()
            if str(t.get("campaignId")) == cid and t.get("state") == "ENABLED"
            and (t.get("bid") or t.get("_effective_bid"))]
    if bids:
        import statistics as _s
        return _s.median(bids)
    dbs = [float(g["defaultBid"])
           for g in adgroup_snap.values()
           if str(g.get("campaignId")) == cid and g.get("defaultBid")]
    return (sum(dbs) / len(dbs)) if dbs else None


def enrich_placement_bid(actions: list, placement_report: list,
                         campaign_snapshot: dict, target_snap: dict, adgroup_snap: dict,
                         default_base: dict, all_base: dict) -> list:
    """用联合优化公式重新计算各位置建议倍率，并附 协同建议 字段说明 base bid 配合方向。"""
    by_cid: dict = {}
    for row in placement_report:
        by_cid.setdefault(str(row.get("campaignId") or ""), []).append(row)

    enriched = []
    for a in actions:
        cid = str(a.get("campaignId") or "")
        campaign = campaign_snapshot.get(cid, {})
        rows = by_cid.get(cid, [])
        base = baseline_env(a.get("产品码", ""), default_base, all_base)
        target_acos = float(base.get("target_acos", 0.3))
        pct_min = int(base.get("placement_pct_min", 0))
        pct_max = int(base.get("placement_pct_max", 300))
        smoothing = float(base.get("placement_smoothing", 0.4))
        min_clicks = int(base.get("placement_min_clicks", 15))
        current_pcts = {adj["predicate"]: int(adj.get("percentage") or 0)
                        for adj in (campaign.get("bidding") or {}).get("adjustments") or []
                        if adj.get("predicate")}
        base_bid = _campaign_base_bid(cid, target_snap, adgroup_snap) or 1.0
        joint = compute_joint_bids(base_bid, rows, current_pcts, target_acos,
                                   smoothing=smoothing, pct_min=pct_min, pct_max=pct_max,
                                   min_clicks=min_clicks)
        if joint and joint.get("pcts"):
            pred = a.get("投放位置", "")
            if pred in joint["pcts"]:
                a["新倍率"] = joint["pcts"][pred]
            delta = joint.get("base_bid_delta_pct", 0)
            a["协同建议"] = (f"base_bid建议{'加' if delta > 0 else '减'}{abs(delta):.1f}%; "
                             + joint.get("reasoning", ""))
        enriched.append(a)
    return enriched


# 报告维度字段 (维度/指标 口径自动展开列名)
DIMENSION_COLUMNS = {
    "campaign": ["campaignId", "campaignName"],
    "adGroup": ["adGroupId", "adGroupName"],
    "targeting": ["targeting"],
    "searchTerm": ["searchTerm"],
    "asin": ["asin"],
    "campaignPlacement": ["placementClassification"],
}
METRIC_MAP = {
    "impressions": "impressions", "clicks": "clicks", "cost": "cost",
    "sales7d": "sales7d", "sales14d": "sales14d", "sales30d": "sales30d",
    "purchases7d": "purchases7d", "purchases14d": "purchases14d", "purchases30d": "purchases30d",
    "campaignBudgetAmount": "campaignBudgetAmount", "campaignStatus": "campaignStatus",
    "topOfSearchImpressionShare": "topOfSearchImpressionShare",
    "keywordId": "keywordId", "keywordBid": "keywordBid", "keywordType": "keywordType",
    "adKeywordStatus": "adKeywordStatus", "targetId": "targetId",
}

# 需要缓存到本地的快照文件 (判定自动广告 + 收割去重 + 预算动作)
CAMPAIGN_SNAPSHOT = HERE / "data" / "campaigns_all.json"
ADGROUP_SNAPSHOT = HERE / "data" / "adgroups_all.json"
TARGET_SNAPSHOT = HERE / "data" / "targets_all.json"
KEYWORD_SNAPSHOT = HERE / "data" / "keywords_bid_all.json"

# 这些列永远是标识符/文本, 禁止数值化 (campaignId 转成 float 会导致映射查找失败)
NON_NUMERIC_COLUMNS = {
    "campaignId", "adGroupId", "keywordId", "asin", "searchTerm", "campaignName",
    "adGroupName", "targeting", "campaignStatus", "campaignBudgetType", "matchType",
    "keywordType", "adKeywordStatus", "targetId", "keyword",
}


def iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def next_run_dir(out_dir: Path, day: str) -> Path:
    """本次运行输出文件夹: 日期 & 当日第几次 (如 2026-10-08 & 2)。"""
    numbers = [int(d.name.split(" & ")[1]) for d in out_dir.iterdir()
               if re.fullmatch(re.escape(day)+r" & \d+",d.name)]
    n = max(numbers,default=0)+1
    d = out_dir / f"{day} & {n}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_columns(dimensions: list[str], metrics: list[str]) -> list[str]:
    cols: list[str] = []
    for d in dimensions:
        for c in DIMENSION_COLUMNS[d]:
            if c not in cols:
                cols.append(c)
    for m in metrics:
        col = METRIC_MAP[m]
        if col not in cols:
            cols.append(col)
    return cols


def load_auto_ids() -> set[str]:
    """从本地 campaign 快照取 AUTO 活动集合 (报告无法按 targetingType 过滤)。"""
    if not CAMPAIGN_SNAPSHOT.exists():
        raise RuntimeError("campaigns 快照不存在, 无法判定自动广告")
    cs = json.loads(CAMPAIGN_SNAPSHOT.read_text(encoding="utf-8"))
    return {str(c["campaignId"]) for c in cs
            if c.get("targetingType") == "AUTO" and c.get("state") == "ENABLED"}


REPORT_CACHE = HERE / "data" / "report_cache"


def _apply_date_range(specs: list[dict], range_str: str) -> None:
    """覆盖报告时间范围: 7d / 30d / Nd / YYYY-MM-DD:YYYY-MM-DD。"""
    if re.fullmatch(r"\d+[dD]", range_str):
        days = int(range_str[:-1])
        for s in specs:
            s["天数"] = days
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}:\d{4}-\d{2}-\d{2}", range_str):
        start, end = range_str.split(":")
        for s in specs:
            s["_start"] = start
            s["_end"] = end
    else:
        raise ValueError(f"日期范围格式错误: {range_str!r}  (支持: 7d / 30d / Nd / YYYY-MM-DD:YYYY-MM-DD)")


def pull_reports(client: AdsClient, specs: list[dict], polling: dict | None = None) -> dict[str, list[dict]]:
    """批量创建全部报告 (亚马逊侧并行生成), 再统一等待下载。每次都重新向 Amazon 要新报告,
    不读 DB 也不读 JSON 缓存——保证广告指标(花费/点击/订单等)永远是当次最新数据。
    在途报告登记在 pending.json: 进程被杀后重跑可接回同一份报告, 不重复创建 (这是避免
    重复创建在途报告, 不是数据新鲜度缓存)。下载完成后仍写一份 JSON 到 report_cache/,
    供 `run.py alert` 等只读命令复用最近一次拉到的数据, 不代表本函数会再读它。"""
    polling = polling or {}
    tasks: list[dict] = []
    reports: dict[str, list[dict]] = {}
    REPORT_CACHE.mkdir(parents=True, exist_ok=True)
    pending_file = REPORT_CACHE / "pending.json"
    try:
        pending: dict = json.loads(pending_file.read_text(encoding="utf-8"))
    except Exception:
        pending = {}
    for spec in specs:
        dims = spec["维度"]
        cols = list(spec["列"]) if spec.get("列") else build_columns(dims, spec["指标"])
        if spec.get("_start") and spec.get("_end"):
            start, end = spec["_start"], spec["_end"]
        else:
            lag = int(spec.get("归因等待天数", 0))
            start, end = iso(int(spec["天数"])+lag), iso(1+lag)
        digest = hashlib.sha1(json.dumps([spec["报告类型"], dims, cols, start, end,
                                          spec.get("时间单位", "SUMMARY"), spec.get("过滤器")],
                                         sort_keys=True).encode()).hexdigest()[:12]
        cache_file = REPORT_CACHE / f"{spec['名称']}_{digest}.json"
        if digest in pending:
            rid = pending[digest]
            print(f"[报告] {spec['名称']} {start}..{end}: 复用在途报告 {rid}")
        else:
            name = f"pro1-{spec['名称']}-{start}-{end}"
            print(f"[报告] {spec['名称']} {start}..{end} groupBy={dims} 列数={len(cols)}")
            rid = client.create_report(name, start, end, spec["报告类型"], dims, cols,
                                       spec.get("时间单位", "SUMMARY"), spec.get("过滤器"))
            pending[digest] = rid
            pending_file.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
            print(f"  reportId={rid}")
        tasks.append({"spec": spec, "rid": rid, "cache_file": cache_file, "digest": digest})
    if tasks:
        rids = {t["rid"] for t in tasks}
        print(f"[等待] {len(rids)} 份报告并发轮询 (Amazon 侧本就并行生成, 不逐个顺序等) ...")
        urls = client.wait_reports(rids, timeout_s=int(polling.get("超时秒", 3600)),
                                   poll_s=int(polling.get("初始间隔秒", 30)),
                                   poll_max_s=int(polling.get("最大间隔秒", 120)))
        downloaded: dict[str, list] = {}  # rid -> 原始行, 同一份报告被多个 spec 复用时只下载一次
        for t in tasks:
            spec, rid = t["spec"], t["rid"]
            if rid not in downloaded:
                downloaded[rid] = client.download_report(urls[rid])
            rows = [dict(row, _report_days=int(spec.get("天数", 1))) for row in downloaded[rid]]
            print(f"  {spec['名称']}: {len(rows)} 行")
            t["cache_file"].write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            pending.pop(t["digest"], None)
            reports[spec["名称"]] = rows
        pending_file.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
    return reports


def _open_pipeline_conn(cfg: dict):
    """管道.双写 开着就开一个 SQLite 连接给 pull() 复用；关着返回 None。
    实际逻辑在 pipeline.open_conn()（execute.py 也要用，挪成共享函数了）。"""
    import pipeline
    return pipeline.open_conn(cfg, HERE)


def _dual_write_pipeline(conn, cfg: dict, specs: list[dict], reports: dict[str, list[dict]]) -> None:
    """Phase 1 数据管道：把已经拉到的报表行额外落一份到 SQLite，不影响现有 JSON 流程。

    conn 为 None 或 管道.双写=false 或任何异常都只打印警告、不中断主流程 —— 这一步是影子写入，
    建议/执行链路永远以 reports（JSON 缓存路径）为准。"""
    if conn is None or not (cfg.get("管道") or {}).get("双写"):
        return
    try:
        import pipeline
        pipeline.store_reports(conn, str(cfg["profile_id"]), specs, reports)
    except Exception as exc:
        print(f"[管道][警告] 双写 SQLite 失败，不影响本次建议生成: {exc}")


# （未使用的 _write_snapshot / pull_snapshots 已搬至 archive/rules_dead_code.py）


# ---------- 基线 ----------
def load_baseline(path: Path) -> tuple[dict, dict]:
    """返回 (default 基线, 全量配置含产品覆盖)。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    default = data.get("default") or {}
    return default, data


def product_code(campaign_name: str) -> str:
    """从活动名提取产品代码: 'P03-AUTO-260613' → 'P03', '101-GRE-MD-260528' → '101'。"""
    m = re.match(r"\s*([A-Za-z0-9]+)", campaign_name or "")
    return (m.group(1) if m else "UNKNOWN").upper()


# ---------- 竞品ASIN/类目定位收割 (REG2) ----------
# AUTO投放 report 的 targeting 原始表达式: asin="B0XXXXXXXX" / asin-expanded="B0XXXXXXXX" /
# category="12345"。跟 dashboard.py 的 _TARGETING_VALUE_RE 同一个格式, 各自独立一份正则
# (dashboard.py 是只读看板, 不依赖 rules.py 的核心规则逻辑, 避免反向耦合)。
_TARGETING_RE = re.compile(r'^(asin-expanded|asin|category)="([^"]*)"$')
TARGETING_KIND_TO_EXPR_TYPE = {
    "asin": "ASIN_SAME_AS", "asin-expanded": "ASIN_EXPANDED_FROM", "category": "CATEGORY_SAME_AS",
}


def classify_targeting(targeting) -> tuple[str | None, str | None]:
    """'asin="B0XXXX"' -> ('asin','B0XXXX')；不是竞品ASIN/类目定位 (close-match等) 返回 (None,None)。"""
    if not targeting:
        return None, None
    m = _TARGETING_RE.match(str(targeting).strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def pt_campaign_index(campaigns: dict) -> dict[str, list[dict]]:
    """产品码 -> 候选PT(商品定位)活动列表, 从活动名识别 (targetingType=MANUAL 且名字含
    POSITION/ASIN 关键字, 大小写不敏感)。名字前缀含"/"的双产品码活动 (如
    "105/205-POSITION-1102") 同时挂到两个产品码下, 跟 ONE1 建活动时同一套识别逻辑。"""
    idx: dict[str, list[dict]] = {}
    pat = re.compile(r"position|asin", re.IGNORECASE)
    for c in campaigns.values():
        name = c.get("name") or ""
        if c.get("targetingType") != "MANUAL" or not pat.search(name):
            continue
        prefix = name.split("-")[0] if name else ""
        for code in re.findall(r"[A-Za-z0-9]+", prefix):
            idx.setdefault(code.upper(), []).append(c)
    return idx


def wilson_upper_bound(successes: float, n: float, z: float = 1.645) -> float | None:
    """CVR 的 Wilson 置信区间上界: 即使往最乐观方向估, 真实CVR大概率也不会超过这个值。
    n<=0 时没有任何证据, 不该拿来跟任何阈值比——返回 None (调用方要在条件里先用
    clicks>=最低样本量 挡在前面, 短路掉 cvr_ub 本身, 否则 None 跟数字比较会报错)。
    z 默认 1.645 对应单边95%置信, 越大越保守 (阈值不容易被判定"显著低于预期")。"""
    if n <= 0:
        return None
    p = min(1.0, successes / n)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return min(1.0, center + margin)


def _parse_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip())
    except (ValueError, TypeError):
        return None


def active_seasonal_delta(code: str, all_base: dict, today: date | None = None) -> float:
    """扫描 baseline.yaml 顶层 季节期 列表, 命中当天日期且(未限定产品码或包含当前code)的
    条目, 把 acos上限增量 累加返回 (没有命中/没配置返回0)。多条同时命中就叠加, 不互斥——
    一般同一时间只会配一条, 叠加只是不强加"只生效第一条"这种隐藏假设。"""
    periods = all_base.get("季节期") or []
    if not periods:
        return 0.0
    today = today or date.today()
    delta = 0.0
    for p in periods:
        start, end = _parse_date(p.get("开始")), _parse_date(p.get("结束"))
        if start is None or end is None or not (start <= today <= end):
            continue
        codes = p.get("产品码") or []
        if codes and code not in codes:
            continue
        delta += float(p.get("acos上限增量", 0) or 0)
    return delta


def baseline_env(code: str, default: dict, all_base: dict, today: date | None = None) -> dict:
    """该产品代码的基线变量 + 派生阈值, 注入条件表达式环境。"""
    base = dict(default)
    base.update((all_base.get(code) or {}))
    seasonal_delta = active_seasonal_delta(code, all_base, today)
    if seasonal_delta:
        base["target_acos"] = float(base.get("target_acos", 0.3)) + seasonal_delta
    probability=float(base.get("no_order_probability",0.1))
    if not 0<probability<1 or not 0<float(base.get("expected_cvr",0.08))<1:
        raise ValueError("no_order_probability/expected_cvr 必须在0与1之间")
    if not 0<float(base.get("bid_min",0.2))<=float(base.get("bid_max",20)):
        raise ValueError("竞价上下限无效")
    base["no_order_spend_threshold"] = max(
        float(base.get("no_order_spend_floor", 15.0)), float(base.get("target_cpa", 15.0)))
    base["no_order_click_threshold"] = max(
        int(base.get("no_order_click_min", 10)),
        math.ceil(math.log(float(base.get("no_order_probability",0.1))) /
                  math.log(1-min(max(float(base.get("expected_cvr",0.08)),0.001),0.99))))
    return base


def load_protected(path: Path) -> set[str]:
    if not path.exists():
        return set()
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.add(line.lower())
    return words


def load_neg_phrase_vocab(path: Path) -> dict:
    """否定词组.yaml -> {类目词, 尺码词, 尺码词例外产品码} (均小写 set)。文件不存在返回空表。"""
    empty = {"类目词": set(), "尺码词": set(), "尺码词例外产品码": set()}
    if not path.exists():
        return empty
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "类目词": {str(w).strip().lower() for w in (data.get("类目词") or [])},
        "尺码词": {str(w).strip().lower() for w in (data.get("尺码词") or [])},
        "尺码词例外产品码": {str(w).strip() for w in (data.get("尺码词例外产品码") or [])},
    }


# ---------- 表达式求值 ----------
@functools.lru_cache(maxsize=None)
def _compile_condition(condition: str):
    """条件字符串的解析+白名单校验只做一次, 结果 (code object) 缓存复用——同一条规则
    对每一行都会调用一次, 不缓存就是对着同一段源码重复 ast.parse。"""
    allowed = (ast.Expression, ast.BoolOp, ast.Compare, ast.Name, ast.Load, ast.Constant,
               ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub,
               ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.And, ast.Or, ast.Eq, ast.NotEq,
               ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Call)
    tree = ast.parse(condition, mode="eval")
    if any(not isinstance(n, allowed) for n in ast.walk(tree)):
        raise ValueError(f"条件含不允许的语法: {condition}")
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (not isinstance(n.func, ast.Name) or n.func.id not in
                                         {"abs", "round", "min", "max", "len", "int", "float", "str"}):
            raise ValueError(f"条件调用了不允许的函数: {condition}")
    return compile(tree, "<condition>", "eval")


def safe_eval(condition: str, env: dict) -> bool:
    """只允许布尔/比较/算术/白名单调用，拒绝任意 Python 代码。"""
    if not condition:
        return True
    try:
        code = _compile_condition(condition)
        return bool(eval(code, {"__builtins__": {}}, env))  # noqa: S307 - 受控环境
    except NameError:
        return False   # 行里没有该字段 → 不触发
    except ZeroDivisionError:
        return False


@functools.lru_cache(maxsize=None)
def _compile_expr(expr: str):
    """同 _compile_condition, 给 eval_value 用——置信/预算表达式也是同一串源码对每行重复求值。"""
    if "__" in expr or "import" in expr:
        raise ValueError(f"表达式含禁止内容: {expr}")
    tree = ast.parse(expr, mode="eval")
    allowed = (ast.Expression, ast.IfExp, ast.Compare, ast.Name, ast.Load, ast.Constant,
               ast.UnaryOp, ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
               ast.Div, ast.Mod, ast.Pow, ast.Call, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
               ast.Eq, ast.NotEq, ast.BoolOp, ast.And, ast.Or)
    if any(not isinstance(n, allowed) for n in ast.walk(tree)):
        raise ValueError(f"表达式含不允许的语法: {expr}")
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (not isinstance(n.func, ast.Name) or n.func.id not in
                                         {"abs", "round", "min", "max", "int", "float", "str"}):
            raise ValueError(f"表达式调用了不允许的函数: {expr}")
    return compile(tree, "<expr>", "eval")


def eval_value(expr: str, env: dict):
    """求值并返回原始值 (置信表达式/预算参数表达式用)。"""
    if not expr:
        return None
    try:
        code = _compile_expr(expr)
        return eval(code, {"__builtins__": {}}, env)  # noqa: S307
    except Exception:
        return None


CONF_LITERALS = {"high", "medium", "low", "needs_human"}


def conf_value(expr, env) -> str:
    """置信字段: 字面量直接用, 表达式求值, 失败回退 medium。"""
    if isinstance(expr, str) and expr.lower() in CONF_LITERALS:
        return expr.lower()
    return eval_value(expr, env) or "medium"


def make_env(row: dict, base_env: dict, protected: set[str], neg_phrase_vocab: dict | None = None,
             product_code_fn=None, positioning: dict[str, dict] | None = None) -> dict:
    """行环境 = 全部行字段 (标识符列保留原值, 指标列数值化) + 派生列 + 基线变量 + 内置函数。"""
    env: dict = {k: v for k, v in row.items()
                 if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(k))}
    for k, v in list(env.items()):
        if k in NON_NUMERIC_COLUMNS:
            continue  # 标识符/状态列保留原样
        if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v):
            try:
                env[k] = float(v)
            except ValueError:
                pass
    # 派生列
    cost = float(row.get("cost") or 0)
    clicks = float(row.get("clicks") or 0)
    sales = float(row.get("sales7d") or 0)
    env["acos7d"] = (cost / sales) if sales > 0 else None
    env["cpc7d"] = (cost / clicks) if clicks > 0 else None
    purchases = float(row.get("purchases7d") or 0)
    env["cvr_ub"] = wilson_upper_bound(purchases, clicks, float(base_env.get("wilson_z", 1.645)))
    impressions = float(row.get("impressions") or 0)
    env["ctr"] = (clicks / impressions) if impressions > 0 else None
    days = float(row.get("_report_days") or 1)
    env["avgDailyCost"] = cost / max(days, 1)
    env["targetId"] = str(row.get("targetId") or row.get("keywordId") or "")
    # 内置: 保护词 / 函数
    st = str(row.get("searchTerm") or "").strip().lower()
    env["保护词"] = st in protected if st else False
    # 类目/尺码不符: 否定词组.yaml 驱动, 词表改了不用碰条件表达式。命中的具体词组存进
    # _否定词组命中, 给 expand_negative 额外生成 NEGATIVE_PHRASE 用 (见 config.yaml 规则段)。
    cat_hits: list[str] = []
    if neg_phrase_vocab and st:
        cat_hits = [w for w in neg_phrase_vocab["类目词"] if w in st]
        code = product_code_fn(row.get("campaignName")) if product_code_fn else None
        if code not in neg_phrase_vocab["尺码词例外产品码"]:
            cat_hits += [w for w in neg_phrase_vocab["尺码词"] if w in st]
    env["类目尺码不符"] = bool(cat_hits)
    env["_否定词组命中"] = cat_hits
    # REG2 竞品定位收割: AUTO投放里 targeting 形如 asin="..."/category="..." 才是竞品ASIN/
    # 类目定位, close-match/substitutes等常规AUTO自动发现不算 (classify_targeting 返回 None)。
    env["是否竞品定位"] = classify_targeting(row.get("targeting"))[0] is not None
    # 新品冲量期保护: 阶段/新品天数来自产品定位.yaml，没有positioning或没匹配到产品码则
    # 都是unknown/None，不影响非新品规则(各条件里都是 "阶段=='new' and ..." 的加法式判断)。
    if positioning is not None:
        code = product_code_fn(row.get("campaignName")) if product_code_fn else None
        env["阶段"] = positioning_of(code, positioning)["阶段"] if code else "unknown"
        env["新品天数"] = days_since_launch(code, positioning) if code else None
    else:
        env["阶段"] = "unknown"
        env["新品天数"] = None
    env.update({"abs": abs, "round": round, "min": min, "max": max,
                "len": len, "int": int, "float": float, "str": str})
    env.update(base_env)
    return env


def match_rows(rows: list[dict], filter_expr: str, condition: str, env_of) -> list[tuple[dict, dict]]:
    """返回 [(row, env)], 仅含同时通过过滤和条件的行。"""
    out = []
    for row in rows:
        env = env_of(row)
        if not safe_eval(filter_expr, env):
            continue
        if not safe_eval(condition, env):
            continue
        out.append((row, env))
    return out


def action_id(kind: str, row: dict, term: str = "") -> str:
    raw = "|".join([kind, str(row.get("campaignId") or ""), str(row.get("adGroupId") or ""), term])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


# ---------- 动作展开: 把抽象动作落到具体目标 ----------
# Amazon ASIN 形状 (10 位, B0 开头) — 商品词, 关键词动作不处理
ASIN_RE = re.compile(r"^b0[a-z0-9]{8}$", re.IGNORECASE)


def expand_negative(hits, conf, ignore_asin: bool = True, phrase_rule: bool = False,
                    map_data: dict | None = None) -> tuple[list[dict], list[dict]]:
    """否定精确 → 加到同广告组。ASIN/商品词不处理。
    phrase_rule=True (类目尺码不符-否定 用): 命中的类目/尺码词本身额外生成一条
    NEGATIVE_PHRASE (同一词在同广告组内只否一次，不管几条 searchTerm 命中它)。
    map_data 给出时 (ctx["map_data"], 跟 收割 复用同一份 手动映射.json/auto_broad_map.json):
    否定(精确+词组)同步镜像到映射的 BROAD 活动广告组——否定只会从 AUTO 搜索词报表触发，
    BROAD 自己没有搜索词报表看不到这些无关词，需要靠镜像同步才否得到。"""
    actions, skipped = [], []
    seen_phrase: set[tuple] = set()
    seen_mirror: set[tuple] = set()

    def mirror(term: str, match_type: str, src_campaign_id, label, score, reason, src_name):
        if not map_data:
            return
        entry = map_data.get("map", {}).get(str(src_campaign_id))
        if not entry or not entry.get("broad"):
            return
        target = next((b for b in entry["broad"] if b.get("state") == "ENABLED"), None)
        ags = entry.get("adGroupIds") or []
        if not target or not ags:
            return
        key = (target["cid"], ags[0], match_type, term.lower())
        if key in seen_mirror:
            return
        seen_mirror.add(key)
        actions.append({
            "动作ID": action_id("negsync", {"campaignId": target["cid"], "adGroupId": ags[0]},
                               match_type + term.lower()),
            "动作": "否定", "匹配": match_type, "词": term,
            "campaignId": target["cid"], "adGroupId": ags[0],
            "产品码": "",
            "置信": label, "置信分": score, "置信理由": reason,
            "来源": f"同步镜像←{src_name}",
            "指标": "",
        })

    for r, env in hits:
        st = (r.get("searchTerm") or "").strip()
        if not st:
            continue
        if ignore_asin and ASIN_RE.fullmatch(st):
            skipped.append({"词": st, "来源": r.get("campaignName"), "原因": "ASIN商品词-暂不处理"})
            continue
        label = conf_value(conf, env)
        score, reason = conf_numeric(label, env, "否定")
        cid, agid, name = r.get("campaignId"), r.get("adGroupId"), r.get("campaignName")
        actions.append({
            "动作ID": action_id("neg", r, st.lower()),
            "动作": "否定", "匹配": "NEGATIVE_EXACT", "词": st,
            "campaignId": cid, "adGroupId": agid,
            "产品码": product_code(name),
            "置信": label, "置信分": score, "置信理由": reason,
            "来源": name,
            "指标": f"cost={r.get('cost')} clicks={r.get('clicks')} purchases={r.get('purchases7d')}",
        })
        mirror(st, "NEGATIVE_EXACT", cid, label, score, reason, name)

        if phrase_rule:
            for term in env.get("_否定词组命中") or []:
                key = (cid, agid, term)
                if key in seen_phrase:
                    continue
                seen_phrase.add(key)
                actions.append({
                    "动作ID": action_id("negphrase", r, term),
                    "动作": "否定", "匹配": "NEGATIVE_PHRASE", "词": term,
                    "campaignId": cid, "adGroupId": agid,
                    "产品码": product_code(name),
                    "置信": label, "置信分": score, "置信理由": reason,
                    "来源": name,
                    "指标": f"词组否定(来自搜索词: {st})",
                })
                mirror(term, "NEGATIVE_PHRASE", cid, label, score, reason, name)
    return actions, skipped


def expand_harvest(hits, map_data, kw_index, conf_expr, default_base, all_base, protected,
                   ignore_asin: bool = True) -> tuple[list[dict], list[dict]]:
    """出单词 → 收割到同前缀 BROAD 活动 (映射文件), 去重后取目标活动广告组。ASIN/商品词不收。"""
    actions, skipped = [], []
    for r, env in hits:
        st = (r.get("searchTerm") or "").strip().lower()
        if not st:
            continue
        if ignore_asin and ASIN_RE.fullmatch(st):
            skipped.append({"词": st, "来源": r.get("campaignName"), "原因": "ASIN商品词-暂不处理"})
            continue
        cid = str(r.get("campaignId"))
        entry = map_data.get("map", {}).get(cid)
        if not entry or not entry.get("broad"):
            skipped.append({"词": st, "来源": r.get("campaignName"), "原因": "无对应BROAD活动"})
            continue
        target = next((b for b in entry["broad"] if b.get("state") == "ENABLED"), None)
        if not target:
            skipped.append({"词": st, "来源": r.get("campaignName"), "原因": "BROAD活动非启用"})
            continue
        existing = kw_index.get(target["cid"], {})
        records = [v[st] for v in existing.values() if st in v]
        if records:
            skipped.append({"词": st, "来源": r.get("campaignName"),
                            "原因": f"已存在(活动{target['cid']}) 状态=" + "; ".join(
                                f"{item.get('state')} keywordId={item.get('keywordId')}"
                                for group in records for item in (group if isinstance(group,list) else [{"state":group}] ))})
            continue
        broad_adgs = list(existing.keys())
        ags = entry.get("adGroupIds") or []
        adg = ags[0] if ags else ""
        if not adg:
            skipped.append({"词":st,"来源":r.get("campaignName"),"原因":"目标广告组缺失"})
            continue
        label = conf_value(conf_expr, env)
        score, reason = conf_numeric(label, env, "收割")
        actions.append({
            "动作ID": action_id("harv", {"campaignId":target["cid"],"adGroupId":adg}, st),
            "动作": "收割", "匹配": "BROAD", "词": st,
            "campaignId": target["cid"], "adGroupId": adg,
            "产品码": product_code(r.get("campaignName")),
            "置信": label, "置信分": score, "置信理由": reason,
            "来源": r.get("campaignName"),
            "指标": f"purchases={r.get('purchases7d')} sales={r.get('sales7d')} acos={env.get('acos7d')}",
        })
    return actions, skipped


def expand_pt_harvest(hits, pt_index: dict, adgroups: list, pt_target_index: dict,
                      conf_expr) -> tuple[list[dict], list[dict], dict]:
    """竞品ASIN/类目定位 (来自AUTO投放) 出单 -> 收割到对应 PT(商品定位) 活动。
    目标活动按产品码从活动名匹配 (pt_index, 见 pt_campaign_index); 没有对应PT活动/
    广告组歧义/已存在同定向都跳过, 不自动猜一个。跟 expand_harvest(BROAD关键词收割)
    是同一套"先找映射目标再判重"思路, 只是目标索引换成按产品码+活动名识别, 不是
    手动映射.json (PT活动是ONE1批量建的, 命名已经规范, 不需要再维护一份映射文件)。
    返回 (动作, 跳过明细, envs) —— envs 给 enrich_pt_harvest 算Bid用, 跟 expand_harvest
    不同 (expand_harvest所在的 evaluate() 直接把 hits 原样传给 enrich_harvest)。"""
    actions, skipped = [], []
    envs: dict[tuple[str, str], dict] = {}
    ag_by_campaign: dict[str, list] = {}
    for g in adgroups:
        ag_by_campaign.setdefault(str(g.get("campaignId")), []).append(g)

    for r, env in hits:
        kind, value = classify_targeting(r.get("targeting"))
        if not kind or not value:
            continue
        name = r.get("campaignName")
        code = product_code(name)
        candidates = pt_index.get(code, [])
        enabled = [c for c in candidates if c.get("state") == "ENABLED"]
        chosen = enabled or candidates
        if not chosen:
            skipped.append({"词": value, "来源": name, "原因": "无对应定位活动"})
            continue
        if len(chosen) > 1:
            skipped.append({"词": value, "来源": name, "原因": "定位活动歧义(多个候选同名前缀)"})
            continue
        target_c = chosen[0]
        cid = str(target_c.get("campaignId"))
        ag_candidates = [g for g in ag_by_campaign.get(cid, []) if g.get("state") == "ENABLED"]
        if len(ag_candidates) != 1:
            skipped.append({"词": value, "来源": name,
                            "原因": f"目标广告组缺失或歧义(活动{cid}内{len(ag_candidates)}个启用广告组)"})
            continue
        agid = str(ag_candidates[0].get("adGroupId"))
        expr_type = TARGETING_KIND_TO_EXPR_TYPE[kind]
        if value.lower() in pt_target_index.get(cid, set()):
            skipped.append({"词": value, "来源": name, "原因": f"已存在(活动{cid})"})
            continue
        label = conf_value(conf_expr, env)
        score, reason = conf_numeric(label, env, "收割")
        envs[(str(name), value.lower())] = env
        actions.append({
            "动作ID": action_id("ptharv", {"campaignId": cid, "adGroupId": agid}, value.lower()),
            "动作": "收割ASIN", "匹配": expr_type, "词": value,
            "campaignId": cid, "adGroupId": agid,
            "产品码": code,
            "置信": label, "置信分": score, "置信理由": reason,
            "来源": name,
            "指标": (f"purchases={r.get('purchases7d')} sales={r.get('sales7d')} "
                    f"acos={env.get('acos7d')} 目标活动={target_c.get('name')}"),
        })
    return actions, skipped, envs


#: 负向定位只接受 ASIN 精确值 (Amazon negativeTargetingClauses 只支持 ASIN_SAME_AS /
#: ASIN_BRAND_SAME_AS); asin-expanded 也落地成 ASIN_SAME_AS (负向定位没有"从此扩展"这个
#: 表达式类型); category 不支持负向定位, 命中后只能跳过。
NEGATABLE_TARGETING_KINDS = {"asin", "asin-expanded"}


def expand_negative_asin(hits, conf, neg_target_index: dict[str, set[str]]) -> tuple[list[dict], list[dict]]:
    """AUTO投放里高浪费的竞品ASIN/类目定位 -> 在同一活动/广告组内加一条负向定位, 止住
    无效花费 (跟 否定 关键词同样的"高浪费"阈值, 只是作用对象是 targeting 字段本身, 不是
    搜索词)。目标就是来源那条AUTO投放自己的活动/广告组, 不走映射文件——这跟 收割ASIN
    (目标是另一个PT活动) 刚好相反方向。
    neg_target_index: {campaignId: {已存在的负向ASIN值(小写)}}, 判重用, 由调用方
    (load_context 现查 list_negative_targets) 传入, 跟 pt_target_index 同一套思路。"""
    actions, skipped = [], []
    for r, env in hits:
        kind, value = classify_targeting(r.get("targeting"))
        if not kind or not value:
            continue
        name = r.get("campaignName")
        if kind not in NEGATABLE_TARGETING_KINDS:
            skipped.append({"词": value, "来源": name, "原因": "类目定位不支持负向定位"})
            continue
        cid, agid = str(r.get("campaignId")), str(r.get("adGroupId"))
        if value.lower() in neg_target_index.get(cid, set()):
            skipped.append({"词": value, "来源": name, "原因": f"已存在负向定位(活动{cid})"})
            continue
        label = conf_value(conf, env)
        score, reason = conf_numeric(label, env, "否定")
        actions.append({
            "动作ID": action_id("negasin", r, value.lower()),
            "动作": "否定ASIN", "匹配": "ASIN_SAME_AS", "词": value,
            "campaignId": cid, "adGroupId": agid,
            "产品码": product_code(name),
            "置信": label, "置信分": score, "置信理由": reason,
            "来源": name,
            "指标": f"cost={r.get('cost')} clicks={r.get('clicks')} purchases={r.get('purchases7d')}",
        })
    return actions, skipped


def expand_budget(hits, rule) -> list[dict]:
    actions = []
    expr = rule.get("参数") or "campaignBudgetAmount"
    for r, env in hits:
        new_budget = eval_value(expr, env)
        try:
            new_budget = round(float(new_budget), 2)
        except (TypeError, ValueError):
            continue
        if new_budget <= 0:
            continue
        label = conf_value(rule.get("置信", "medium"), env)
        score, reason = conf_numeric(label, env, "预算")
        actions.append({
            "动作ID": action_id("bud", r, f"{date.today()}:{r.get('campaignBudgetAmount')}->{new_budget}"),
            "动作": "预算", "匹配": "", "词": "",
            "campaignId": r.get("campaignId"), "adGroupId": "",
            "新预算": new_budget, "原预算": r.get("campaignBudgetAmount"),
            "产品码": product_code(r.get("campaignName")),
            "置信": label, "置信分": score, "置信理由": reason,
            "来源": r.get("campaignName"),
            "指标": f"cost={r.get('cost')} avgDailyCost={env.get('avgDailyCost')} purchases={r.get('purchases7d')}",
        })
    return actions


def load_target_snapshot() -> dict[str, dict]:
    if not TARGET_SNAPSHOT.exists():
        return {}
    rows = json.loads(TARGET_SNAPSHOT.read_text(encoding="utf-8"))
    return {str(r.get("targetId")): r for r in rows if r.get("targetId") is not None}


def expand_bid(hits, rule, target_snapshot: dict[str, dict], default_base: dict,
               all_base: dict, protected: set[str],
               pl_joint: dict[str, dict] | None = None) -> tuple[list[dict], list[dict]]:
    """仅生成 AUTO_TARGET BID 计划；无唯一 target/current bid/状态则跳过。

    带比例表达式的弹性规则 (高ACOS降价/稳定低ACOS加价) 叠加与位置竞价的联合优化
    (pl_joint, 来自 compute_joint_bids): 按 auto_joint_weight 权重与自身ACOS信号
    加权平均，只放大/收缩调整幅度、不反转方向——跟 BROAD 关键词 (expand_keyword_bid)
    同一套逻辑，确保同一活动下 base bid 和位置 Bid 朝同一个方向协同调整，不是各调各的。
    零单硬降价这类安全熔断规则(无比例表达式)不参与联合优化。"""
    pl_joint = pl_joint or {}
    actions, skipped = [], []
    for r, env in hits:
        tid = str(r.get("targetId") or r.get("keywordId") or "")
        snap = target_snapshot.get(tid)
        if not tid or not snap:
            skipped.append({"词": r.get("targeting") or tid, "来源": r.get("campaignName"), "原因": "target快照缺失或无法唯一关联"})
            continue
        if str(r.get("keywordType") or "") not in {"TARGETING_EXPRESSION", "TARGETING_EXPRESSION_PREDEFINED"}:
            skipped.append({"词": r.get("targeting") or tid, "来源": r.get("campaignName"), "原因": "非AUTO投放表达式"})
            continue
        if str(snap.get("state") or r.get("adKeywordStatus") or "").upper() != "ENABLED":
            skipped.append({"词": r.get("targeting") or tid, "来源": r.get("campaignName"), "原因": "target非启用"})
            continue
        try:
            old = float(snap.get("bid") if snap.get("bid") is not None else snap.get("_effective_bid"))
            if old <= 0:
                raise ValueError("非正竞价")
        except (TypeError, ValueError):
            skipped.append({"词": r.get("targeting") or tid, "来源": r.get("campaignName"), "原因": "当前Bid缺失"})
            continue

        direction = rule.get("方向")
        ratio_expr = rule.get("比例表达式", "")
        base_ratio = abs(float(eval_value(ratio_expr, env) or rule.get("比例", 0)))
        combined_signed = (1.0 if direction == "加价" else -1.0) * base_ratio
        notes: list[str] = []

        if ratio_expr:
            cid = str(r.get("campaignId") or "")
            joint = pl_joint.get(cid)
            if joint and joint.get("base_bid_delta_pct") is not None and combined_signed != 0:
                jw = float(env.get("auto_joint_weight", 0.3))
                placement_signed = joint["base_bid_delta_pct"] / 100.0
                blended = (1 - jw) * combined_signed + jw * placement_signed
                if (blended > 0) == (combined_signed > 0):
                    combined_signed = blended
                    notes.append(f"位置联合: {joint.get('reasoning', '')}")
                # 否则联合信号与自身方向相反, 放弃联合信号, 保留 combined_signed 原值

        if combined_signed == 0:
            continue
        final_direction = "加价" if combined_signed > 0 else "减价"
        cap = float(env.get("bid_increase_cap" if final_direction == "加价" else "bid_decrease_cap", 0.2))
        ratio = min(abs(combined_signed), cap)
        new = old * (1 + ratio if final_direction == "加价" else 1 - ratio)
        new = round(min(max(new, float(env.get("bid_min", 0.2))), float(env.get("bid_max", 20.0))), 2)
        if new == round(old, 2) or (final_direction == "减价" and new >= old) or (final_direction == "加价" and new <= old):
            continue
        label = conf_value(rule.get("置信", "medium"), env)
        score, reason = conf_numeric(label, env, "BID")
        actions.append({"动作ID": action_id("bid", r, f"{date.today()}:{tid}:{old}->{new}"), "动作": "BID", "层级": "AUTO_TARGET",
                        "targetId": tid, "campaignId": r.get("campaignId"), "adGroupId": r.get("adGroupId"),
                        "投放组": r.get("targeting"), "原Bid": round(old, 2), "新Bid": new,
                        "调整比例": round((new / old) - 1, 6), "规则名": rule.get("名称"), "来源": r.get("campaignName"),
                        "产品码": product_code(r.get("campaignName")), "指标": f"cost={r.get('cost')} clicks={r.get('clicks')} purchases={r.get('purchases7d')} acos={env.get('acos7d')}",
                        "协同建议": "; ".join(notes),
                        "保护状态": "是" if env.get("保护词") else "否",
                        "置信": label, "置信分": score, "置信理由": reason})
    return actions, skipped


def load_keyword_snapshot() -> dict[str, dict]:
    if not KEYWORD_SNAPSHOT.exists():
        return {}
    rows = json.loads(KEYWORD_SNAPSHOT.read_text(encoding="utf-8"))
    return {str(r.get("keywordId")): r for r in rows if r.get("keywordId") is not None}


def _broad_campaign_base_bid(cid: str, keyword_snapshot: dict[str, dict]) -> float | None:
    """该活动下已启用 BROAD 关键词当前 Bid 的中位数, 作为联合优化 (compute_joint_bids) 的
    代表性 base bid —— 镜像 _campaign_base_bid 对 AUTO 活动取 target 中位数的做法。"""
    bids = [float(k.get("bid") if k.get("bid") is not None else k.get("_effective_bid"))
            for k in keyword_snapshot.values()
            if str(k.get("campaignId")) == cid and str(k.get("matchType") or "").upper() == "BROAD"
            and str(k.get("state") or "").upper() == "ENABLED"
            and (k.get("bid") or k.get("_effective_bid"))]
    return statistics.median(bids) if bids else None


def expand_keyword_bid(hits, rule, keyword_snapshot: dict[str, dict], default_base: dict,
                       all_base: dict, protected: set[str], ledger_idx: dict,
                       pl_joint: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """生成已上线 BROAD 关键词的 BID 计划; 只动 matchType=BROAD、无唯一关键词/当前Bid/状态则跳过。

    与 expand_bid (AUTO_TARGET) 用同一套比例/上下限/置信逻辑, 竞价对象换成已存在的 BROAD 关键词
    (keywordId, 走 /sp/keywords)。对带 比例表达式 的弹性规则 (高ACOS降价/稳定低ACOS加价) 额外叠加
    两层信号, 零单硬降价这类安全熔断规则不参与:
      1. 历史反馈 (history_feedback): 上次同方向调整后 ACOS 是否朝目标靠近, 据此放大/收缩本次幅度;
      2. 与位置竞价的联合优化 (pl_joint, 来自 compute_joint_bids): 按权重与自身信号加权平均,
         只允许改变幅度、不允许反转方向 (位置层噪声不应推翻关键词自身ACOS给出的方向)。"""
    actions, skipped = [], []
    for r, env in hits:
        kid = str(r.get("keywordId") or "")
        snap = keyword_snapshot.get(kid)
        if not kid or not snap:
            skipped.append({"词": r.get("keyword") or kid, "来源": r.get("campaignName"), "原因": "关键词快照缺失或无法唯一关联"})
            continue
        if str(snap.get("matchType") or r.get("matchType") or "").upper() != "BROAD":
            skipped.append({"词": r.get("keyword") or kid, "来源": r.get("campaignName"), "原因": "非BROAD匹配关键词"})
            continue
        if str(snap.get("state") or r.get("adKeywordStatus") or "").upper() != "ENABLED":
            skipped.append({"词": r.get("keyword") or kid, "来源": r.get("campaignName"), "原因": "关键词非启用"})
            continue
        try:
            old = float(snap.get("bid") if snap.get("bid") is not None else snap.get("_effective_bid"))
            if old <= 0:
                raise ValueError("非正竞价")
        except (TypeError, ValueError):
            skipped.append({"词": r.get("keyword") or kid, "来源": r.get("campaignName"), "原因": "当前Bid缺失"})
            continue

        direction = rule.get("方向")
        ratio_expr = rule.get("比例表达式", "")
        base_ratio = abs(float(eval_value(ratio_expr, env) or rule.get("比例", 0)))
        combined_signed = (1.0 if direction == "加价" else -1.0) * base_ratio
        notes: list[str] = []

        if ratio_expr:
            cid = str(r.get("campaignId") or "")
            mult, fb_reason = history_feedback(
                ledger_idx, "BID", "KEYWORD", cid, kid, direction, env.get("acos7d"),
                float(env.get("target_acos", 0.3)),
                float(env.get("history_feedback_lookback_days", 10)),
                float(env.get("history_feedback_boost", 1.25)),
                float(env.get("history_feedback_damp", 0.5)))
            combined_signed *= mult
            if fb_reason:
                notes.append(fb_reason)

            joint = pl_joint.get(cid)
            if joint and joint.get("base_bid_delta_pct") is not None and combined_signed != 0:
                jw = float(env.get("broad_joint_weight", 0.3))
                placement_signed = joint["base_bid_delta_pct"] / 100.0
                blended = (1 - jw) * combined_signed + jw * placement_signed
                if (blended > 0) == (combined_signed > 0):
                    combined_signed = blended
                    notes.append(f"位置联合: {joint.get('reasoning', '')}")
                # 否则联合信号与自身方向相反, 放弃联合信号, 保留 combined_signed 原值

        if combined_signed == 0:
            continue
        final_direction = "加价" if combined_signed > 0 else "减价"
        cap = float(env.get("bid_increase_cap" if final_direction == "加价" else "bid_decrease_cap", 0.2))
        ratio = min(abs(combined_signed), cap)
        new = old * (1 + ratio if final_direction == "加价" else 1 - ratio)
        new = round(min(max(new, float(env.get("bid_min", 0.2))), float(env.get("bid_max", 20.0))), 2)
        if new == round(old, 2) or (final_direction == "减价" and new >= old) or (final_direction == "加价" and new <= old):
            continue
        label = conf_value(rule.get("置信", "medium"), env)
        score, reason = conf_numeric(label, env, "BID")
        acos_now = env.get("acos7d")
        actions.append({"动作ID": action_id("kwbid", r, f"{date.today()}:{kid}:{old}->{new}"), "动作": "BID", "层级": "KEYWORD",
                        "targetId": kid, "campaignId": r.get("campaignId"), "adGroupId": r.get("adGroupId"),
                        "投放组": r.get("keyword"), "原Bid": round(old, 2), "新Bid": new,
                        "调整比例": round((new / old) - 1, 6),
                        "决策ACOS": round(acos_now, 4) if acos_now is not None else "",
                        "规则名": rule.get("名称"), "来源": r.get("campaignName"),
                        "产品码": product_code(r.get("campaignName")),
                        "指标": f"cost={r.get('cost')} clicks={r.get('clicks')} purchases={r.get('purchases7d')} acos={acos_now}",
                        "协同建议": "; ".join(notes),
                        "保护状态": "是" if env.get("保护词") else "否",
                        "置信": label, "置信分": score, "置信理由": reason})
    return actions, skipped


def load_product_line_map(path: Path) -> dict[str, str]:
    """活动代码 -> 产品线canonical代码。文件不存在/解析失败则返回空映射 (梯度功能整体关闭不报错)。"""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): str(v).upper() for k, v in data.items() if v}


def canonical_product(code: str, line_map: dict[str, str]) -> str:
    return line_map.get(str(code).upper(), str(code).upper())


#: 没在 产品定位.yaml 登记的产品代码落到这个默认值——类目/阶段都当"unknown"，
#: 不参与价格梯度保护, 也不当弱势/新品处理 (走原来的"同类竞争"逻辑)。
DEFAULT_POSITIONING = {"类目": "unknown", "阶段": "unknown", "价格梯度组": "", "上架日期": None}


def load_product_positioning(path: Path) -> dict[str, dict]:
    """产品代码 -> {类目, 阶段, 价格梯度组}。文件不存在/解析失败则返回空映射
    (重叠检测/词库缺口功能退化成"所有产品同等对待"，不报错)。"""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        out[str(k).upper()] = {"类目": v.get("类目", "unknown"),
                               "阶段": v.get("阶段", "unknown"),
                               "价格梯度组": v.get("价格梯度组") or "",
                               "上架日期": v.get("上架日期")}
    return out


def positioning_of(code: str, positioning: dict[str, dict]) -> dict:
    return positioning.get(str(code).upper(), DEFAULT_POSITIONING)


def days_since_launch(code: str, positioning: dict[str, dict], today: date | None = None) -> int | None:
    """新品上架至今天数。没有 上架日期 字段(非新品/未填)返回 None，规则条件里用
    `新品天数 is None` 判断"不是通用新品保护期逻辑适用的产品"。"""
    launch = _parse_date(positioning_of(code, positioning).get("上架日期"))
    if launch is None:
        return None
    return ((today or date.today()) - launch).days


def expand_product_line_gradient(broad_rows: list[dict], keyword_snapshot: dict[str, dict],
                                 line_map: dict[str, str], actions: list[dict],
                                 default_base: dict, all_base: dict) -> tuple[list[dict], list[dict]]:
    """同产品线 (不同马甲活动代码对应同一实际产品, 见 产品线.yaml) 下, 同一个 BROAD 关键词如果在
    多个活动里同时投放, 等于自己和自己抢同一个词的拍卖。按表现 (订单优先, ACOS次之) 排名, 非头部
    活动的 Bid 压出梯度上限 (逐级降低), 减少内部竞价内卷; 头部活动不受影响, 数据不足的活动既不参与
    排名也不被压低。"""
    by_group: dict[tuple, list[dict]] = {}
    for row in broad_rows:
        term = str(row.get("keyword", "")).strip().lower()
        if not term:
            continue
        code = product_code(row.get("campaignName"))
        by_group.setdefault((canonical_product(code, line_map), term), []).append(row)

    acted_by_kid = {str(a.get("targetId")): a for a in actions
                    if a.get("动作") == "BID" and a.get("层级") == "KEYWORD"}
    extra: list[dict] = []
    skipped: list[dict] = []
    min_clicks = float(default_base.get("broad_bid_min_clicks", 20))

    def _acos(row: dict) -> float:
        cost = float(row.get("cost") or 0)
        sales = float(row.get("sales7d") or 0)
        return cost / sales if sales > 0 else float("inf")

    for (canon, term), rows in by_group.items():
        if len({str(row.get("campaignId")) for row in rows}) < 2:
            continue
        by_cid: dict[str, dict] = {}
        for row in rows:
            if float(row.get("clicks") or 0) >= min_clicks:
                by_cid.setdefault(str(row.get("campaignId")), row)
        if len(by_cid) < 2:
            continue

        ranked = sorted(by_cid.values(), key=lambda row: (-float(row.get("purchases7d") or 0), _acos(row)))
        primary = ranked[0]
        primary_kid = str(primary.get("keywordId") or "")
        primary_action = acted_by_kid.get(primary_kid)
        if primary_action:
            primary_bid = float(primary_action["新Bid"])
        else:
            psnap = keyword_snapshot.get(primary_kid, {})
            pbid = psnap.get("bid") if psnap.get("bid") is not None else psnap.get("_effective_bid")
            primary_bid = float(pbid) if pbid else 0.0
        if primary_bid <= 0:
            continue

        base = baseline_env(product_code(primary.get("campaignName")), default_base, all_base)
        bid_min = float(base.get("bid_min", 0.2))
        step = float(base.get("product_line_gradient_step", 0.15))

        for rank, row in enumerate(ranked[1:], start=1):
            kid = str(row.get("keywordId") or "")
            if not kid:
                continue
            cap = round(max(bid_min, primary_bid * (1 - step * rank)), 2)
            note = f"产品线梯度:与{primary.get('campaignName')}同词'{term}'竞争,排名第{rank + 1},压低Bid上限至{cap}"
            existing = acted_by_kid.get(kid)
            if existing:
                if float(existing["新Bid"]) > cap:
                    existing["新Bid"] = cap
                    existing["调整比例"] = round((cap / float(existing["原Bid"])) - 1, 6)
                    prev_note = existing.get("协同建议") or ""
                    existing["协同建议"] = (prev_note + "; " + note) if prev_note else note
                continue
            ksnap = keyword_snapshot.get(kid, {})
            kbid = ksnap.get("bid") if ksnap.get("bid") is not None else ksnap.get("_effective_bid")
            try:
                cur_bid = float(kbid)
            except (TypeError, ValueError):
                continue
            if cur_bid <= cap + 0.001:
                continue
            extra.append({
                "动作ID": action_id("plgrad", row, f"{date.today()}:{kid}:{cur_bid}->{cap}"),
                "动作": "BID", "层级": "KEYWORD",
                "targetId": kid, "campaignId": row.get("campaignId"), "adGroupId": row.get("adGroupId"),
                "投放组": row.get("keyword"), "原Bid": round(cur_bid, 2), "新Bid": cap,
                "调整比例": round((cap / cur_bid) - 1, 6), "决策ACOS": "",
                "规则名": "产品线梯度", "来源": row.get("campaignName"),
                "产品码": product_code(row.get("campaignName")),
                "指标": f"cost={row.get('cost')} clicks={row.get('clicks')} purchases={row.get('purchases7d')}",
                "协同建议": note,
                "保护状态": "否",
                "置信": "medium", "置信分": 55,
                "置信理由": f"产品线梯度协同(同产品线{canon}内'{term}'排名第{rank + 1})",
            })
    return actions + extra, skipped


#: 不算内容词的高频虚词, 词根比较时去掉, 否则光靠 "for"/"with" 这类词就能拆开本该算
#: 同一组的搜索词 (如 "rain jacket for men" vs "rain jackets men")。
OVERLAP_STOPWORDS = {"for", "a", "an", "the", "and", "or", "of", "to", "in", "on", "with",
                     "your", "my", "our", "is", "are"}


def _stem_word(w: str) -> str:
    """极简规则式词形还原, 只处理常见复数/进行时/过去式, 不追求语言学精确——
    目的是让 'jacket'/'jackets'/'jacketed' 这类同根词在比较时算相同, 不是真正的词干提取。"""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 3 and w.endswith("es") and not w.endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def normalize_term(term: str) -> frozenset[str]:
    """搜索词 -> 词根集合(忽略虚词和单词变形), 比较这个集合判断"除变形外是否相同"。
    集合而非顺序列表, 所以词序不同也算相同——"rain jacket men" 和 "men rain jacket" 等价。"""
    words = re.findall(r"[a-z0-9]+", term.lower())
    return frozenset(_stem_word(w) for w in words if w not in OVERLAP_STOPWORDS)


def detect_product_line_overlap(search_term_rows: list[dict], positioning: dict[str, dict],
                                default_base: dict, all_base: dict) -> list[dict]:
    """词根相同(忽略虚词和单复数/进行时等单词变形)的搜索词如果被多个活动同时抓到,
    按 产品定位.yaml 里登记的类目/阶段/价格梯度组分成三种性质不同的情况:

    1. 同一个价格梯度组内竞争(如 03/05/07 梯度) —— 这是故意的价格分层, 同词同时出现是
       帮用户比价, 不建议减少投放, 只展示数据, 角色标"梯度内竞争(正常)"。
    2. 同类目、不在同一梯度组的竞争 —— 真正的内耗, 按订单优先/ACOS次之排名, 非头部
       标"建议减少投放/否定该词"(老逻辑)。
    3. 不同类目的竞争(如 sailing 蹭到 workwear 的词) —— 不建议切断(可能是在借该词导流),
       只展示、标"跨类目(观察)", 提示人工单独核算这条词在该类目账户里自己的转化质量。

    阶段=weak/new 的产品(卖得差/新品)不管落在哪种情况, 都不建议"减少投放"——它们缺的是
    词, 不是要收缩, 角色改标提示去看词库缺口报表。

    只读检测, 不产生任何 BID/预算动作, 不改账户。数据不足(点击不够)的组不参与排名。"""
    by_stem: dict[frozenset, dict[str, dict]] = {}
    variants: dict[frozenset, dict[str, int]] = {}
    for row in search_term_rows:
        term = (row.get("searchTerm") or "").strip().lower()
        if not term or ASIN_RE.fullmatch(term):
            continue  # ASIN商品词不是关键词重叠, 跟 expand_negative 的处理口径一致
        stem = normalize_term(term)
        if not stem:
            continue
        code = product_code(row.get("campaignName"))
        cid = str(row.get("campaignId") or "")
        agg = by_stem.setdefault(stem, {}).setdefault(cid, {
            "campaignId": cid, "campaignName": row.get("campaignName"), "productCode": code,
            "impressions": 0, "clicks": 0, "cost": 0.0, "sales7d": 0.0, "purchases7d": 0,
        })
        agg["impressions"] += int(row.get("impressions") or 0)
        agg["clicks"] += int(row.get("clicks") or 0)
        agg["cost"] += float(row.get("cost") or 0)
        agg["sales7d"] += float(row.get("sales7d") or 0)
        agg["purchases7d"] += int(row.get("purchases7d") or 0)
        vcount = variants.setdefault(stem, {})
        vcount[term] = vcount.get(term, 0) + int(row.get("clicks") or 0)

    def _acos(a: dict) -> float:
        return a["cost"] / a["sales7d"] if a["sales7d"] > 0 else float("inf")

    rows_out: list[dict] = []
    for stem, by_cid in by_stem.items():
        if len({str(a["campaignId"]) for a in by_cid.values()}) < 2:
            continue
        # 服务端只做一个很低的噪音底线(1 clicks), 不卡掉数据——真正的点击量/展示量阈值
        # 交给看板前端的滑杆实时调(用户要求"自主勾选阈值"), 否则调高阈值时已经被服务端
        # 过滤掉的数据没法再找回来。
        qualifying = [a for a in by_cid.values() if a["clicks"] >= 1]
        if len(qualifying) < 2:
            continue
        for a in qualifying:
            a["_pos"] = positioning_of(a["productCode"], positioning)

        # 按 (类目, 价格梯度组) 聚类: 梯度组非空的落进梯度簇(受保护, 不建议减少);
        # 没有梯度组的按类目聚类, 同类目内按老逻辑排名/建议减少; 类目里只有自己一个
        # 的, 单独标"跨类目(观察)"。
        clusters: dict[tuple, list[dict]] = {}
        for a in qualifying:
            pos = a["_pos"]
            key = ("ladder", pos["类目"], pos["价格梯度组"]) if pos["价格梯度组"] \
                else ("category", pos["类目"])
            clusters.setdefault(key, []).append(a)

        vcount = variants.get(stem, {})
        term_display = max(vcount, key=vcount.get) if vcount else "/".join(sorted(stem))
        term_variants = sorted(v for v in vcount if v != term_display)

        for key, members in clusters.items():
            ranked = sorted(members, key=lambda a: (-a["purchases7d"], _acos(a)))
            is_ladder = key[0] == "ladder"
            is_cross = len(members) < 2   # 类目里只有它自己一个, 跟 by_cid 里其它家竞争
            for rank, a in enumerate(ranked):
                stage = a["_pos"]["阶段"]
                if is_ladder:
                    role = "梯度内竞争(正常)" if rank == 0 else f"梯度内竞争(正常, 排第{rank + 1})"
                elif is_cross:
                    role = "跨类目(观察, 不建议调整)"
                elif stage in ("weak", "new"):
                    role = f"阶段={stage}, 不建议减少, 参考词库缺口报表"
                elif rank == 0:
                    role = "头部-建议保留"
                else:
                    role = f"排第{rank + 1}-建议减少投放/否定该词"
                acos = _acos(a)
                rows_out.append({
                    "词根组": term_display,
                    "搜索词变体": "; ".join(term_variants) if term_variants else "",
                    "竞争活动数": len(qualifying),
                    "活动": a["campaignName"], "产品码": a["productCode"],
                    "类目": a["_pos"]["类目"], "角色": role,
                    "impressions": a["impressions"], "clicks": a["clicks"], "cost": round(a["cost"], 2),
                    "sales7d": round(a["sales7d"], 2), "purchases7d": a["purchases7d"],
                    "acos": round(acos, 4) if acos != float("inf") else None,
                })
    rows_out.sort(key=lambda r: (r["词根组"], 0 if "头部" in r["角色"] else 1))
    return rows_out


def write_overlap_report(run_dir: Path, overlap_rows: list[dict], day: str) -> None:
    """产品线重叠.md —— 只读报告, 不是动作板块(不出 CSV, 不会被 execute 捡去执行)。"""
    if not overlap_rows:
        return
    groups: dict[str, list[dict]] = {}
    for r in overlap_rows:
        groups.setdefault(r["词根组"], []).append(r)
    with (run_dir / "产品线重叠.md").open("w", encoding="utf-8") as f:
        f.write(f"# 关键词重叠检测 {day} — {len(groups)} 组\n\n")
        f.write("词根相同(忽略虚词和单复数/进行时等变形)的搜索词被多个活动同时抓到。按 "
                "`产品定位.yaml` 分三种: **梯度内竞争(正常)**——同一价格梯度组, 不建议动; "
                "**头部/建议减少**——真正同类目内耗, 老逻辑排名; **跨类目(观察)**——不同类目"
                "蹭同一个词, 不建议切断, 需人工单独核算该词在该类目自己的转化质量。"
                "阶段=weak/new 的产品一律不建议减少, 去看词库缺口报表。只读, 不会自动改账户。"
                "数据来自30天 `搜索词历史` 报表(全账户, 不限AUTO)。\n\n")
        f.write("| 词根组 | 搜索词变体 | 活动 | 产品码 | 类目 | 角色 "
                "| clicks | cost | sales7d | purchases7d | ACOS |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|\n")
        for term, rows in groups.items():
            variants = next((r["搜索词变体"] for r in rows if r["搜索词变体"]), "")
            for r in rows:
                acos = f"{r['acos']:.1%}" if r["acos"] is not None else "-"
                f.write(f"| {term} | {variants} | {r['活动']} | {r['产品码']} | {r['类目']} | {r['角色']} "
                        f"| {r['clicks']} | {r['cost']} | {r['sales7d']} | {r['purchases7d']} | {acos} |\n")


def detect_shared_negative_candidates(ledger_success: dict, product_line_map: dict,
                                       active_campaigns: dict) -> list[dict]:
    """扫描执行台账里已成功执行的 否定/否定ASIN, 按(产品线canonical, 词, 匹配)分组统计
    独立命中过的活动数。同一个词被2个以上不同活动各自独立否定过, 说明这不是偶然噪音,
    是真实的跨产品线浪费词——列出同产品线里还没否定过这个词、当前仍启用的其它活动,
    供人工评估是否要主动补齐。只读提示, 不生成否定建议/不碰执行层, 跟 产品线重叠.md
    同一个定位(dashboard 命令里才跑, 不跟每天的 suggest 绑在一起)。"""
    groups: dict[tuple[str, str], set[str]] = {}
    match_of: dict[tuple[str, str], str] = {}
    for entry in ledger_success.values():
        row = entry.get("row", {})
        if row.get("动作") not in ("否定", "否定ASIN"):
            continue
        term = str(row.get("词", "")).strip().lower()
        cid = str(row.get("campaignId", ""))
        if not term or not cid:
            continue
        code = product_code(row.get("来源", ""))
        canon = product_line_map.get(code, code)
        key = (canon, term)
        groups.setdefault(key, set()).add(cid)
        match_of[key] = row.get("匹配", "")

    out = []
    for (canon, term), done_cids in groups.items():
        if len(done_cids) < 2:
            continue
        siblings = {cid: c for cid, c in active_campaigns.items()
                    if c.get("state") == "ENABLED" and cid not in done_cids
                    and product_line_map.get(product_code(c.get("name", "")),
                                              product_code(c.get("name", ""))) == canon}
        if not siblings:
            continue
        out.append({
            "产品线": canon, "词": term, "匹配": match_of[(canon, term)],
            "已否定活动数": len(done_cids),
            "待补齐活动": sorted(c.get("name", cid) for cid, c in siblings.items()),
        })
    out.sort(key=lambda r: (-r["已否定活动数"], r["产品线"], r["词"]))
    return out


def write_shared_negative_report(out_dir: Path, rows: list[dict], day: str) -> None:
    """共享否定候选.md —— 只读报告, 不是动作板块(不出 CSV, 不会被 execute 捡去执行)。"""
    if not rows:
        return
    with (out_dir / "共享否定候选.md").open("w", encoding="utf-8") as f:
        f.write(f"# 跨活动共享否定候选 {day} — {len(rows)} 条\n\n")
        f.write("同一个词已经在同产品线2个以上不同活动里各自独立被否定过——不是偶然噪音, "
                "是这条产品线真实的浪费词。下面列出同产品线里还没否定过这个词、当前仍启用"
                "的其它活动, 建议人工评估是否要主动在这些活动里也补一条否定, 不用等它们"
                "自己各花一遍钱才被规则发现。只读, 不会自动改账户。\n\n")
        f.write("| 产品线 | 词 | 匹配类型 | 已否定活动数 | 建议补齐活动 |\n"
                "|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['产品线']} | {r['词']} | {r['匹配']} | {r['已否定活动数']} "
                    f"| {', '.join(r['待补齐活动'])} |\n")


#: 种子词库: 美区雨衣品类购物搜索习惯里常见、但核心品类词("rain"/"gear"/"suit"/"men"换
#: 语序也凑不出来的修饰词/场景词。按类目分, generic 这组所有类目都加进候选池。这些是
#: 完全没有账户数据支撑的候选, 只在整个账户历史里都没出现过时才建议, 需要人工判断是否
#: 真的适合这个产品再去试。
SEED_VOCAB: dict[str, list[str]] = {
    "workwear": [
        "hi vis rain gear", "safety rain gear", "construction rain gear", "flagger rain suit",
        "utility rain gear", "class 3 rain gear", "road crew rain gear", "high visibility rain suit",
    ],
    "fishing": [
        "fishing rain gear", "foul weather gear", "offshore rain gear", "commercial fishing suit",
        "bibs and jacket set", "deckhand rain gear", "grundens style rain gear",
    ],
    "sailing": [
        "sailing foul weather gear", "offshore sailing jacket", "breathable waterproof sailing gear",
        "splash gear", "deck gear sailing", "ocean racing gear", "foulies",
    ],
    "generic": [
        "rain gear", "rain suit", "raincoat", "rainwear", "wet weather gear", "storm suit",
        "pvc rain gear", "heavy duty rain gear", "rain bibs", "rain overalls",
    ],
}


#: 每个弱势/新品最多展示这么多条"同类目已验证"机会词, 按订单数/ACOS排名取前列——
#: 账户历史里这类长尾匹配可能有几百条, 报告要能看, 不是要穷举。
MAX_GAP_PER_PRODUCT = 20


def detect_keyword_gaps(search_term_rows: list[dict], positioning: dict[str, dict],
                        seed_vocab: dict[str, list[str]] | None = None) -> list[dict]:
    """阶段=weak/new 的产品缺的是词, 不是要收缩投放——重叠检测对它们一律不建议减少
    (见 detect_product_line_overlap), 这份报告反过来找机会词:

    1. 同类目已验证 —— 同类目里别的产品已经出过单的词根, 这个产品自己账户里从没出现过
       (抄同类目作业)。
    2. 种子词库(未验证) —— 来自美区购物搜索习惯的候选词, 整个账户历史里都没出现过,
       没有任何数据支撑, 只是因为核心品类词太少(换语序也就那几个)而补充的场景词候选。

    只读, 不产生任何动作, 不改账户。"""
    seed_vocab = seed_vocab if seed_vocab is not None else SEED_VOCAB
    own_stems: dict[str, dict[frozenset, dict]] = {}
    variants: dict[frozenset, dict[str, int]] = {}
    for row in search_term_rows:
        term = (row.get("searchTerm") or "").strip().lower()
        if not term or ASIN_RE.fullmatch(term):
            continue
        stem = normalize_term(term)
        if not stem:
            continue
        code = product_code(row.get("campaignName"))
        agg = own_stems.setdefault(code, {}).setdefault(stem, {
            "impressions": 0, "clicks": 0, "cost": 0.0, "sales7d": 0.0, "purchases7d": 0,
        })
        agg["impressions"] += int(row.get("impressions") or 0)
        agg["clicks"] += int(row.get("clicks") or 0)
        agg["cost"] += float(row.get("cost") or 0)
        agg["sales7d"] += float(row.get("sales7d") or 0)
        agg["purchases7d"] += int(row.get("purchases7d") or 0)
        vcount = variants.setdefault(stem, {})
        vcount[term] = vcount.get(term, 0) + int(row.get("clicks") or 0)

    def _acos(a: dict) -> float:
        return a["cost"] / a["sales7d"] if a["sales7d"] > 0 else float("inf")

    def _display(stem: frozenset) -> str:
        vcount = variants.get(stem, {})
        return max(vcount, key=vcount.get) if vcount else "/".join(sorted(stem))

    # 同类目(排除自己)已出过单的词根, 按订单数/ACOS排名, 供"抄作业"用。
    by_category: dict[str, list[tuple[str, frozenset, dict]]] = {}
    for code, stems in own_stems.items():
        cat = positioning_of(code, positioning)["类目"]
        for stem, m in stems.items():
            if m["purchases7d"] >= 1:
                by_category.setdefault(cat, []).append((code, stem, m))
    for cat in by_category:
        by_category[cat].sort(key=lambda t: (-t[2]["purchases7d"], _acos(t[2])))

    all_used_anywhere = {s for stems in own_stems.values() for s in stems}

    rows_out: list[dict] = []
    for code, stems in own_stems.items():
        pos = positioning_of(code, positioning)
        if pos["阶段"] not in ("weak", "new"):
            continue
        cat = pos["类目"]
        used_stems = set(stems.keys())

        seen_gap: set[frozenset] = set()
        for src_code, stem, m in by_category.get(cat, []):
            if len(seen_gap) >= MAX_GAP_PER_PRODUCT:
                break
            if src_code == code or stem in used_stems or stem in seen_gap:
                continue
            seen_gap.add(stem)
            acos = _acos(m)
            rows_out.append({
                "产品码": code, "类目": cat, "阶段": pos["阶段"], "来源": "同类目已验证",
                "词根": _display(stem), "验证活动": src_code,
                "impressions": m["impressions"], "clicks": m["clicks"], "cost": round(m["cost"], 2),
                "purchases7d": m["purchases7d"],
                "acos": round(acos, 4) if acos != float("inf") else None,
            })

        for phrase in seed_vocab.get(cat, []) + seed_vocab.get("generic", []):
            stem = normalize_term(phrase)
            if not stem or stem in all_used_anywhere:
                continue
            rows_out.append({
                "产品码": code, "类目": cat, "阶段": pos["阶段"], "来源": "种子词库(未验证)",
                "词根": phrase, "验证活动": "",
                "impressions": None, "clicks": None, "cost": None, "purchases7d": None, "acos": None,
            })
    return rows_out


def aggregate_product_performance(search_term_rows: list[dict],
                                  positioning: dict[str, dict]) -> list[dict]:
    """每个产品代码30天总表现(不限定是不是重叠词)——给看板的"手动勾选产品比较"用,
    跟重叠检测是两回事: 重叠检测只看互相竞争的那些词, 这里看产品整体。只读, 不改账户。"""
    by_code: dict[str, dict] = {}
    for row in search_term_rows:
        term = (row.get("searchTerm") or "").strip().lower()
        if not term or ASIN_RE.fullmatch(term):
            continue
        code = product_code(row.get("campaignName"))
        agg = by_code.setdefault(code, {
            "产品码": code, "impressions": 0, "clicks": 0, "cost": 0.0,
            "sales7d": 0.0, "purchases7d": 0, "词根数": set(),
        })
        agg["impressions"] += int(row.get("impressions") or 0)
        agg["clicks"] += int(row.get("clicks") or 0)
        agg["cost"] += float(row.get("cost") or 0)
        agg["sales7d"] += float(row.get("sales7d") or 0)
        agg["purchases7d"] += int(row.get("purchases7d") or 0)
        stem = normalize_term(term)
        if stem:
            agg["词根数"].add(stem)

    out = []
    for code, a in by_code.items():
        pos = positioning_of(code, positioning)
        acos = a["cost"] / a["sales7d"] if a["sales7d"] > 0 else None
        out.append({
            "产品码": code, "类目": pos["类目"], "阶段": pos["阶段"],
            "clicks": a["clicks"], "cost": round(a["cost"], 2),
            "sales7d": round(a["sales7d"], 2), "purchases7d": a["purchases7d"],
            "acos": round(acos, 4) if acos is not None else None,
            "词根数": len(a["词根数"]),
        })
    out.sort(key=lambda r: -r["cost"])
    return out


def collect_product_stems(search_term_rows: list[dict]) -> dict[str, list[dict]]:
    """每个产品代码的完整词根表现明细(不限定是不是重叠词)——给看板"手动勾选产品/词根组
    比较"用: 选中几个产品后, 前端拿这份数据做集合运算, 分出"共同竞争的词根"和"各自独有
    的词根"(独有的=没被组内其它产品瓜分, 是listing/A+差异化定位的参考)。只读, 不改账户。

    每条词根记录带 _key (排序后的词根集合拼成的字符串), 前端用它做跨产品的集合比较——
    避免把 Python frozenset 直接扔进 JSON。"""
    by_code: dict[str, dict[frozenset, dict]] = {}
    variants: dict[frozenset, dict[str, int]] = {}
    for row in search_term_rows:
        term = (row.get("searchTerm") or "").strip().lower()
        if not term or ASIN_RE.fullmatch(term):
            continue
        stem = normalize_term(term)
        if not stem:
            continue
        code = product_code(row.get("campaignName"))
        agg = by_code.setdefault(code, {}).setdefault(stem, {
            "clicks": 0, "cost": 0.0, "sales7d": 0.0, "purchases7d": 0,
        })
        agg["clicks"] += int(row.get("clicks") or 0)
        agg["cost"] += float(row.get("cost") or 0)
        agg["sales7d"] += float(row.get("sales7d") or 0)
        agg["purchases7d"] += int(row.get("purchases7d") or 0)
        vcount = variants.setdefault(stem, {})
        vcount[term] = vcount.get(term, 0) + int(row.get("clicks") or 0)

    def _display(stem: frozenset) -> str:
        vcount = variants.get(stem, {})
        return max(vcount, key=vcount.get) if vcount else "/".join(sorted(stem))

    out: dict[str, list[dict]] = {}
    for code, stems in by_code.items():
        lst = []
        for stem, m in stems.items():
            acos = m["cost"] / m["sales7d"] if m["sales7d"] > 0 else None
            lst.append({
                "词根": _display(stem), "_key": "|".join(sorted(stem)),
                "clicks": m["clicks"], "cost": round(m["cost"], 2),
                "sales7d": round(m["sales7d"], 2), "purchases7d": m["purchases7d"],
                "acos": round(acos, 4) if acos is not None else None,
            })
        lst.sort(key=lambda r: -r["clicks"])
        out[code] = lst
    return out


def write_keyword_gap_report(run_dir: Path, gap_rows: list[dict], day: str) -> None:
    """词库缺口.md —— 只读报告, 不是动作板块。"""
    if not gap_rows:
        return
    groups: dict[str, list[dict]] = {}
    for r in gap_rows:
        groups.setdefault(r["产品码"], []).append(r)
    with (run_dir / "词库缺口.md").open("w", encoding="utf-8") as f:
        f.write(f"# 词库缺口/新词挖掘 {day} — {len(groups)} 个产品\n\n")
        f.write("阶段=weak(卖得差)/new(新品)的产品缺的是词、不是要收缩投放。"
                "**同类目已验证**: 同类目里别的产品已经出过单的词根, 这个产品自己账户里从没"
                "投放过。**种子词库(未验证)**: 来自美区购物搜索习惯的候选词, 整个账户历史里"
                "都没出现过, 没有数据支撑, 只供参考, 需要人工判断是否适合再去试投。"
                "只读, 不产生任何动作。\n\n")
        for code, rows in groups.items():
            pos_stage = rows[0]["阶段"]
            f.write(f"## {code} (阶段={pos_stage}, 类目={rows[0]['类目']})\n\n")
            verified = [r for r in rows if r["来源"] == "同类目已验证"]
            seed = [r for r in rows if r["来源"] == "种子词库(未验证)"]
            if verified:
                f.write("### 同类目已验证\n\n| 词根 | 验证活动 | clicks | cost | purchases7d | ACOS |\n"
                        "|---|---|---|---|---|---|\n")
                for r in verified:
                    acos = f"{r['acos']:.1%}" if r["acos"] is not None else "-"
                    f.write(f"| {r['词根']} | {r['验证活动']} | {r['clicks']} | {r['cost']} "
                            f"| {r['purchases7d']} | {acos} |\n")
                f.write("\n")
            if seed:
                f.write("### 种子词库(未验证, 无数据支撑)\n\n| 候选词 |\n|---|\n")
                for r in seed:
                    f.write(f"| {r['词根']} |\n")
                f.write("\n")


# 报告 placement 字段值 → API predicate 映射
PLACEMENT_MAP = {
    "top of search on-amazon": "PLACEMENT_TOP",
    "rest of search on-amazon": "PLACEMENT_REST_OF_SEARCH",
    "product pages on-amazon": "PLACEMENT_PRODUCT_PAGE",
    "detail page on-amazon": "PLACEMENT_PRODUCT_PAGE",
}


def get_placement_pct(campaign: dict, predicate: str) -> int:
    """从活动快照读当前位置竞价倍率 (%)，未设置时返回 0。"""
    for adj in (campaign.get("bidding") or {}).get("adjustments") or []:
        if adj.get("predicate") == predicate:
            return int(adj.get("percentage") or 0)
    return 0


def expand_placement_bid(hits, rule, campaign_snapshot: dict,
                         default_base: dict, all_base: dict) -> tuple[list[dict], list[dict]]:
    """调整活动位置竞价倍率 (TOS / ROS / PP)。每条行对应一个 (活动, 投放位置) 对。"""
    actions, skipped = [], []
    predicate = rule.get("投放位置", "PLACEMENT_TOP")
    direction = rule.get("方向", "减价")

    for r, env in hits:
        cid = str(r.get("campaignId") or "")
        campaign = campaign_snapshot.get(cid, {})
        current_pct = get_placement_pct(campaign, predicate)

        ratio = abs(float(eval_value(rule.get("比例表达式", ""), env) or rule.get("比例", 0.15)))
        ratio = min(ratio, float(env.get("placement_pct_cap", 0.3)))

        if direction == "加价":
            if current_pct == 0:
                new_pct = int(env.get("placement_pct_bootstrap", 10))
            else:
                new_pct = round(current_pct * (1 + ratio))
        else:
            new_pct = round(current_pct * (1 - ratio)) if current_pct > 0 else 0

        pct_min = int(env.get("placement_pct_min", 0))
        pct_max = int(env.get("placement_pct_max", 300))
        new_pct = min(max(new_pct, pct_min), pct_max)

        if new_pct == current_pct:
            skipped.append({"词": f"{predicate}@{cid}", "来源": r.get("campaignName"),
                             "原因": "倍率无变化 (已在边界)"})
            continue

        label = conf_value(rule.get("置信", "medium"), env)
        score, reason = conf_numeric(label, env, "BID")
        actions.append({
            "动作ID": action_id("pbid", r, f"{date.today()}:{cid}:{predicate}:{current_pct}->{new_pct}"),
            "动作": "位置BID", "层级": "PLACEMENT",
            "targetId": "", "campaignId": cid, "adGroupId": "",
            "投放位置": predicate,
            "原倍率": current_pct, "新倍率": new_pct,
            "规则名": rule.get("名称"), "来源": r.get("campaignName"),
            "产品码": product_code(r.get("campaignName")),
            "置信": label, "置信分": score, "置信理由": reason,
            "指标": (f"placement={r.get('placement')} acos={env.get('acos7d'):.1%} "
                    f"cost={r.get('cost')} purchases={r.get('purchases7d')}") if env.get("acos7d") else
                    f"placement={r.get('placement')} cost={r.get('cost')} purchases={r.get('purchases7d')}",
        })
    return actions, skipped


# ---------------------------------------------------------------- 板块

#: 板块 -> 触发它的规则动作。
#:
#: 这张表是"只跑一个板块"的全部机关: 选中的板块决定跑哪些规则, 规则的 `数据` 决定
#: 拉哪些报表 —— 没被选中的报表根本不会去亚马逊排队, 这是单独跑一个操作最大的
#: 时间节省 (全量 10~25 分钟, 单板块通常只等一两份报表)。
#: 加预算 / 减预算 同出自 动作=预算, 方向在出表时才分, 所以两者指向同一个动作。
BOARDS = {
    "收割": "收割",
    "加预算": "预算",
    "减预算": "预算",
    "否定": "否定",
    "BID调整": "BID",
    "位置调整": "位置BID",
}
#: 出表顺序, 也是文件名前缀 01-06 的顺序。
BOARD_ORDER = ["收割", "加预算", "减预算", "否定", "BID调整", "位置调整"]


def load_config() -> dict:
    return yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))


def resolve_boards(only) -> list[str]:
    """--only 的值 -> 要出哪些板块。None/空 = 全部。

    按这个顺序认, 先中的算:
      1. 板块全名      收割 -> 收割
      2. 动作名        BID -> BID调整;  预算 -> 加预算 + 减预算
      3. 板块名的一段   位置 -> 位置调整
    动作名排在模糊匹配之前, 否则 BID 会连 位置调整 (动作 位置BID) 一起命中。
    """
    if not only:
        return list(BOARD_ORDER)
    names = only if isinstance(only, list) else [only]
    wanted: list[str] = []
    for raw in names:
        key = str(raw).strip()
        hit = ([b for b in BOARD_ORDER if b == key]
               or [b for b in BOARD_ORDER if BOARDS[b] == key]
               or [b for b in BOARD_ORDER if key.lower() in b.lower()])
        if not hit:
            raise SystemExit(f"\n--only {raw!r} 不是板块。可选: {', '.join(BOARD_ORDER)}\n")
        wanted += [b for b in hit if b not in wanted]
    return [b for b in BOARD_ORDER if b in wanted]


def select(cfg: dict, boards: list[str]) -> tuple[list[dict], list[dict]]:
    """(要跑的规则, 要拉的报表规格) —— 其余的既不跑也不拉。"""
    actions = {BOARDS[b] for b in boards}
    if "收割" in actions:
        # REG2: 收割ASIN(竞品定位收割) 跟 收割(BROAD关键词收割) 并入同一个板块(01-收割.csv)，
        # 选"收割"板块时两种规则一起跑, 见 board_of() 对应的出表侧归并。
        actions.add("收割ASIN")
    if "否定" in actions:
        # 否定ASIN(AUTO高浪费定位负向定位) 跟 否定(关键词否定) 并入同一个板块(04-否定.csv)，
        # 同一套并入逻辑, 见 board_of()。
        actions.add("否定ASIN")
    rules = [r for r in cfg["规则"] if r["动作"] in actions]
    if not rules:
        raise SystemExit(f"\nconfig.yaml 的 规则 里没有 动作 属于 {sorted(actions)} 的，"
                         f"所以 {', '.join(boards)} 没有规则可跑。\n")
    needed = {r["数据"] for r in rules}
    needed |= {n + "历史" for n in needed}          # 证据检查要同口径的历史窗口
    specs = [s for s in cfg["报表"] if s["名称"] in needed]
    return rules, specs


# ---------------------------------------------------------------- 各阶段

def pull(client: AdsClient, cfg: dict, specs: list[dict],
         date_range: str | None, product: str | None) -> dict[str, list[dict]]:
    """拉报表 -> 只留自动广告 -> 配上历史窗口 -> (可选) 只留一个产品。"""
    specs = copy.deepcopy(specs)
    if date_range:
        _apply_date_range(specs, date_range)
        print(f"[范围] 自定义报告范围: {date_range}")

    conn = _open_pipeline_conn(cfg)
    try:
        reports = pull_reports(client, specs, cfg.get("轮询"))
        # campaignPlacement 分组必须显式请求 placementClassification 列才有值 (见
        # DIMENSION_COLUMNS); 历史上这里漏请求过, 导致位置表现报表一直没有能区分位置的
        # 字段, 下面的位置调整规则从未真正命中过。这里把它别名回 'placement'，跟
        # rules.py/optimization.py 其余代码已经在用的变量名对齐，不用到处改名。
        for name in ("位置表现", "位置表现历史"):
            for row in reports.get(name, []):
                row.setdefault("placement", row.get("placementClassification"))
        _dual_write_pipeline(conn, cfg, specs, reports)
    finally:
        if conn is not None:
            conn.close()
    # 规则引擎只管自动广告，但预警需要看全账户（手动KT-Broad/Exact也算），
    # 在仅自动广告过滤之前先留一份全量快照。
    all_campaigns_raw = aggregate(list(reports.get("广告活动", [])), "广告活动")
    all_search_terms_raw = aggregate(list(reports.get("搜索词", [])), "搜索词")
    auto_ids = load_auto_ids()
    active_groups = {str(g["adGroupId"])
                     for g in json.loads(ADGROUP_SNAPSHOT.read_text(encoding="utf-8"))
                     if g.get("state") == "ENABLED"}
    for spec in specs:
        if spec.get("仅自动广告"):
            name = spec["名称"]
            reports[name] = [r for r in reports[name]
                             if str(r.get("campaignId")) in auto_ids
                             and (not r.get("adGroupId") or str(r["adGroupId"]) in active_groups)]
    reports = prepare_reports(reports)
    reports["_广告活动_全账户"] = all_campaigns_raw
    reports["_搜索词_全账户"] = all_search_terms_raw

    if product:
        prod = product.upper()
        for name in list(reports):
            reports[name] = [r for r in reports[name]
                             if product_code(r.get("campaignName", "")) == prod]
        print(f"[过滤] 只处理产品代码: {prod}  (各表剩余行数: "
              + ", ".join(f"{k}={len(v)}" for k, v in reports.items() if not k.endswith("历史"))
              + ")")
    return reports


def load_context(cfg: dict, keywords: list, client: AdsClient | None = None) -> dict:
    """规则求值要用到的本地上下文: 阈值 / 保护词 / 快照 / AUTO->BROAD 映射。
    client 给了才会去查 PT(商品定位)活动当前已有的定向 (收割ASIN判重用, 没有本地快照可
    复用——PT活动的target不在 refresh_pool 同步范围内, 那边只同步AUTO活动的target)；
    不给(比如测试/不跑收割ASIN规则时)就跳过, pt_target_index 留空, 该规则自然不命中。"""
    default_base, all_base = load_baseline(HERE / cfg["基线文件"])
    kw_index = keyword_index(keywords)
    write_json((HERE / cfg["安全"]["广泛词索引"]).resolve(), kw_index)

    map_path = (HERE / cfg["安全"]["映射文件"]).resolve()
    map_data = json.loads(map_path.read_text(encoding="utf-8")) if map_path.exists() else {"map": {}}
    manual_path = HERE / "data" / "手动映射.json"
    if manual_path.exists():
        # 手动映射 (用户指定) 优先覆盖自动映射
        mdata = json.loads(manual_path.read_text(encoding="utf-8"))
        map_data.setdefault("map", {}).update(mdata.get("map") or {})
        print(f"[映射] 手动映射 {len(mdata.get('map') or {})} 条已合并")

    # 映射的目标活动/广告组必须在当前启用快照内; 广告组不可来自来源AUTO。
    active_cs = {str(c["campaignId"]): c
                 for c in json.loads(CAMPAIGN_SNAPSHOT.read_text(encoding="utf-8"))}
    active_ags = json.loads(ADGROUP_SNAPSHOT.read_text(encoding="utf-8"))
    for entry in map_data.get("map", {}).values():
        for target in entry.get("broad", []):
            target["cid"] = str(target["cid"])
            campaign = active_cs.get(target["cid"], {})
            target["state"] = (campaign.get("state", "UNKNOWN")
                               if campaign.get("targetingType") == "MANUAL" else "UNKNOWN")
        target = next((b for b in entry.get("broad", []) if b.get("state") == "ENABLED"), {})
        candidates = [str(g["adGroupId"]) for g in active_ags
                      if str(g.get("campaignId")) == target.get("cid")
                      and g.get("state") == "ENABLED"]
        requested = [str(x) for x in entry.get("adGroupIds", [])]
        selected = [g for g in requested if g in candidates]
        entry["adGroupIds"] = selected or (candidates if len(candidates) == 1 else [])
        # 广告组有歧义时要求人工映射; 即使活动已有关键词也不可随意选广告组。
        if not entry["adGroupIds"]:
            for b in entry.get("broad", []):
                b["state"] = "ADGROUP_MAPPING_REQUIRED"

    # REG2 竞品定位收割的目标索引: 产品码 -> 候选PT活动 (pt_campaign_index, 按命名识别),
    # + 这些PT活动当前已有的定向 (活塞查, 现查不缓存——这些活动数量少、刷新也不频繁,
    # 不值得像 keywords_state.json 那样另开一份本地快照维护成本)。
    pt_index = pt_campaign_index(active_cs)
    pt_target_index: dict[str, set[str]] = {}
    pt_campaign_ids = sorted({str(c["campaignId"]) for lst in pt_index.values() for c in lst})
    if client is not None and pt_campaign_ids:
        try:
            pt_targets = filtered_list(client.list_targets,
                                       {"campaignIdFilter": {"include": pt_campaign_ids}})
            for t in pt_targets:
                cid = str(t.get("campaignId"))
                for e in (t.get("expression") or []):
                    pt_target_index.setdefault(cid, set()).add(str(e.get("value") or "").lower())
        except Exception as exc:
            print(f"[定位收割] 查询PT活动现有定向失败, 本轮不出收割ASIN建议: {exc}")

    # AUTO高浪费ASIN定位-否定的判重索引: AUTO活动当前已有的负向定位 (现查, 原因同上——
    # 负向定位数量少、刷新也不频繁, 不值得另开本地快照)。
    auto_campaign_ids = sorted({str(c["campaignId"]) for c in active_cs.values()
                               if c.get("targetingType") == "AUTO"})
    neg_target_index: dict[str, set[str]] = {}
    if client is not None and auto_campaign_ids:
        try:
            neg_targets = []
            for i in range(0, len(auto_campaign_ids), 100):
                neg_targets += filtered_list(client.list_negative_targets,
                                             {"campaignIdFilter": {"include": auto_campaign_ids[i:i + 100]}})
            for t in neg_targets:
                cid = str(t.get("campaignId"))
                for e in (t.get("expression") or []):
                    neg_target_index.setdefault(cid, set()).add(str(e.get("value") or "").lower())
        except Exception as exc:
            print(f"[AUTO定位否定] 查询AUTO活动现有负向定位失败, 本轮不出否定ASIN建议: {exc}")

    return {"default_base": default_base, "all_base": all_base,
            "protected": load_protected(HERE / cfg["保护词文件"]),
            "neg_phrase_vocab": load_neg_phrase_vocab(
                (HERE / cfg["安全"].get("否定词组文件", "否定词组.yaml")).resolve()),
            "ignore_asin": bool(cfg["安全"].get("忽略ASIN", True)),
            "map_data": map_data, "kw_index": kw_index,
            "target_snapshot": load_target_snapshot(),
            "keyword_snapshot": load_keyword_snapshot(),
            "product_line_map": load_product_line_map(
                (HERE / cfg["安全"].get("产品线映射文件", "产品线.yaml")).resolve()),
            "positioning": load_product_positioning(
                (HERE / cfg["安全"].get("产品定位文件", "产品定位.yaml")).resolve()),
            "pt_index": pt_index, "pt_target_index": pt_target_index,
            "neg_target_index": neg_target_index,
            "campaigns": active_cs, "adgroups": active_ags}


def evaluate(cfg: dict, rules: list[dict], reports: dict, ctx: dict,
             client: AdsClient, keywords: list) -> tuple[list[dict], list[dict], set]:
    """跑规则 -> 动作建议。返回 (动作, 跳过明细, 命中过的搜索词)。"""
    default_base, all_base = ctx["default_base"], ctx["all_base"]
    protected = ctx["protected"]
    actions: list[dict] = []
    skipped: list[dict] = []
    matched_terms: set[tuple] = set()

    # 预算规则读的是快照里的当前预算, 报表里的那一列可能是旧的
    for r in reports.get("广告活动", []):
        budget = (ctx["campaigns"].get(str(r.get("campaignId")), {})
                  .get("budget", {}).get("budget"))
        if budget is not None:
            r["campaignBudgetAmount"] = budget

    base_env_cache: dict[str, dict] = {}

    def base_env_for(code: str) -> dict:
        if code not in base_env_cache:
            base_env_cache[code] = baseline_env(code, default_base, all_base)
        return base_env_cache[code]

    # 历史反馈: 索引一次, 给 BROAD 关键词 BID 用于看上次同向调整的效果。
    ledger_idx = ledger_index(read_json(HERE / "data" / "execution_ledger.json", {}).get("success", {}))

    # 关键词竞价 + 位置竞价联合优化: 每个活动算一次 compute_joint_bids, 给 expand_keyword_bid
    # (BROAD) 和 expand_bid (AUTO_TARGET) 共用——确保同一活动下 base bid 和位置 Bid
    # 朝同一个方向协同调整, 而不是各自为战。代表竞价优先用该活动 BROAD 关键词中位数,
    # 没有 BROAD 词的纯 AUTO 活动退回用 AUTO target 中位数/广告组默认竞价。
    pl_joint: dict[str, dict] = {}
    adgroup_snap = {str(g["adGroupId"]): g for g in ctx["adgroups"] if g.get("adGroupId")}
    placement_rows_all = reports.get("位置表现", [])
    if placement_rows_all:
        by_cid_pl: dict[str, list] = {}
        for row in placement_rows_all:
            by_cid_pl.setdefault(str(row.get("campaignId") or ""), []).append(row)
        for cid, rows_pl in by_cid_pl.items():
            base_bid = (_broad_campaign_base_bid(cid, ctx["keyword_snapshot"])
                        or _campaign_base_bid(cid, ctx["target_snapshot"], adgroup_snap))
            if not base_bid:
                continue
            benv = base_env_for(product_code(rows_pl[0].get("campaignName")))
            campaign = ctx["campaigns"].get(cid, {})
            current_pcts = {adj["predicate"]: int(adj.get("percentage") or 0)
                            for adj in (campaign.get("bidding") or {}).get("adjustments") or []
                            if adj.get("predicate")}
            joint = compute_joint_bids(base_bid, rows_pl, current_pcts, float(benv.get("target_acos", 0.3)),
                                       smoothing=float(benv.get("placement_smoothing", 0.4)),
                                       pct_min=int(benv.get("placement_pct_min", 0)),
                                       pct_max=int(benv.get("placement_pct_max", 300)),
                                       min_clicks=int(benv.get("placement_min_clicks", 20)))
            if joint:
                pl_joint[cid] = joint

    for rule in rules:
        rows = reports.get(rule["数据"], [])

        def env_of(row, _rule=rule):
            base_env = base_env_for(product_code(row.get("campaignName")))
            return make_env(row, base_env, protected, ctx.get("neg_phrase_vocab"), product_code,
                            ctx.get("positioning"))

        complete = []
        for r in rows:
            if all(r.get(k) is not None for k in ("cost", "clicks", "sales7d", "purchases7d")):
                complete.append(r)
            else:
                skipped.append({"词": r.get("searchTerm") or r.get("targeting") or r.get("campaignId"),
                                "来源": r.get("campaignName"), "原因": "关键指标缺失，保留观察"})

        hits = match_rows(complete, rule.get("过滤", ""), rule["条件"], env_of)
        hits, evidence_skipped = evidence_gate(hits, rule)
        skipped += evidence_skipped
        print(f"[规则] {rule['名称']}: 命中 {len(hits)} 行")
        for r, _ in hits:
            if r.get("searchTerm"):
                matched_terms.add((str(r.get("campaignId")), str(r.get("adGroupId")),
                                   r["searchTerm"].strip().lower()))

        if rule["动作"] == "否定":
            acts, neg_skipped = expand_negative(hits, rule.get("置信", "high"), ctx["ignore_asin"],
                                                phrase_rule=(rule.get("动作参数") == "词组否定"),
                                                map_data=ctx["map_data"])
            skipped += neg_skipped
        elif rule["动作"] == "收割":
            acts, harvest_skipped = expand_harvest(hits, ctx["map_data"], ctx["kw_index"],
                                                   rule.get("置信", "medium"),
                                                   default_base, all_base,
                                                   protected, ctx["ignore_asin"])
            acts, bid_skipped = enrich_harvest(acts, hits, client, cfg, keywords)
            skipped += harvest_skipped + bid_skipped
        elif rule["动作"] == "收割ASIN":
            acts, pt_skipped, pt_envs = expand_pt_harvest(hits, ctx["pt_index"], ctx["adgroups"],
                                                          ctx["pt_target_index"],
                                                          rule.get("置信", "medium"))
            acts, pt_bid_skipped = enrich_pt_harvest(acts, pt_envs, client, cfg)
            skipped += pt_skipped + pt_bid_skipped
        elif rule["动作"] == "否定ASIN":
            acts, negasin_skipped = expand_negative_asin(hits, rule.get("置信", "needs_human"),
                                                          ctx["neg_target_index"])
            skipped += negasin_skipped
        elif rule["动作"] == "预算":
            acts = expand_budget(hits, rule)
        elif rule["动作"] == "BID":
            if rule["数据"] == "BROAD投放":
                acts, bid_skipped = expand_keyword_bid(hits, rule, ctx["keyword_snapshot"],
                                                        default_base, all_base, protected,
                                                        ledger_idx, pl_joint)
            else:
                acts, bid_skipped = expand_bid(hits, rule, ctx["target_snapshot"],
                                               default_base, all_base, protected, pl_joint)
            skipped += bid_skipped
        elif rule["动作"] == "位置BID":
            acts, pl_skipped = expand_placement_bid(hits, rule, ctx["campaigns"],
                                                    default_base, all_base)
            skipped += pl_skipped
        else:
            print(f"  !! 未知动作: {rule['动作']}")
            continue
        for a in acts:
            a["规则名"] = rule["名称"]
            if rule.get("跳过冷却"):
                a["跳过冷却"] = True
        actions += acts

    # 联合优化: 用位置报表重算 base bid + placement pct
    placement_acts = [a for a in actions if a.get("动作") == "位置BID"]
    if placement_acts:
        actions = [a for a in actions if a.get("动作") != "位置BID"]
        actions += enrich_placement_bid(placement_acts, reports.get("位置表现", []),
                                        ctx["campaigns"], ctx["target_snapshot"],
                                        adgroup_snap, default_base, all_base)

    # 同产品线同词竞价梯度: 减少马甲活动之间互相抢同一个BROAD词
    broad_rows = reports.get("BROAD投放", [])
    if broad_rows:
        actions, grad_skipped = expand_product_line_gradient(
            broad_rows, ctx["keyword_snapshot"], ctx["product_line_map"],
            actions, default_base, all_base)
        skipped += grad_skipped

    return actions, skipped, matched_terms


def screen(cfg: dict, actions: list[dict], ctx: dict,
           skipped: list[dict]) -> list[dict]:
    """去重 -> 剔掉 pending/冷却期的 -> 单批上限。跳过的都写进 skipped。"""
    seen, deduped = set(), []
    for a in actions:
        key = (a["动作"], a.get("层级"), a.get("匹配"), a.get("targetId"), a.get("campaignId"),
               a.get("adGroupId"), str(a.get("词") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)

    ledger = read_json(HERE / "data" / "execution_ledger.json", {})
    pending = ledger.get("pending", {})
    success = ledger.get("success", {})
    kept = []
    for a in deduped:
        base = baseline_env(a.get("产品码", ""), ctx["default_base"], ctx["all_base"])
        if a.get("动作ID") in pending:
            skipped.append({"词": a.get("词") or a.get("投放组") or a.get("campaignId"),
                            "来源": a.get("来源"),
                            "原因": "上次提交结果不确定，需核实执行账本pending"})
        elif (a["动作"] in ("BID", "预算") and not a.get("跳过冷却")
              and cooldown(a, float(base.get("bid_cooldown_days", 3)), success)):
            skipped.append({"词": a.get("投放组") or a.get("campaignId"),
                            "来源": a.get("来源"), "原因": "执行后冷却期内"})
        else:
            a["规则名"] = a.get("规则名", "")
            kept.append(a)

    cap = cfg["安全"].get("单批上限", 200)
    if len(kept) > cap:
        print(f"!! 命中 {len(kept)} 条, 超过单批上限 {cap}, 只保留前 {cap} 条 (建议先调规则阈值)")
        kept = kept[:cap]
    return kept


# ---------------------------------------------------------------- 出表

#: 这几列是纯数字长ID, 写CSV时要加前导单引号强制Excel按文本处理, 否则Excel打开/
#: 保存时会把长数字转成科学计数法、精度丢失 (如 132542477142284 -> 1.32542E+14),
#: 导致 execute.py 按这个ID调用广告API时404/400。execute.load_rows() 读的时候会把
#: 这个单引号去掉还原成原始ID, 两边要配套改。
EXCEL_TEXT_GUARD_FIELDS = ("targetId", "campaignId", "adGroupId")

#: 建议表的列。CSV 的第一列是 批准, 审查时就改这一列。
FIELDS = ["批准", "动作ID", "动作", "层级", "匹配", "词", "targetId", "campaignId", "adGroupId",
          "投放组", "投放位置", "原Bid", "新Bid", "调整比例", "决策ACOS", "原倍率", "新倍率",
          "新预算", "原预算", "规则名", "来源", "产品码",
          "置信", "置信分", "置信理由", "指标", "竞价依据", "推荐区间", "协同建议",
          "保护状态", "跳过原因", "执行状态"]


def board_of(a: dict) -> str | None:
    """一条动作属于哪个板块 (预算按方向分加/减)。"""
    if a["动作"] == "BID":
        return "BID调整"
    if a["动作"] == "位置BID":
        return "位置调整"
    if a["动作"] in ("收割", "收割ASIN"):
        return "收割"
    if a["动作"] in ("否定", "否定ASIN"):
        return "否定"
    if a["动作"] == "预算":
        try:
            return "加预算" if float(a["新预算"]) > float(a["原预算"]) else "减预算"
        except (TypeError, ValueError, KeyError):
            return "加预算"
    return None


def write_boards(run_dir: Path, actions: list[dict], boards: list[str],
                 conf_threshold: int, day: str) -> dict[str, list[dict]]:
    """每个板块一份 CSV + 一份 MD, 各自独立批准、独立执行。返回写出的板块。"""
    grouped: dict[str, list[dict]] = {}
    for a in actions:
        b = board_of(a)
        if b in boards:
            grouped.setdefault(b, []).append(a)

    for i, name in enumerate(BOARD_ORDER, 1):
        items = grouped.get(name) or []
        if not items:
            continue
        csv_path = run_dir / f"{i:02d}-{name}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for a in items:
                a.setdefault("批准", "是" if (int(a.get("置信分", 0)) >= conf_threshold
                                             and a.get("置信") != "needs_human") else "否")
                a.setdefault("执行状态", "待执行")
                row = dict(a)
                for k in EXCEL_TEXT_GUARD_FIELDS:
                    v = row.get(k)
                    if v not in (None, ""):
                        row[k] = "'" + str(v)
                w.writerow(row)
        with (run_dir / f"{i:02d}-{name}.md").open("w", encoding="utf-8") as f:
            f.write(f"# {name}建议 {day} — {len(items)} 条\n\n")
            f.write("批准方式: 删除不要的行 (或把 批准 改为 否)。\n\n")
            f.write(f"干跑: `python run.py execute {csv_path.name}` ｜ "
                    f"执行: `python run.py execute --go {csv_path.name}`\n\n")
            f.write("| 动作 | 词 | 产品码 | 置信 | 来源活动 | 指标 |\n|---|---|---|---|---|---|\n")
            for a in items:
                f.write(f"| {a['动作']} | {a.get('词') or a.get('新预算','')} "
                        f"| {a.get('产品码','')} | {a.get('置信','')} "
                        f"| {a.get('来源','')} | {a.get('指标','')} |\n")
    return grouped


def write_overview(run_dir: Path, grouped: dict, skipped: list[dict], watchlist: list[dict],
                   boards: list[str], product: str | None, watch_min: float, day: str) -> None:
    """总览.md: 板块索引 + 跳过明细 + 观察名单 + 下一步命令。"""
    with (run_dir / "总览.md").open("w", encoding="utf-8") as f:
        notes = []
        if product:
            notes.append(f"过滤产品: **{product.upper()}**")
        if len(boards) < len(BOARD_ORDER):
            notes.append(f"只出板块: **{', '.join(boards)}**（其余板块这一轮没有跑，"
                         f"别的报表也没有去拉）")
        f.write(f"# 自动广告建议总览 {day} ({run_dir.name})\n\n")
        for n in notes:
            f.write(f"> {n}\n")
        if notes:
            f.write("\n")
        f.write("| 板块 | 条数 | 文件 |\n|---|---|---|\n")
        for i, name in enumerate(BOARD_ORDER, 1):
            items = grouped.get(name) or []
            if items:
                f.write(f"| {name} | {len(items)} | {i:02d}-{name}.csv |\n")
        new_launch_codes = sorted({a.get("产品码") for items in grouped.values() for a in items
                                   if str(a.get("规则名", "")).startswith("新品冲量-") and a.get("产品码")})
        if new_launch_codes:
            f.write(f"\n## 新品冲量期提示 ({'/'.join(new_launch_codes)})\n\n"
                    "以下规则引擎管不到、需要人工跟进的事项 (本轮看完可以直接忽略/删掉这段，"
                    "不长期保留):\n\n"
                    "- Listing图片/A+页面是否已按新款更新，首图差评/尺码表是否清晰；\n"
                    "- 评价(review)数量/速度是否跟得上冲量节奏，必要时协调Vine/站外评价；\n"
                    "- 冲量期加预算/加Bid建议基于曝光信号而非订单证明 (置信度medium)，"
                    "批准前建议人工看一眼具体活动再放量；\n"
                    "- 新品保护期(`new_launch_protect_days`，默认20天)结束后，"
                    "零单/浪费类规则会自动恢复生效，无需手动切回。\n")
        f.write(f"\n跳过 {len(skipped)} 条, 观察名单 {len(watchlist)} 条, 明细在下方。\n")
        f.write(f"\n整批干跑: `python run.py execute \"{run_dir.name}\"`\n")
        if skipped:
            f.write(f"\n## 跳过明细 ({len(skipped)})\n\n| 词 | 来源 | 原因 |\n|---|---|---|\n")
            for s in skipped:
                f.write(f"| {s['词']} | {s['来源']} | {s['原因']} |\n")
        if watchlist:
            f.write(f"\n## 观察名单 (花费≥{watch_min} 未触发规则, 共 {len(watchlist)})\n\n"
                    "| 词 | 来源 | cost | clicks | purchases |\n|---|---|---|---|---|\n")
            for r in watchlist:
                f.write(f"| {r.get('searchTerm')} | {r.get('campaignName')} | {r.get('cost')} "
                        f"| {r.get('clicks')} | {r.get('purchases7d')} |\n")


# ---------------------------------------------------------------- 操作

def suggest(product: str | None = None, date_range: str | None = None,
            only=None) -> int:
    """拉数据 -> 跑规则 -> 出建议表。对广告账户只读, 不改任何东西。

    --only 收割 之类只跑那一个板块的规则, 别的报表不会去亚马逊排队。
    """
    from ads_api import AdsClient

    cfg = load_config()
    keep = cfg.get("输出", {}).get("保留轮数", 2)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 2:
        raise ValueError("输出.保留轮数必须是至少2的整数")

    boards = resolve_boards(only)
    rules, specs = select(cfg, boards)
    if len(boards) < len(BOARD_ORDER):
        print(f"[板块] 只出: {', '.join(boards)}  —— 规则 {len(rules)} 条, "
              f"报表 {len(specs)} 份 ({', '.join(s['名称'] for s in specs)})")

    client = AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                       api_base=cfg.get("api_base"))
    refresh_pool(client, cfg)
    # 关键词全量同步只有收割要用 (判重 + 相似词竞价), 别的板块不必等它
    keywords = sync_keywords(client, cfg) if "收割" in boards else []

    reports = pull(client, cfg, specs, date_range, product)

    ctx = load_context(cfg, keywords, client)
    actions, skipped, matched_terms = evaluate(cfg, rules, reports, ctx, client, keywords)
    actions = screen(cfg, actions, ctx, skipped)

    # 观察名单: 有花费但未触发任何规则的搜索词
    watch_min = float(cfg["安全"].get("观察名单最低花费", 5))
    watchlist = [r for r in reports.get("搜索词", [])
                 if (r.get("searchTerm") or "").strip()
                 and float(r.get("cost") or 0) >= watch_min
                 and (str(r.get("campaignId")), str(r.get("adGroupId")),
                      r["searchTerm"].strip().lower()) not in matched_terms]
    watchlist.sort(key=lambda r: float(r.get("cost") or 0), reverse=True)

    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    day = date.today().isoformat()
    run_dir = next_run_dir(out_dir, day)

    conf_threshold = int(cfg.get("安全", {}).get("自动批准阈值", 70))
    grouped = write_boards(run_dir, actions, boards, conf_threshold, day)
    write_overview(run_dir, grouped, skipped, watchlist, boards, product, watch_min, day)
    write_json(run_dir / "run_complete.json", {
        "completed_at": time.time(), "actions": len(actions),
        "boards": boards,
        "product_filter": product.upper() if product else None,
        "date_range": date_range or None,
    })
    removed = prune_outputs(out_dir, run_dir, keep)
    if removed:
        print("[输出清理] 已删除旧轮次: " + ", ".join(removed))

    print(f"\n输出目录: {run_dir}")
    for name in BOARD_ORDER:
        if grouped.get(name):
            print(f"  {BOARD_ORDER.index(name) + 1:02d}-{name}.csv: {len(grouped[name])} 条")
    print(f'下一步: python run.py execute "{run_dir.name}"   (整批干跑), '
          f"或 --only <板块> 只跑一个板块")
    print("产品线重叠/词库缺口/历史看板都不是每天自动算的——想看最新的，不定期单独跑一次 "
          "python run.py dashboard。")
    return 0


def refresh_dashboards(product: str | None = None) -> int:
    """独立刷新产品线重叠/词库缺口相关的全部产出(重叠分析看板.html + dashboard.html +
    output/产品线重叠.md + output/词库缺口.md)，不跑任何规则、不出建议、不受 --only
    板块限制。这些都不是每天跟着 suggest 自动生成的——按需不定期跑这一个命令就够。

    重叠分析看板要用 30 天 搜索词历史(全账户) 现算 产品线重叠/词库缺口/各产品词根画像，
    这里单独拉这一份报表(走正常的仅自动广告=false口径)，不依赖、也不影响 suggest
    那边板块选择拉的报表。顺带也拉一份 商品定位投放(如果 config.yaml 里配了的话) ——
    这份专门抓手动商品定位(Product Targeting)活动的竞品ASIN/类目定向，跟 suggest 的
    板块选择同样无关，只为 dashboard.html 的竞品ASIN/类目投放表现两张表铺数据。
    dashboard.html 本身纯读 SQLite 历史库，不碰 API。"""
    from ads_api import AdsClient
    cfg = load_config()
    client = AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                       api_base=cfg.get("api_base"))
    refresh_pool(client, cfg)

    spec = next((s for s in cfg["报表"] if s["名称"] == "搜索词历史"), None)
    if spec is None:
        raise SystemExit("\nconfig.yaml 的 报表 里没有 搜索词历史，没法生成重叠分析看板。\n")
    specs = [spec]
    pt_spec = next((s for s in cfg["报表"] if s["名称"] == "商品定位投放"), None)
    if pt_spec is not None:
        specs.append(pt_spec)
    else:
        print("[看板][提示] config.yaml 没有 商品定位投放 这份报表配置，跳过——"
              "竞品ASIN/类目投放表现两张表只会看到 AUTO投放 里带出来的那部分，"
              "独立的手动商品定位活动不会被看到。")
    reports = pull(client, cfg, specs, None, product)
    rows = reports.get("搜索词历史", [])

    default_base, all_base = load_baseline(HERE / cfg["基线文件"])
    positioning = load_product_positioning(
        (HERE / cfg["安全"].get("产品定位文件", "产品定位.yaml")).resolve())

    overlap_rows = detect_product_line_overlap(rows, positioning, default_base, all_base)
    gap_rows = detect_keyword_gaps(rows, positioning)
    product_summary = aggregate_product_performance(rows, positioning)
    product_stems = collect_product_stems(rows)

    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    day = date.today().isoformat()
    # 产品线重叠.md/词库缺口.md 跟看板一样挪到这个独立命令里, 写在 output/ 根目录
    # (不是某次 suggest 的轮次目录), 每次跑这个命令就整份覆盖——不再跟着每天的
    # suggest 自动生成。
    write_overlap_report(out_dir, overlap_rows, day)
    write_keyword_gap_report(out_dir, gap_rows, day)

    product_line_map = load_product_line_map(
        (HERE / cfg["安全"].get("产品线映射文件", "产品线.yaml")).resolve())
    active_campaigns = {str(c["campaignId"]): c
                        for c in json.loads(CAMPAIGN_SNAPSHOT.read_text(encoding="utf-8"))}
    ledger_success = read_json(HERE / "data" / "execution_ledger.json", {}).get("success", {})
    shared_neg_rows = detect_shared_negative_candidates(ledger_success, product_line_map,
                                                        active_campaigns)
    write_shared_negative_report(out_dir, shared_neg_rows, day)
    print(f"共享否定候选.md: {len(shared_neg_rows)} 条跨活动共享否定候选" if shared_neg_rows
          else "共享否定候选.md: 本轮没有候选")

    import overlap_dashboard
    default_min_clicks = int(default_base.get("product_line_overlap_min_clicks", 5))
    overlap_dashboard.main(overlap_rows, gap_rows, product_summary, product_stems, day,
                           out_dir / "重叠分析看板.html", default_min_clicks=default_min_clicks)

    n_overlap_groups = len({r["词根组"] for r in overlap_rows})
    n_gap_products = len({r["产品码"] for r in gap_rows})
    print(f"产品线重叠.md: {n_overlap_groups} 组同词根多活动竞争" if n_overlap_groups
          else "产品线重叠.md: 本轮没有命中")
    print(f"词库缺口.md: {n_gap_products} 个弱势/新品有机会词候选" if n_gap_products
          else "词库缺口.md: 本轮没有候选")

    db_path = (cfg.get("管道") or {}).get("db_path")
    if db_path:
        try:
            import dashboard
            dashboard.main(db_path=(HERE / db_path).resolve(),
                           out_path=out_dir / "dashboard.html")
        except Exception as exc:
            print(f"[看板][警告] dashboard.html 重新生成失败: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        "\nrules.py 现在是被 run.py 调用的一步, 不要直接跑它。\n"
        "  python run.py suggest              出全部板块的建议\n"
        "  python run.py suggest --only 收割   只出收割\n"
        "  python run.py --list               有哪些操作\n")
