"""只读分析：按星期几聚合广告活动表现，寻找置信度较高的"周期性浪费"模式。

不改账户、不写 output/（那是 suggest/execute 流水线的地盘），结果单独落在
dayparting/reports/。Amazon Ads 报表接口不支持小时级粒度（timeUnit 只有
SUMMARY/DAILY），所以这里只能验证到"星期几"这一层——这是目前能拿到的、
统计上站得住脚的最细颗粒度；"几点之后"那层判断仍要靠人工在广告后台的
实时花费曲线去定，本脚本负责的是"值不值得对哪个产品的哪几天动手"。

用法（走项目根目录，不走 run.py，这是独立于主流程的实验性工具）:
    python dayparting/analyze_weekday.py --days 60
    python dayparting/analyze_weekday.py --days 60 --product NW007
"""
from __future__ import annotations
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent  # 项目根目录
sys.path.insert(0, str(HERE))

from ads_api import AdsClient
from rules import product_code, baseline_env, load_baseline, pull_reports

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def iso(days_ago: int) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="拉取的历史天数")
    ap.add_argument("--lag", type=int, default=3, help="归因等待天数")
    ap.add_argument("--product", default=None, help="只看某个产品码")
    ap.add_argument("--min-weeks", type=int, default=4,
                     help="每个星期几至少要出现这么多次才算数据足够，判定才算'置信度较高'")
    args = ap.parse_args()

    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    default_base, all_base = load_baseline(HERE / cfg.get("基线文件", "baseline.yaml"))

    client = AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                        api_base=cfg.get("api_base"))

    # Amazon DAILY 粒度的 spCampaigns 报表单次最长跨度 31 天，超过要切片多次请求再拼。
    end_anchor = iso(1 + args.lag)
    start_anchor = iso(args.days + args.lag)
    end_dt = datetime.strptime(end_anchor, "%Y-%m-%d")
    cursor = datetime.strptime(start_anchor, "%Y-%m-%d")
    specs = []
    chunk_idx = 0
    while cursor <= end_dt:
        chunk_end = min(cursor + timedelta(days=30), end_dt)
        specs.append({
            "名称": f"时段分析-活动-{chunk_idx}",
            "报告类型": "spCampaigns",
            "维度": ["campaign"],
            "列": ["campaignId", "campaignName", "date", "impressions", "clicks", "cost",
                   "sales7d", "purchases7d", "campaignStatus"],
            "天数": args.days,
            "_start": cursor.strftime("%Y-%m-%d"),
            "_end": chunk_end.strftime("%Y-%m-%d"),
            "时间单位": "DAILY",
        })
        cursor = chunk_end + timedelta(days=1)
        chunk_idx += 1

    raw = pull_reports(client, specs, cfg.get("轮询", {}))
    rows = [r for spec in specs for r in raw[spec["名称"]]]
    print(f"[分析] 拉到 {len(rows)} 行日度数据（{len(specs)} 份分片报表）")

    # product -> weekday -> 聚合
    agg: dict[str, dict[int, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"cost": 0.0, "sales": 0.0, "purchases": 0,
                                      "clicks": 0, "impr": 0, "days": set()}))
    overall: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "sales": 0.0, "purchases": 0})

    for r in rows:
        code = product_code(r.get("campaignName"))
        if args.product and code.upper() != args.product.upper():
            continue
        d = r.get("date")
        if not d:
            continue
        wd = datetime.strptime(d, "%Y-%m-%d").weekday()  # 0=周一
        cost = float(r.get("cost") or 0)
        sales = float(r.get("sales7d") or 0)
        purchases = int(r.get("purchases7d") or 0)
        clicks = int(r.get("clicks") or 0)
        impr = int(r.get("impressions") or 0)

        bucket = agg[code][wd]
        bucket["cost"] += cost
        bucket["sales"] += sales
        bucket["purchases"] += purchases
        bucket["clicks"] += clicks
        bucket["impr"] += impr
        bucket["days"].add(d)

        o = overall[code]
        o["cost"] += cost
        o["sales"] += sales
        o["purchases"] += purchases

    # 按总花费排行——花费越高、后面分析的时段调整潜在影响才越大
    waste_rank = []
    for code, o in overall.items():
        acos = o["cost"] / o["sales"] if o["sales"] else None
        waste_rank.append((code, o["cost"], o["sales"], acos))
    waste_rank.sort(key=lambda t: t[1], reverse=True)

    out_dir = HERE / "dayparting" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"时段分析_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.md"

    start_d, end_d = iso(args.days + args.lag), iso(1 + args.lag)
    lines = [
        f"# 周期浪费分析（{start_d} ~ {end_d}）",
        "",
        f"样本：{len(rows)} 行日度数据，覆盖 {len({r.get('date') for r in rows})} 个自然日。",
        "Amazon Ads 报表无小时粒度，本分析只到\"星期几\"层级，够不够判定见每个产品表格里的"
        "天数列——同一星期几出现次数 < `--min-weeks`（默认4）的不建议据此下结论。",
        "",
        "## 产品总花费排行（优先试验对象）",
        "",
        "| 产品码 | 总花费 | 总销售额 | ACOS |",
        "|---|---|---|---|",
    ]
    for code, cost, sales, acos in waste_rank[:15]:
        acos_s = f"{acos*100:.1f}%" if acos is not None else "无销售"
        lines.append(f"| {code} | {cost:.2f} | {sales:.2f} | {acos_s} |")

    lines.append("")
    lines.append("## 按星期分解（花费最高的10个产品）")

    for code, *_ in waste_rank[:10]:
        env = baseline_env(code, default_base, all_base)
        target_acos = float(env.get("target_acos", 0.3))
        total_cost = sum(b["cost"] for b in agg[code].values()) or 1.0

        lines.append("")
        lines.append(f"### {code}（target_acos={target_acos*100:.0f}%）")
        lines.append("")
        lines.append("| 星期 | 样本天数 | 花费 | 销售额 | ACOS | 花费占比 | 判定 |")
        lines.append("|---|---|---|---|---|---|---|")
        for wd in range(7):
            b = agg[code].get(wd)
            if not b:
                continue
            n_days = len(b["days"])
            acos = b["cost"] / b["sales"] if b["sales"] else None
            acos_s = f"{acos*100:.1f}%" if acos is not None else "无销售"
            share = b["cost"] / total_cost * 100

            if n_days < args.min_weeks:
                verdict = "样本不足，先别动"
            elif acos is not None and acos > target_acos * 1.3:
                verdict = "⚠️ 持续高ACOS，候选压缩对象"
            elif b["purchases"] == 0 and b["cost"] > 0:
                verdict = "⚠️ 零转化花费，候选压缩对象"
            else:
                verdict = "正常"

            lines.append(f"| {WEEKDAY_NAMES[wd]} | {n_days} | {b['cost']:.2f} | "
                          f"{b['sales']:.2f} | {acos_s} | {share:.0f}% | {verdict} |")

    out_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"[完成] 报告写入 {out_file}")


if __name__ == "__main__":
    main()
