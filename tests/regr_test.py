# -*- coding: utf-8 -*-
"""规则引擎离线回归测试 (不联网): python tests/regr_test.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not (HERE / "baseline.yaml").exists():
    HERE = HERE.parent
sys.path.insert(0, str(HERE))

from rules import (match_rows, expand_harvest, expand_budget, expand_negative,
                   expand_pt_harvest, pt_campaign_index, classify_targeting,
                   expand_negative_asin,
                   make_env, baseline_env, load_baseline, product_code)
import optimization as opt

default_base, all_base = load_baseline(HERE / "baseline.yaml")
protected = {"navis", "fishing gear"}  # 模拟保护清单

rows = [
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "fishing jacket", "clicks": 30, "cost": 3.0, "sales7d": 30, "purchases7d": 1},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "rain suit", "clicks": 40, "cost": 12.5, "sales7d": 60, "purchases7d": 2},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "cold gear", "clicks": 30, "cost": 16.0, "sales7d": 0, "purchases7d": 0},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "navis", "clicks": 30, "cost": 20.0, "sales7d": 0, "purchases7d": 0},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "cheap", "clicks": 3, "cost": 6.0, "sales7d": 0, "purchases7d": 0},
    {"campaignId": "222", "campaignName": "NW009-AU-x", "adGroupId": "ag2",
     "searchTerm": "orphan term", "clicks": 5, "cost": 1.0, "sales7d": 0, "purchases7d": 1},
]


def env_of(row):
    return make_env(row, baseline_env(product_code(row.get("campaignName")), default_base, all_base), protected)


NEG = "purchases7d == 0 and cost >= no_order_spend_threshold and clicks >= no_order_click_threshold and not 保护词"
HARV = "purchases7d >= 1 and not 保护词"

neg = match_rows(rows, "", NEG, env_of)
harv = match_rows(rows, "", HARV, env_of)
assert [r["searchTerm"] for r, _ in neg] == ["cold gear"], [r["searchTerm"] for r, _ in neg]
assert [r["searchTerm"] for r, _ in harv] == ["fishing jacket", "rain suit", "orphan term"]

acts, skip = expand_harvest(
    harv,
    {"map": {"111": {"broad": [{"cid": "999", "state": "ENABLED"}], "adGroupIds": ["ag1"]}}},
    {"999": {"agX": {"rain suit": "ENABLED"}}},
    "high if purchases7d >= min_winner_orders else 'medium'",
    default_base, all_base, protected)
assert [(a["词"], a["置信"], a["campaignId"], a["adGroupId"]) for a in acts] == \
    [("fishing jacket", "medium", "999", "ag1")], acts
assert skip[0]["词"]=="rain suit" and "ENABLED" in skip[0]["原因"]
assert skip[1]["原因"]=="无对应BROAD活动"

crows = [{"campaignId": "111", "campaignName": "P03-AUTO", "campaignStatus": "ENABLED",
          "campaignBudgetAmount": 10.0, "cost": 9.5, "purchases7d": 3}]
bud = expand_budget(
    match_rows(crows, "campaignStatus == 'ENABLED'",
               "cost >= campaignBudgetAmount * 0.9 and purchases7d >= 2", env_of),
    {"参数": "campaignBudgetAmount + 5", "置信": "high"})
assert bud and bud[0]["新预算"] == 15.0, bud

negs, neg_skipped = expand_negative(neg, "high")
assert negs[0]["词"] == "cold gear" and negs[0]["匹配"] == "NEGATIVE_EXACT" and not neg_skipped

# 词组否定 + AUTO->BROAD 否定同步镜像 (否定词组.yaml 驱动, 见 config.yaml "类目尺码不符-否定")
vocab = {"类目词": {"boot"}, "尺码词": {"4xl"}, "尺码词例外产品码": {"P03"}}
cat_rows = [
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "winter boot", "clicks": 5, "cost": 2.0, "sales7d": 0, "purchases7d": 0},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "searchTerm": "4xl rain jacket", "clicks": 5, "cost": 2.0, "sales7d": 0, "purchases7d": 0},
]


def env_of_vocab(row):
    return make_env(row, baseline_env(product_code(row.get("campaignName")), default_base, all_base),
                    protected, vocab, product_code)


cat_hits = match_rows(cat_rows, "", "purchases7d == 0 and 类目尺码不符 and not 保护词", env_of_vocab)
assert [r["searchTerm"] for r, _ in cat_hits] == ["winter boot"], cat_hits  # P03 排除4xl(尺码词例外)
map_data = {"map": {"111": {"broad": [{"cid": "999", "state": "ENABLED"}], "adGroupIds": ["ag1broad"]}}}
cat_acts, cat_skip = expand_negative(cat_hits, "needs_human", phrase_rule=True, map_data=map_data)
by_match = {(a["campaignId"], a["adGroupId"], a["匹配"], a["词"]) for a in cat_acts}
assert ("111", "ag1", "NEGATIVE_EXACT", "winter boot") in by_match, cat_acts
assert ("111", "ag1", "NEGATIVE_PHRASE", "boot") in by_match, cat_acts
assert ("999", "ag1broad", "NEGATIVE_EXACT", "winter boot") in by_match, cat_acts  # 镜像到映射的BROAD
assert ("999", "ag1broad", "NEGATIVE_PHRASE", "boot") in by_match, cat_acts

# REG2: 竞品ASIN/类目定位收割 (收割ASIN -> 对应PT活动, 按命名识别目标 + 判重)
assert classify_targeting('asin="B0ABCDEFGH"') == ("asin", "B0ABCDEFGH")
assert classify_targeting('close-match') == (None, None)

pt_rows = [
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "targeting": 'asin="B0WINNER01"', "clicks": 20, "cost": 8.0, "sales7d": 40, "purchases7d": 3},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "targeting": 'category="12345"', "clicks": 20, "cost": 8.0, "sales7d": 40, "purchases7d": 3},
    {"campaignId": "222", "campaignName": "NW009-AU-x", "adGroupId": "ag2",
     "targeting": 'asin="B0NOTARGET"', "clicks": 20, "cost": 8.0, "sales7d": 40, "purchases7d": 3},
]


def env_of_pt(row):
    return make_env(row, baseline_env(product_code(row.get("campaignName")), default_base, all_base), protected)


pt_hits = match_rows(pt_rows, "", "purchases7d >= 1 and 是否竞品定位", env_of_pt)
assert len(pt_hits) == 3, pt_hits

pt_campaigns = {
    "999": {"campaignId": "999", "name": "P03-POSITION-0101", "targetingType": "MANUAL", "state": "ENABLED"},
}
pt_idx = pt_campaign_index(pt_campaigns)
assert pt_idx == {"P03": [pt_campaigns["999"]]}, pt_idx
pt_adgroups = [{"campaignId": "999", "adGroupId": "ag1pt", "state": "ENABLED"}]

pt_acts, pt_skip, pt_envs = expand_pt_harvest(
    pt_hits, pt_idx, pt_adgroups, {"999": {"b0winner01"}},  # B0WINNER01 已存在, 应判重跳过
    "high if purchases7d >= min_winner_orders else 'medium'")
by_word = {a["词"]: a for a in pt_acts}
assert "12345" in by_word and by_word["12345"]["匹配"] == "CATEGORY_SAME_AS", pt_acts
assert by_word["12345"]["campaignId"] == "999" and by_word["12345"]["adGroupId"] == "ag1pt"
assert "B0WINNER01" not in by_word, pt_acts  # 已存在定向, 判重跳过
assert any(s["词"] == "B0WINNER01" and "已存在" in s["原因"] for s in pt_skip), pt_skip
assert any(s["词"] == "B0NOTARGET" and s["原因"] == "无对应定位活动" for s in pt_skip), pt_skip
assert ("P03-AUTO-260613", "12345") in pt_envs, pt_envs

# AUTO高浪费ASIN定位-否定 (否定ASIN -> 负向定位加在来源AUTO活动自己的广告组, 判重用现查索引)
negasin_rows = [
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "targeting": 'asin="B0WASTE001"', "clicks": 30, "cost": 20.0, "sales7d": 0, "purchases7d": 0},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "targeting": 'category="99999"', "clicks": 30, "cost": 20.0, "sales7d": 0, "purchases7d": 0},
    {"campaignId": "111", "campaignName": "P03-AUTO-260613", "adGroupId": "ag1",
     "targeting": 'asin="B0ALREADY1"', "clicks": 30, "cost": 20.0, "sales7d": 0, "purchases7d": 0},
]
negasin_hits = match_rows(
    negasin_rows, "",
    "purchases7d == 0 and cost >= no_order_spend_threshold and clicks >= no_order_click_threshold "
    "and 是否竞品定位", env_of_pt)
assert len(negasin_hits) == 3, negasin_hits
negasin_acts, negasin_skip = expand_negative_asin(
    negasin_hits, "needs_human", {"111": {"b0already1"}})  # B0ALREADY1 已存在负向定位, 应判重跳过
by_negasin_word = {a["词"]: a for a in negasin_acts}
assert set(by_negasin_word) == {"B0WASTE001"}, negasin_acts  # 类目跳过, 已存在的跳过
assert by_negasin_word["B0WASTE001"]["匹配"] == "ASIN_SAME_AS"
assert by_negasin_word["B0WASTE001"]["campaignId"] == "111" and by_negasin_word["B0WASTE001"]["adGroupId"] == "ag1"
assert any(s["词"] == "99999" and s["原因"] == "类目定位不支持负向定位" for s in negasin_skip), negasin_skip
assert any(s["词"] == "B0ALREADY1" and "已存在负向定位" in s["原因"] for s in negasin_skip), negasin_skip

# 新品冲量期: 阶段=new + 上架日期 注入 make_env, 保护期内零单规则豁免/保护期外恢复 (config.yaml
# "...-否定"/"...-降Bid"/"...-减预算" 系列加的 "not (阶段=='new' and (新品天数 is None or
# 新品天数<=new_launch_protect_days))" 守卫)
from datetime import date, timedelta
from rules import days_since_launch, load_product_positioning

# 用 ZZTEST 这个不存在于 baseline.yaml 产品码覆盖段的代码, 保证本测试验证的是通用机制
# 本身 (new_launch_protect_days 走 default=20), 不受 NW001 等具体产品的单独覆盖值影响。
new_positioning = {"ZZTEST": {"类目": "workwear", "阶段": "new", "价格梯度组": "",
                              "上架日期": (date.today() - timedelta(days=5)).isoformat()}}
assert days_since_launch("ZZTEST", new_positioning) == 5
assert days_since_launch("ZZTEST", {}) is None  # 没配置该产品码 -> DEFAULT_POSITIONING, 上架日期None
assert days_since_launch("UNKNOWN", new_positioning) is None


def env_of_new(row, positioning=new_positioning):
    return make_env(row, baseline_env(product_code(row.get("campaignName")), default_base, all_base),
                    protected, None, product_code, positioning)


new_rows = [
    {"campaignId": "333", "campaignName": "ZZTEST-AUTO-261015", "adGroupId": "ag3",
     "searchTerm": "rain gear", "clicks": 30, "cost": 20.0, "sales7d": 0, "purchases7d": 0},
]
guard = (" and not (阶段 == 'new' and (新品天数 == None or "
         "新品天数 <= new_launch_protect_days))")
assert match_rows(new_rows, "", NEG + guard, env_of_new) == []  # 保护期内(第5天<=20)豁免, 不命中

old_positioning = {"ZZTEST": {**new_positioning["ZZTEST"],
                              "上架日期": (date.today() - timedelta(days=30)).isoformat()}}
hits_after_protect = match_rows(new_rows, "", NEG + guard, lambda row: env_of_new(row, old_positioning))
assert [r["searchTerm"] for r, _ in hits_after_protect] == ["rain gear"]  # 第30天>20, 保护期已过, 正常命中

mature_positioning = {"ZZTEST": {**new_positioning["ZZTEST"], "阶段": "mature"}}
hits_mature = match_rows(new_rows, "", NEG + guard, lambda row: env_of_new(row, mature_positioning))
assert [r["searchTerm"] for r, _ in hits_mature] == ["rain gear"]  # 阶段非new, 守卫不豁免, 正常命中

# evidence_gate: 新品冲量规则(跳过历史门: true) 必须绕过"历史证据缺失"/"历史表现未确认盈利"
# 两道门, 否则brand-new target(没有任何历史数据)永远无法命中这两条规则——这正是实际跑
# NW001 时发现的bug(新品冲量规则在 evaluate() 里match_rows阶段命中了, 却被 evidence_gate
# 当场吃掉, 最终输出里一条没有)。
new_launch_bid_rule = {"动作": "BID", "方向": "加价", "跳过历史门": True}
sparse_row = {"cost": 2.0, "clicks": 3, "sales7d": 0, "purchases7d": 0,
             "history_available": False, "history_sales": 0, "history_cost": 0}
sparse_env = {"target_acos": 0.3}
assert len(opt.evidence_gate([(sparse_row, sparse_env)], new_launch_bid_rule)[0]) == 1
mature_bid_rule = {"动作": "BID", "方向": "加价"}  # 没有跳过历史门, 同样的数据必须被挡住
assert len(opt.evidence_gate([(sparse_row, sparse_env)], mature_bid_rule)[0]) == 0
# 关键指标缺失这道门不受跳过历史门豁免——当前行数据本身不完整, 跟历史证据是两回事
incomplete_row = {**sparse_row, "cost": None}
assert len(opt.evidence_gate([(incomplete_row, sparse_env)], new_launch_bid_rule)[0]) == 0

print("收割动作:", [(a["词"], a["置信"]) for a in acts])
print("跳过:", [(s["词"], s["原因"]) for s in skip])
print("RULES_V2_OFFLINE_OK — 基线变量/保护词/置信分级/判重/无映射/预算/否定 全部通过")

import unittest
from test_upgrade import UpgradeTests
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(UpgradeTests))
if not result.wasSuccessful():
    raise SystemExit(1)
