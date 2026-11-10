# -*- coding: utf-8 -*-
"""周报 (REG，定期任务，不并入每日 suggest，也不是纯粹独立track——产出合并成一份周报)。

    python run.py weekly-report
    python run.py weekly-report --product NW007

四节，构建顺序②→③→①→④，报告里按①②③④的阅读顺序排版:

① 品牌分析搜索词报告 (Brand Analytics, SP-API Reports API) —— 命中我方ASIN的热搜词。
② 搜索词报告收割EXACT缺口 (Ads API, 已有的 spSearchTerm 数据) —— 高转化但没有EXACT
   覆盖的词，只读建议，不接 execute.py，人工决定是否建EXACT活动。
③ 竞品ASIN商品目录定位 (SP-API Catalog Items API) —— 跟我方ASIN同类目/关键词的候选
   竞品ASIN，供人工判断是否加PT定位。
④ 变体转化偏移 (同SKU vs 其它SKU成交) —— 广告挂的是某个具体颜色/尺码(子ASIN)，但
   亚马逊经常把点击导向同一父体下的另一个变体成交；otherSku份额高说明算法已经在帮你
   换变体，值得人工看看广告挂的那个变体是不是该换/是不是断货/抢不到Buy Box。
   纯 advisory，不接 execute.py。

①③需要 sp_api.env (已存在) + asin_map.yaml (我方ASIN -> 产品码，需要用户自己维护；
缺失或缺某个ASIN条目时对应节只会说明原因，不会让整份周报失败)。④需要 Amazon Ads
报表里的 purchasesSameSku7d/purchasesOtherSku7d/salesOtherSku7d/unitsSoldOtherSku7d
字段 (2026-11-09 根据公开文档交叉核实的字段名，没有逐字跟官方 schema 核对过——账户
不支持这几列时会在②节報告创建阶段就 400，run() 会自动退回不带这几列重试，④节相应
显示"本轮未获取到变体归因数据"，不会让其它三节失败)。

只读: 不改账户任何设置，不触碰 execute.py。
输出 output/周报.md (固定文件名，每次覆盖，跟 cvr_trend.py 的"周期性分析报告"
惯例一致，不进每日 suggest 的带编号看板)。②④节原始数据滚动存
data/weekly_report_history/(最近2轮)。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else Path(__file__).resolve().parent
HISTORY_DIR = HERE / "data" / "weekly_report_history"
KEEP_ROUNDS = 2
ATTRIBUTION_LAG_DAYS = 3  # 跟 cvr_trend.py/config.yaml 其它报表同一个归因等待口径

# ④ 变体转化偏移: 列名未逐字核对官方 schema (见模块 docstring), 不支持时 run() 会退回
# 不带这几列重试。
OTHER_SKU_COLUMNS = ["purchasesSameSku7d", "purchasesOtherSku7d",
                     "salesOtherSku7d", "unitsSoldOtherSku7d"]
OTHER_SKU_MIN_PURCHASES = 3  # 总成交(sameSku+otherSku)低于这个样本太小, 不值得下结论
OTHER_SKU_SHARE_FLOOR = 0.3  # otherSku占比超过这个才写进报表, 避免偶发单笔成交的噪音


# =============================================================== ② EXACT覆盖缺口

def _search_term_spec(base_spec: dict, with_other_sku: bool = True) -> dict:
    """克隆 config.yaml 里现成的"搜索词"报表规格，去掉"仅自动广告"限制——②节要判断
    "转化好但没有EXACT覆盖"，理论上要看全账户(含BROAD/PT来源)曝出的搜索词，不只是
    AUTO来源，否则会漏掉已经在BROAD活动里转化很好、但从未被收割到EXACT的词。不改
    config.yaml 本身，只在这个模块里用一份改过名字的副本(报表缓存按名称+内容摘要
    分文件，改名避免跟每日 suggest 用的那份"搜索词"缓存混在一起)。with_other_sku=True
    时额外带上④节要用的 OTHER_SKU_COLUMNS；run() 第一次失败(列名不被支持)会用
    with_other_sku=False 重新克隆一份再试一次，不影响②节本身。"""
    import copy
    spec = copy.deepcopy(base_spec)
    spec["名称"] = "搜索词(周报全账户)"
    spec.pop("仅自动广告", None)
    if with_other_sku:
        cols = list(spec["列"])
        cols += [c for c in OTHER_SKU_COLUMNS if c not in cols]
        spec["列"] = cols
    return spec


def _exact_index(keyword_rows: list[dict]) -> dict[str, list[dict]]:
    """本地、只读的 EXACT-only 关键词索引(文本->命中行)。不改 optimization.keyword_index()
    (那个硬编码只认 BROAD，是每日 suggest 共享状态，按 REG 任务缓存隔离惯例不动它)。"""
    idx: dict[str, list[dict]] = {}
    for r in keyword_rows:
        if r.get("matchType") != "EXACT":
            continue
        term = str(r.get("keywordText") or "").strip().lower()
        if term:
            idx.setdefault(term, []).append(r)
    return idx


def _avg_bid(campaign_id: str, keyword_rows: list[dict]) -> float | None:
    """同活动下已有 EXACT/BROAD 关键词的 bid 均值，给②节的建议bid当ballpark用。
    没有就返回 None，不编造数字(这节是advisory-only，够用)。"""
    bids = [float(r["bid"]) for r in keyword_rows
           if str(r.get("campaignId")) == str(campaign_id)
           and r.get("matchType") in ("EXACT", "BROAD") and r.get("bid")]
    return (sum(bids) / len(bids)) if bids else None


def _harvest_candidates(rows: list[dict], default_base: dict, all_base: dict,
                        protected: set[str], harvest_condition: str,
                        exact_idx: dict, keyword_rows: list[dict]) -> list[dict]:
    """复用现成的"出单词-收割BROAD"规则条件字符串(不是手抄一遍阈值表达式，避免跟
    config.yaml 漂移)，过滤出命中阈值、且 _exact_index 查不到的词；ASIN商品词排除
    复用 rules.ASIN_RE，跟 expand_harvest 里排除ASIN词同一条正则。"""
    import rules
    out = []
    for row in rows:
        st = str(row.get("searchTerm") or "").strip().lower()
        if not st or rules.ASIN_RE.fullmatch(st) or st in exact_idx:
            continue
        code = rules.product_code(row.get("campaignName"))
        base = rules.baseline_env(code, default_base, all_base)
        env = rules.make_env(row, base, protected)
        if not rules.safe_eval(harvest_condition, env):
            continue
        out.append({
            "词": st, "产品码": code, "来源活动": row.get("campaignName"),
            "campaignId": row.get("campaignId"),
            "clicks": row.get("clicks"), "purchases7d": row.get("purchases7d"),
            "cost": row.get("cost"), "acos7d": env.get("acos7d"),
            "建议bidballpark": _avg_bid(row.get("campaignId"), keyword_rows),
        })
    out.sort(key=lambda r: -(float(r["purchases7d"] or 0)))
    return out


# =============================================================== ④ 变体转化偏移

def _variant_shift_candidates(rows: list[dict]) -> list[dict]:
    """按(产品码, 来源活动)聚合 purchasesSameSku7d/purchasesOtherSku7d, 算 otherSku份额——
    份额高说明亚马逊经常把这条广告的点击导向同一父体下的另一个变体成交, 广告挂的那个
    具体颜色/尺码可能该换, 或者该查一下是不是断货/没抢到Buy Box。纯advisory, 不接
    execute.py。行里没有这两个字段(账户不支持/本轮没拉到)直接跳过, 不编0。"""
    import rules
    agg: dict[tuple, dict] = {}
    for row in rows:
        same, other = row.get("purchasesSameSku7d"), row.get("purchasesOtherSku7d")
        if same is None or other is None:
            continue
        code = rules.product_code(row.get("campaignName"))
        key = (code, row.get("campaignName"))
        a = agg.setdefault(key, {"产品码": code, "来源活动": row.get("campaignName"),
                                 "sameSku7d": 0, "otherSku7d": 0, "otherSku销售额7d": 0.0})
        a["sameSku7d"] += int(same)
        a["otherSku7d"] += int(other)
        a["otherSku销售额7d"] += float(row.get("salesOtherSku7d") or 0)
    out = []
    for a in agg.values():
        total = a["sameSku7d"] + a["otherSku7d"]
        if total < OTHER_SKU_MIN_PURCHASES:
            continue
        share = a["otherSku7d"] / total
        if share < OTHER_SKU_SHARE_FLOOR:
            continue
        out.append({**a, "总成交": total, "otherSku份额": share})
    out.sort(key=lambda r: -r["otherSku份额"])
    return out


# =============================================================== ①③ 我方ASIN清单

def _my_asins() -> dict[str, str]:
    """asin_map.yaml: {ASIN: 产品码}，用户自己维护(各国ASIN来源文件格式不统一、法/西
    两站目前也没有源文件，这里不去猜解析——缺失/为空就让①③两节说明原因，不编数据)。"""
    import yaml
    path = HERE / "asin_map.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k).upper(): str(v) for k, v in data.items()}


def _row_asins(row) -> set[str]:
    """递归扫一行里所有字符串值，挑出形如 ASIN 的 (B0+8位字母数字)。品牌分析报告
    下载后的确切字段名(clickedAsin1/purchasedAsin1之类)没有100%核实过，用这种
    通用扫描代替硬编字段名，不容易因为字段名拼错而悄悄漏判。"""
    import rules
    found: set[str] = set()

    def walk(v):
        if isinstance(v, str) and rules.ASIN_RE.fullmatch(v):
            found.add(v.upper())
        elif isinstance(v, dict):
            for vv in v.values():
                walk(vv)
        elif isinstance(v, list):
            for vv in v:
                walk(vv)
    walk(row)
    return found


def _last_full_week() -> tuple[str, str]:
    """最近一个完整的周日~周六(Brand Analytics报告按日历周对齐，WEEK周期要求
    dataStartTime是周日/dataEndTime是周六)，再退3天归因等待，跟其它报表同口径。"""
    anchor = date.today() - timedelta(days=ATTRIBUTION_LAG_DAYS)
    days_since_saturday = (anchor.weekday() - 5) % 7
    last_saturday = anchor - timedelta(days=days_since_saturday)
    last_sunday = last_saturday - timedelta(days=6)
    return last_sunday.isoformat(), last_saturday.isoformat()


def _site_code() -> str | None:
    site_dir = os.environ.get("ADS_SITE_DIR")
    return Path(site_dir).name if site_dir else None


def _section3_competitor_asins(my_asins: dict[str, str]) -> dict:
    """③ 竞品ASIN商品目录定位 (Catalog Items API)。"""
    from sp_api_client import SpApiClient, SpApiCredentialsMissing, NotAuthorized, site_market
    if not my_asins:
        return {"ok": False, "reason": "asin_map.yaml 未配置或为空，没有我方ASIN可以用来找竞品。"}
    try:
        region, marketplace_id = site_market(_site_code())
        client = SpApiClient(HERE / "sp_api.env", HERE / "data")
        rows = []
        for asin, code in my_asins.items():
            item = client.get_catalog_item(asin, marketplace_id, region)
            summary = (item.get("summaries") or [{}])[0]
            title, brand = summary.get("itemName") or "", (summary.get("brand") or "").lower()
            words = [w for w in re.findall(r"[A-Za-z]{3,}", title) if w.lower() != brand][:6]
            if not words:
                continue
            for c in client.search_catalog_items(words, marketplace_id, region):
                cand_asin = c.get("asin")
                if not cand_asin or cand_asin.upper() in my_asins:
                    continue
                csum = (c.get("summaries") or [{}])[0]
                rows.append({"我方ASIN": asin, "产品码": code, "候选竞品ASIN": cand_asin,
                            "候选标题": csum.get("itemName", "")})
        return {"ok": True, "rows": rows}
    except (SpApiCredentialsMissing, NotAuthorized, ValueError) as e:
        return {"ok": False, "reason": str(e)}


def _section1_brand_analytics(my_asins: dict[str, str]) -> dict:
    """① 品牌分析搜索词报告 (Brand Analytics Report)。"""
    from sp_api_client import SpApiClient, SpApiCredentialsMissing, NotAuthorized, site_market
    if not my_asins:
        return {"ok": False, "reason": "asin_map.yaml 未配置或为空，无法判断品牌分析报告里哪些行跟我方相关。"}
    try:
        region, marketplace_id = site_market(_site_code())
        client = SpApiClient(HERE / "sp_api.env", HERE / "data")
        week_start, week_end = _last_full_week()
        raw_rows = client.pull_brand_analytics_search_terms(marketplace_id, region, week_start, week_end)
        hits = []
        for row in raw_rows:
            matched = _row_asins(row) & set(my_asins)
            if matched:
                hits.append({"搜索词": row.get("searchTerm") or row.get("searchQuery") or "(字段名未知,见原始行)",
                            "命中我方ASIN": sorted(matched), "搜索频率排名": row.get("searchFrequencyRank"),
                            "原始行": row})
        return {"ok": True, "rows": hits, "week": (week_start, week_end), "total_raw": len(raw_rows)}
    except (SpApiCredentialsMissing, NotAuthorized, ValueError) as e:
        return {"ok": False, "reason": str(e)}


# =============================================================== 落地/缓存

def _save_history(search_term_rows: list[dict], section1: dict, section3: dict,
                  section4_rows: list[dict]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (HISTORY_DIR / f"{stamp}.json").write_text(json.dumps({
        "②搜索词原始行": search_term_rows, "①品牌分析结果": section1, "③竞品ASIN结果": section3,
        "④变体转化偏移": section4_rows,
    }, ensure_ascii=False), encoding="utf-8")
    rounds = sorted(HISTORY_DIR.glob("*.json"))
    for old in rounds[:-KEEP_ROUNDS]:
        old.unlink()


def _write_report(section1: dict, section2_rows: list[dict], section3: dict,
                  section4_rows: list[dict] | None, product: str | None) -> Path:
    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "周报.md"
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# 周报\n\n生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if product:
            f.write(f"\n> 过滤产品: **{product.upper()}**\n")

        f.write("\n## ① 品牌分析搜索词报告\n\n")
        if not section1.get("ok"):
            f.write(f"{section1.get('reason')}\n")
        elif not section1.get("rows"):
            f.write(f"窗口 {section1['week'][0]} ~ {section1['week'][1]}"
                    f"(共 {section1.get('total_raw', 0)} 条热搜词)，没有命中我方ASIN的行。\n")
        else:
            w = section1["week"]
            f.write(f"窗口 {w[0]} ~ {w[1]}，命中我方ASIN的热搜词:\n\n")
            f.write("| 搜索词 | 搜索频率排名 | 命中我方ASIN |\n|---|---|---|\n")
            for r in section1["rows"]:
                f.write(f"| {r['搜索词']} | {r.get('搜索频率排名', 'n/a')} | {', '.join(r['命中我方ASIN'])} |\n")

        f.write("\n## ② 高转化词缺少EXACT覆盖\n\n")
        f.write("只读建议，不会自动建活动/改账户——人工判断是否值得为这个词单独开一个EXACT活动。\n\n")
        if not section2_rows:
            f.write("\n本期没有命中阈值且缺EXACT覆盖的搜索词。\n")
        else:
            f.write("\n| 词 | 产品码 | 来源活动 | clicks | purchases7d | cost | acos7d | 建议bidballpark |\n"
                    "|---|---|---|---|---|---|---|---|\n")
            for r in section2_rows:
                acos = f"{r['acos7d'] * 100:.1f}%" if r.get("acos7d") is not None else "n/a"
                bid = f"{r['建议bidballpark']:.2f}" if r.get("建议bidballpark") is not None else "n/a"
                f.write(f"| {r['词']} | {r['产品码']} | {r['来源活动']} | {r.get('clicks','n/a')} "
                        f"| {r.get('purchases7d','n/a')} | {r.get('cost','n/a')} | {acos} | {bid} |\n")

        f.write("\n## ③ 竞品ASIN商品目录定位\n\n")
        f.write("候选竞品ASIN，供人工判断是否值得加商品定位(PT)——不会自动建定向。\n\n")
        if not section3.get("ok"):
            f.write(f"{section3.get('reason')}\n")
        elif not section3.get("rows"):
            f.write("没有找到候选竞品ASIN。\n")
        else:
            f.write("| 我方ASIN | 产品码 | 候选竞品ASIN | 候选标题 |\n|---|---|---|---|\n")
            for r in section3["rows"]:
                f.write(f"| {r['我方ASIN']} | {r['产品码']} | {r['候选竞品ASIN']} | {r['候选标题']} |\n")

        f.write("\n## ④ 变体转化偏移(同SKU vs 其它SKU成交)\n\n")
        f.write("otherSku份额高 = 亚马逊经常把这条广告的点击导向同一父体下的另一个颜色/尺码成交——"
                "广告挂的那个具体变体可能该换，或者该查一下是不是断货/没抢到Buy Box。"
                "只读建议，不会自动改广告。\n\n")
        if section4_rows is None:
            f.write("\n本轮未获取到变体归因数据(账户不支持这几列报表字段，已自动退回不带这几列重跑②节)。\n")
        elif not section4_rows:
            f.write(f"\n本期没有总成交≥{OTHER_SKU_MIN_PURCHASES}且otherSku份额≥{OTHER_SKU_SHARE_FLOOR*100:.0f}%的活动。\n")
        else:
            f.write("\n| 产品码 | 来源活动 | sameSku7d | otherSku7d | otherSku份额 | otherSku销售额7d |\n"
                    "|---|---|---|---|---|---|\n")
            for r in section4_rows:
                f.write(f"| {r['产品码']} | {r['来源活动']} | {r['sameSku7d']} | {r['otherSku7d']} "
                        f"| {r['otherSku份额']*100:.0f}% | {r['otherSku销售额7d']:.2f} |\n")
    return path


def run(product: str | None = None) -> int:
    import rules
    from ads_api import AdsClient

    cfg = rules.load_config()
    client = AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                       api_base=cfg.get("api_base"))
    rules.refresh_pool(client, cfg)
    keyword_rows = rules.sync_keywords(client, cfg)
    exact_idx = _exact_index(keyword_rows)

    base_spec = next(s for s in cfg["报表"] if s["名称"] == "搜索词")
    spec = _search_term_spec(base_spec, with_other_sku=True)
    got_other_sku = True
    try:
        reports = rules.pull_reports(client, [spec], cfg.get("轮询"))
    except RuntimeError as e:
        print(f"[④变体转化偏移] 带 OTHER_SKU_COLUMNS 的报告创建失败({e})，退回不带这几列重试...")
        got_other_sku = False
        spec = _search_term_spec(base_spec, with_other_sku=False)
        reports = rules.pull_reports(client, [spec], cfg.get("轮询"))
    st_rows = rules.aggregate(reports.get(spec["名称"], []), "搜索词")
    if product:
        prod = product.upper()
        st_rows = [r for r in st_rows if rules.product_code(r.get("campaignName", "")) == prod]
        print(f"[过滤] 只处理产品代码: {prod} ({len(st_rows)} 条搜索词)")

    default_base, all_base = rules.load_baseline((HERE / cfg["基线文件"]).resolve())
    protected = rules.load_protected((HERE / cfg["保护词文件"]).resolve())
    harvest_rule = next(r for r in cfg["规则"] if r["名称"] == "出单词-收割BROAD")
    section2_rows = _harvest_candidates(st_rows, default_base, all_base, protected,
                                        harvest_rule["条件"], exact_idx, keyword_rows)
    section4_rows = _variant_shift_candidates(st_rows) if got_other_sku else None

    my_asins = _my_asins()
    if product:
        my_asins = {a: c for a, c in my_asins.items() if c.upper() == product.upper()}
    section1 = _section1_brand_analytics(my_asins)
    section3 = _section3_competitor_asins(my_asins)

    path = _write_report(section1, section2_rows, section3, section4_rows, product)
    _save_history(st_rows, section1, section3, section4_rows or [])
    print(f"\n输出: {path}")
    print("②节原始搜索词数据滚动存 data/weekly_report_history/ (最近2轮)。")
    return 0
