"""时段预算调整引擎——按产品码 + 星期 + 具体时钟时间的规则，到点自动把预算
打折、离开窗口自动还原。

和 suggest/execute 主流程完全独立：不读/写 output/、不碰 baseline.yaml 或
execution_ledger.json。规则在 dayparting/schedule.yaml，执行记录在
dayparting/data/ledger.json，出了问题也不会波及主系统的冷却/历史反馈逻辑。

默认干跑（只打印要做什么，不改账户）。要真正写账户必须显式加 --go，和
execute.py 的安全习惯保持一致。

本脚本自己不会"到点触发"——它只负责"假如现在调用我，该做什么"。要做到
真正按时间自动执行，需要外部定时器反复调用它（例如 Windows 任务计划每
15 分钟跑一次 `python dayparting/apply_schedule.py --go`）；注册这个定时
任务需要你自己确认后再做，本脚本不会擅自写入系统任务计划。

用法:
    python dayparting/apply_schedule.py              # 干跑，只打印
    python dayparting/apply_schedule.py --go          # 真正写账户
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent  # 项目根目录
sys.path.insert(0, str(HERE))

from ads_api import AdsClient
from rules import product_code, load_baseline, baseline_env
from optimization import filtered_list

SCHEDULE_FILE = Path(__file__).resolve().parent / "schedule.yaml"
LEDGER_FILE = Path(__file__).resolve().parent / "data" / "ledger.json"

WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def _parse_hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_window(now: datetime, weekdays: list[int], start_s: str, end_s: str) -> bool:
    """窗口允许跨午夜（如 21:00→08:00，指"周六21点进，周日08点出"）。"""
    start, end = _parse_hm(start_s), _parse_hm(end_s)
    now_t = now.time()
    iso_wd = now.isoweekday()  # 1=周一..7=周日
    if start <= end:
        return iso_wd in weekdays and start <= now_t < end
    if iso_wd in weekdays and now_t >= start:
        return True
    prev_wd = 7 if iso_wd == 1 else iso_wd - 1
    return prev_wd in weekdays and now_t < end


def _load_ledger() -> dict:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return {}


def _save_ledger(data: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="真正写账户；不传只打印干跑结果")
    args = ap.parse_args()

    if not SCHEDULE_FILE.exists():
        print(f"[时段预算] 找不到规则文件 {SCHEDULE_FILE}")
        return
    rules = (yaml.safe_load(SCHEDULE_FILE.read_text(encoding="utf-8")) or {}).get("窗口", [])
    if not rules:
        print("[时段预算] schedule.yaml 没有定义任何窗口规则，什么都不做")
        return

    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    default_base, all_base = load_baseline(HERE / cfg.get("基线文件", "baseline.yaml"))
    client = AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                        api_base=cfg.get("api_base"))

    now = datetime.now()
    print(f"[时段预算] 当前时间 {now:%Y-%m-%d %H:%M} ({WEEKDAY_CN[now.isoweekday()]})"
          + ("  [--go，将实际写账户]" if args.go else "  [干跑，不会写账户]"))

    ledger = _load_ledger()

    products = {str(r["产品码"]).upper() for r in rules}
    campaigns = filtered_list(client.list_campaigns, {"stateFilter": {"include": ["ENABLED"]}})
    by_product: dict[str, list] = {}
    for c in campaigns:
        code = product_code(c.get("name"))
        if code in products:
            by_product.setdefault(code, []).append(c)

    to_write = []  # [(campaign, new_budget, reason, ledger_key, new_entry_or_None)]

    for rule in rules:
        code = str(rule["产品码"]).upper()
        rule_id = rule.get("名称") or f"{code}-{rule['星期']}-{rule['开始']}-{rule['结束']}"
        active = _in_window(now, rule["星期"], rule["开始"], rule["结束"])
        ratio = float(rule.get("预算倍率", 0.5))
        budget_min = float(baseline_env(code, default_base, all_base).get("budget_min", 1.0))

        for c in by_product.get(code, []):
            cid = str(c["campaignId"])
            cur_budget = float((c.get("budget") or {}).get("budget", 0) or 0)
            key = f"{cid}:{rule_id}"
            entry = ledger.get(key)

            if active and not entry:
                new_budget = max(budget_min, round(cur_budget * ratio, 2))
                if abs(new_budget - cur_budget) > 0.0001:
                    to_write.append((c, new_budget,
                                      f"进入窗口[{rule_id}] 降预算 {cur_budget}->{new_budget}",
                                      key, {"original_budget": cur_budget, "rule_id": rule_id,
                                            "entered_at": now.isoformat()}))
            elif not active and entry:
                orig = float(entry["original_budget"])
                if abs(orig - cur_budget) > 0.0001:
                    to_write.append((c, orig,
                                      f"离开窗口[{rule_id}] 还原预算 {cur_budget}->{orig}",
                                      key, None))
                else:
                    ledger.pop(key, None)  # 预算已经是原值了（可能人工改过），账本直接清掉

    if not to_write:
        print("[时段预算] 没有需要调整的活动。")
        _save_ledger(ledger)
        return

    for c, new_budget, reason, *_ in to_write:
        print(f"  活动 {c['campaignId']} {str(c.get('name',''))[:40]}: {reason}")

    if not args.go:
        print(f"[干跑] 共 {len(to_write)} 条，未写账户。确认无误后加 --go 执行。")
        return

    items = [{"campaignId": c["campaignId"], "name": c.get("name"),
              "budget": {"budgetType": "DAILY", "budget": new_budget}}
             for c, new_budget, *_ in to_write]
    ok_raw, err_raw = client.update_campaigns(items)
    ok_idx = {e.get("index") for e in (ok_raw or []) if isinstance(e, dict)}

    n_ok = 0
    for i, (c, new_budget, reason, key, new_entry) in enumerate(to_write):
        if i in ok_idx:
            n_ok += 1
            if new_entry:
                ledger[key] = new_entry
            else:
                ledger.pop(key, None)
        else:
            print(f"  !! 活动 {c['campaignId']} 写入未确认成功，账本不变，下次重跑会重试")
    if err_raw:
        print(f"[时段预算] 失败详情: {json.dumps(err_raw, ensure_ascii=False)[:500]}")
    _save_ledger(ledger)
    print(f"[时段预算] 完成，成功 {n_ok}/{len(to_write)} 条。")


if __name__ == "__main__":
    main()
