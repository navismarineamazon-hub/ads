# -*- coding: utf-8 -*-
"""转化率下滑横向分析 (IRREG1，不定期手动跑，不进每日 suggest 流程)。

    python run.py cvr-trend
    python run.py cvr-trend --product P03

当前 7 天 vs 对比 7 天 (紧邻的前一个7天，不是重叠的14天窗口) 两期 campaign级数据，
按 广告类型(AUTO/BROAD/其它定位) -> 类目 -> 阶段 三层下钻比较CVR(purchases/clicks)，
找出"拖累整体转化率下降"最严重的具体分组，而不是只看一个账户级数字去猜。

只读: 跟 dashboard.py 一样不改账户，只调查询/报告接口。
IRREG 任务缓存跟每日 suggest 的 output/ 清理逻辑分开: 两期原始 campaign级行存进
data/cvr_trend_history/<时间戳>.json，只滚动保留最近 KEEP_ROUNDS 轮——这是给以后
想回看"上几次分析的原始数据"用的, 不是展示用的报告 (报告见 output/转化率下滑分析.md，
每次跑直接覆盖，不按轮次保留)。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else Path(__file__).resolve().parent
HISTORY_DIR = HERE / "data" / "cvr_trend_history"
KEEP_ROUNDS = 2
ATTRIBUTION_LAG_DAYS = 3  # 跟 config.yaml 其它报表同一个归因等待口径


def _campaign_spec(name: str, start: str, end: str) -> dict:
    return {
        "名称": name, "报告类型": "spCampaigns", "维度": ["campaign"],
        "指标": ["impressions", "clicks", "cost", "sales7d", "purchases7d",
                "campaignBudgetAmount", "campaignStatus"],
        "_start": start, "_end": end,
    }


def classify_ad_type(campaign: dict) -> str:
    """AUTO / BROAD及其它手动关键词活动 / PT(商品定位)。跟 rules.pt_campaign_index
    同一套"名字含POSITION/ASIN即算定位活动"的识别规则，这样三层下钻里的"广告类型"
    跟 REG2 收割的目标分类保持一致，不是另外发明一套。"""
    if campaign.get("targetingType") == "AUTO":
        return "AUTO"
    name = campaign.get("name") or ""
    if re.search(r"position|asin", name, re.IGNORECASE):
        return "PT/商品定位"
    return "BROAD/其它手动"


def _bucket(rows: list[dict], campaigns: dict, positioning: dict) -> dict[tuple, dict]:
    import rules
    buckets: dict[tuple, dict] = {}
    for r in rows:
        cid = str(r.get("campaignId"))
        campaign = campaigns.get(cid, {})
        code = rules.product_code(r.get("campaignName"))
        pos = rules.positioning_of(code, positioning)
        key = (classify_ad_type(campaign), pos["类目"], pos["阶段"])
        b = buckets.setdefault(key, {"clicks": 0.0, "purchases": 0.0, "cost": 0.0,
                                     "sales": 0.0, "campaigns": set()})
        b["clicks"] += float(r.get("clicks") or 0)
        b["purchases"] += float(r.get("purchases7d") or 0)
        b["cost"] += float(r.get("cost") or 0)
        b["sales"] += float(r.get("sales7d") or 0)
        b["campaigns"].add(str(r.get("campaignName")))
    return buckets


def _cvr(b: dict) -> float | None:
    return (b["purchases"] / b["clicks"]) if b.get("clicks", 0) > 0 else None


def _save_history(cur_rows: list[dict], prior_rows: list[dict], cur_spec: dict, prior_spec: dict) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (HISTORY_DIR / f"{stamp}.json").write_text(json.dumps({
        "当前窗口": {"start": cur_spec["_start"], "end": cur_spec["_end"]}, "当前原始行": cur_rows,
        "对比窗口": {"start": prior_spec["_start"], "end": prior_spec["_end"]}, "对比原始行": prior_rows,
    }, ensure_ascii=False), encoding="utf-8")
    rounds = sorted(HISTORY_DIR.glob("*.json"))
    for old in rounds[:-KEEP_ROUNDS]:
        old.unlink()


def _write_report(rows_out: list[dict], overall_c: float | None, overall_p: float | None,
                  cur_spec: dict, prior_spec: dict, product: str | None) -> Path:
    def pct(v):
        return f"{v * 100:.2f}%" if v is not None else "n/a"

    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "转化率下滑分析.md"
    with path.open("w", encoding="utf-8") as f:
        f.write("# 转化率下滑横向分析\n\n")
        f.write(f"当前窗口: {cur_spec['_start']} ~ {cur_spec['_end']} ｜ "
                f"对比窗口(紧邻的前一个7天): {prior_spec['_start']} ~ {prior_spec['_end']}\n\n")
        if product:
            f.write(f"> 过滤产品: **{product.upper()}**\n\n")
        f.write(f"**账户整体CVR**: {pct(overall_c)} (对比期 {pct(overall_p)})")
        if overall_c is not None and overall_p is not None:
            delta = overall_c - overall_p
            f.write(f" —— {'下降' if delta < 0 else '上升'} {abs(delta) * 100:.2f} 个百分点\n\n")
        else:
            f.write("  (某一期没有点击数据，无法比较)\n\n")
        f.write("按 广告类型 → 类目 → 阶段 三层下钻，按\"流失订单估计\"(= 当前期clicks ×\n"
                "(对比期CVR − 当前期CVR)；正数代表这组CVR下降在吃订单，负数代表这组CVR反而\n"
                "上升、不是下滑的来源) 降序排列——越靠前越可能是拖累整体转化率的那一块:\n\n")
        f.write("| 广告类型 | 类目 | 阶段 | 当前CVR | 对比CVR | 流失订单估计 | 当前订单 | "
                "对比订单 | 当前clicks | 对比clicks | 活动数 |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in rows_out:
            lost = r["流失订单估计"]
            f.write(f"| {r['广告类型']} | {r['类目']} | {r['阶段']} | {pct(r['当前CVR'])} "
                    f"| {pct(r['对比CVR'])} | {f'{lost:+.1f}' if lost is not None else 'n/a'} "
                    f"| {r['当前订单']:g} | {r['对比订单']:g} | {r['当前clicks']:g} "
                    f"| {r['对比clicks']:g} | {r['活动数']} |\n")
        f.write("\n没有可比数据(某一期该分组clicks=0)的行排在最后，流失订单估计显示 n/a。\n")
    return path


def run(product: str | None = None) -> int:
    """拉当前/对比两个7天窗口的campaign级报表 -> 按广告类型/类目/阶段下钻CVR对比 ->
    写 output/转化率下滑分析.md。只读，不改账户。"""
    import rules
    from ads_api import AdsClient

    cfg = rules.load_config()
    client = AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                       api_base=cfg.get("api_base"))
    rules.refresh_pool(client, cfg)
    campaigns = {str(c["campaignId"]): c
                for c in json.loads(rules.CAMPAIGN_SNAPSHOT.read_text(encoding="utf-8"))}
    positioning = rules.load_product_positioning(
        (HERE / cfg["安全"].get("产品定位文件", "产品定位.yaml")).resolve())

    lag = ATTRIBUTION_LAG_DAYS
    cur_spec = _campaign_spec("CVR当前7天", rules.iso(7 + lag), rules.iso(1 + lag))
    prior_spec = _campaign_spec("CVR对比7天", rules.iso(14 + lag), rules.iso(8 + lag))
    reports = rules.pull_reports(client, [cur_spec, prior_spec], cfg.get("轮询"))
    cur_rows = rules.aggregate(reports.get("CVR当前7天", []), "广告活动")
    prior_rows = rules.aggregate(reports.get("CVR对比7天", []), "广告活动")

    if product:
        prod = product.upper()
        cur_rows = [r for r in cur_rows if rules.product_code(r.get("campaignName", "")) == prod]
        prior_rows = [r for r in prior_rows if rules.product_code(r.get("campaignName", "")) == prod]
        print(f"[过滤] 只处理产品代码: {prod}  (当前期 {len(cur_rows)} 条 / 对比期 {len(prior_rows)} 条)")

    cur_b, prior_b = _bucket(cur_rows, campaigns, positioning), _bucket(prior_rows, campaigns, positioning)
    keys = sorted(set(cur_b) | set(prior_b))

    rows_out = []
    for key in keys:
        c, p = cur_b.get(key, {}), prior_b.get(key, {})
        cvr_c, cvr_p = _cvr(c), _cvr(p)
        lost_orders = c.get("clicks", 0.0) * (cvr_p - cvr_c) if (cvr_c is not None and cvr_p is not None) else None
        rows_out.append({
            "广告类型": key[0], "类目": key[1], "阶段": key[2],
            "当前CVR": cvr_c, "对比CVR": cvr_p,
            "当前clicks": c.get("clicks", 0.0), "对比clicks": p.get("clicks", 0.0),
            "当前订单": c.get("purchases", 0.0), "对比订单": p.get("purchases", 0.0),
            "流失订单估计": lost_orders,
            "活动数": len(c.get("campaigns", set()) | p.get("campaigns", set())),
        })
    rows_out.sort(key=lambda r: (r["流失订单估计"] is None, -(r["流失订单估计"] or 0)))

    overall_c = _cvr({"purchases": sum(b["purchases"] for b in cur_b.values()),
                      "clicks": sum(b["clicks"] for b in cur_b.values())})
    overall_p = _cvr({"purchases": sum(b["purchases"] for b in prior_b.values()),
                      "clicks": sum(b["clicks"] for b in prior_b.values())})

    path = _write_report(rows_out, overall_c, overall_p, cur_spec, prior_spec, product)
    _save_history(cur_rows, prior_rows, cur_spec, prior_spec)
    print(f"\n输出: {path}")
    print("原始两期campaign级数据存进 data/cvr_trend_history/ (滚动保留最近2轮)。")
    return 0
