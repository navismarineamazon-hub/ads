"""可配置证据检查、状态同步、收割竞价及输出保留。所有 API 操作仅查询。"""
from __future__ import annotations
import json
import math
import os
import re
import shutil
import statistics
import time
from datetime import datetime
from pathlib import Path

# 报告 placement 字段值 → API predicate
PLACEMENT_LABEL_MAP = {
    "top of search on-amazon": "PLACEMENT_TOP",
    "rest of search on-amazon": "PLACEMENT_REST_OF_SEARCH",
    "product pages on-amazon": "PLACEMENT_PRODUCT_PAGE",
    "detail page on-amazon": "PLACEMENT_PRODUCT_PAGE",
}

# 多站点：见 rules.py 里同名注释。
HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else Path(__file__).resolve().parent
METRICS = ('impressions', 'clicks', 'cost', 'sales7d', 'purchases7d')

def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def filtered_list(method, filters):
    try:
        return method(filters)
    except RuntimeError as exc:
        # 仅明确的过滤器400错误回退；权限、网络和不完整数据错误仍中止。
        if 'HTTP 400' not in str(exc):
            raise
        print('[快照] 过滤器不被接受，回退全量查询')
        return method()

def refresh_pool(client, cfg):
    ttl = float(cfg.get('同步', {}).get('结构缓存小时', 6)) * 3600
    files = [HERE/'data'/f'{name}_all.json' for name in ('campaigns','adgroups','targets','keywords_bid')]
    schema_ok = read_json(HERE/"data"/"pool_schema.json",{}).get("version")==4
    fresh = schema_ok and all(p.exists() and time.time()-p.stat().st_mtime < ttl for p in files)
    if fresh:
        print('[快照] 复用结构缓存')
        return
    cs = filtered_list(client.list_campaigns, {'stateFilter': {'include': ['ENABLED']}})
    cs = [c for c in cs if c.get('state') == 'ENABLED']
    auto = [str(c['campaignId']) for c in cs if c.get('targetingType') == 'AUTO']
    manual = [str(c['campaignId']) for c in cs if c.get('targetingType') != 'AUTO']
    ags = filtered_list(client.list_ad_groups, {'stateFilter': {'include': ['ENABLED']}})
    ags = [g for g in ags if g.get('state') == 'ENABLED' and str(g.get('campaignId')) in {str(c['campaignId']) for c in cs}]
    ts = []
    for i in range(0,len(auto),100):
        ts += filtered_list(client.list_targets, {'campaignIdFilter': {'include': auto[i:i+100]}, 'stateFilter': {'include': ['ENABLED']}})
    ts = [t for t in ts if str(t.get('campaignId')) in set(auto) and t.get('state') == 'ENABLED']
    ks = []
    for i in range(0,len(manual),100):
        ks += filtered_list(client.list_keywords, {'campaignIdFilter': {'include': manual[i:i+100]}, 'stateFilter': {'include': ['ENABLED']}})
    ks = [k for k in ks if str(k.get('campaignId')) in set(manual) and k.get('state') == 'ENABLED']
    groups = {str(g['adGroupId']):g for g in ags}
    for t in ts:
        if t.get('bid') is None:
            t['_effective_bid'] = groups.get(str(t.get('adGroupId')),{}).get('defaultBid')
            t['_bid_source'] = '广告组默认竞价'
    for k in ks:
        if k.get('bid') is None:
            k['_effective_bid'] = groups.get(str(k.get('adGroupId')),{}).get('defaultBid')
            k['_bid_source'] = '广告组默认竞价'
    for p, rows in zip(files, (cs,ags,ts,ks)):
        write_json(p,rows)
    write_json(HERE/'data'/'pool_schema.json',{'version':4})
    print(f'[快照] 启用活动 {len(cs)} / 广告组 {len(ags)} / AUTO目标 {len(ts)}')

def sync_keywords(client, cfg, force=False):
    path = HERE/'data'/'keywords_state.json'
    ttl = float(cfg.get('同步',{}).get('关键词缓存小时',24))*3600
    if not force and path.exists() and time.time()-path.stat().st_mtime < ttl:
        return read_json(path,[])
    rows = client.list_keywords()  # 全状态用于分辨暂停/归档；按可配置周期同步
    write_json(path,rows)
    return rows

def keyword_index(rows):
    result = {}
    for r in rows:
        if r.get('matchType') != 'BROAD':
            continue
        cid,ag,term = str(r.get('campaignId')),str(r.get('adGroupId')),str(r.get('keywordText','')).strip().lower()
        result.setdefault(cid,{}).setdefault(ag,{}).setdefault(term,[]).append(r)
    return result

def _agg_keys(kind):
    if kind == '搜索词':
        return ('campaignId', 'adGroupId', 'searchTerm')
    if kind == 'AUTO投放':
        return ('campaignId', 'adGroupId', 'keywordId')
    if kind == 'BROAD投放':
        return ('campaignId', 'adGroupId', 'keywordId')
    if kind == '位置表现':
        return ('campaignId', 'placement')
    return ('campaignId',)


def aggregate(rows, kind):
    grouped = {}
    keys = _agg_keys(kind)
    for row in rows:
        r = dict(row)
        if kind == '搜索词':
            r['searchTerm'] = str(r.get('searchTerm','')).strip().lower()
        key = tuple(str(r.get(k,'')) for k in keys)
        if key not in grouped:
            grouped[key] = r
            continue
        dst = grouped[key]
        for k in METRICS:
            # 缺字段不能当成零，传播缺失供证据层阻止修改。
            dst[k] = float(dst[k])+float(r[k]) if dst.get(k) is not None and r.get(k) is not None else None
    return list(grouped.values())


def prepare_reports(reports):
    for name in ('搜索词', 'AUTO投放', 'BROAD投放', '广告活动', '位置表现'):
        if name not in reports and name + '历史' not in reports:
            continue
        reports[name] = aggregate(reports.get(name, []), name)
        hist = aggregate(reports.get(name + '历史', []), name)
        keys = _agg_keys(name)
        index = {tuple(str(r.get(k, '')) for k in keys): r for r in hist}
        for row in reports[name]:
            h = index.get(tuple(str(row.get(k, '')) for k in keys))
            row['history_available'] = h is not None
            row['history_orders'] = h.get('purchases7d') if h else None
            row['history_cost'] = h.get('cost') if h else None
            row['history_sales'] = h.get('sales7d') if h else None
    return reports

def evidence_gate(hits, rule):
    # 新品冲量规则(config.yaml 跳过历史门: true)按设计就是在"没有历史证据证明盈利"的情况下
    # 主动加预算/加Bid冲曝光——跳过下面两道"要历史证据"的门, 否则brand-new target/campaign
    # 100%会被"历史证据缺失"挡住, 规则永远命中不了。关键指标缺失这道门不豁免: 那是当前行
    # 自己的数据完整性问题, 跟历史证据是两回事。
    skip_history = bool(rule.get('跳过历史门'))
    good, skip = [],[]
    for r,env in hits:
        why = ''
        if any(r.get(k) is None for k in ('cost','clicks','sales7d','purchases7d')):
            why = '关键指标缺失，保留观察'
        elif not skip_history and rule['动作'] in ('否定','BID','预算') and not r.get('history_available'):
            why = '历史证据缺失，保留观察'
        elif rule['动作']=='否定' and float(r.get('history_orders') or 0)>0:
            why = '历史窗口有单，先观察/调价，不直接否定'
        elif not skip_history and rule['动作']=='BID' and rule.get('方向')=='加价' and (float(r.get('history_sales') or 0)<=0 or float(r.get('history_cost') or 0)/float(r['history_sales'])>float(env['target_acos'])):
            why = '历史表现未确认盈利，暂不加价'
        if why:
            skip.append({'词':r.get('searchTerm') or r.get('targeting') or r.get('campaignId'),'来源':r.get('campaignName'),'原因':why})
        else:
            good.append((r,env))
    return good,skip

def harvest_bid(env, similar, recommended, default_bid):
    own = env.get('cpc7d')
    candidates=[]
    if own and own>0:
        candidates.append((float(own)*float(env.get('harvest_cpc_factor',0.85)),float(env.get('harvest_own_weight',0.6)),'自身CPC'))
    if similar:
        candidates.append((statistics.median(similar),float(env.get('harvest_similar_weight',0.25)),'相似BROAD词'))
    if recommended:
        candidates.append((statistics.median(recommended),float(env.get('harvest_recommendation_weight',0.15)),'Amazon推荐'))
    valid = [(v,w,n) for v,w,n in candidates if w>0]
    bid = sum(v*w for v,w,_ in valid)/sum(w for _,w,_ in valid) if valid else float(default_bid or env.get('harvest_fallback_bid',0.5))
    orders = float(env.get('purchases7d') or 0)
    clicks = float(env.get('clicks') or 0)
    prior = float(env.get('harvest_prior_clicks',20))
    cvr = (orders+float(env.get('expected_cvr',0.08))*prior)/max(clicks+prior,1)
    cap = float(env.get('target_cpa',15))*cvr
    if orders>0:
        cap = min(cap,float(env.get('sales7d') or 0)/orders*float(env.get('target_acos',0.3))*cvr)
    if cap < float(env.get('bid_min',0.2)):
        return None,'利润约束低于最低竞价，暂不收割'
    new = round(min(bid,cap,float(env.get('bid_max',20))),2)
    new = max(new,float(env.get('bid_min',0.2)))
    return new, '+'.join(n for _,_,n in valid)+f'; 平滑CVR={cvr:.3f}; 成本上限={cap:.2f}; 点击={clicks:g}; 订单={orders:g}'

def enrich_harvest(actions, hits, client, cfg, keywords):
    envs = {(str(r.get('campaignName')),str(r.get('searchTerm','')).lower()):env for r,env in hits}
    groups = {str(r['adGroupId']):r for r in read_json(HERE/'data'/'adgroups_all.json',[])}
    recommendations = {}
    if cfg.get('收割竞价',{}).get('获取Amazon推荐',True):
        terms_by_pair: dict[tuple, set] = {}
        for a in actions:
            terms_by_pair.setdefault((a['campaignId'], a['adGroupId']), set()).add(a['词'])
        for n,((cid,ag), term_set) in enumerate(terms_by_pair.items()):
            if n > 0:
                time.sleep(0.5)  # 避免背靠背触发429, 本身就比指数退避重试快
            terms = sorted(term_set)
            for i in range(0,len(terms),100):
                try:
                    rec = client.recommend_bids(cid,ag,[{'type':'KEYWORD_BROAD_MATCH','value':t} for t in terms[i:i+100]])
                    for (_,term),values in rec.items():
                        recommendations[(str(cid),str(ag),term)] = values
                except Exception as exc:
                    print(f'[推荐竞价] {cid}/{ag}: {exc}；使用自身表现/相似词参考')
    # (关键词词集, bid) 按活动分组预先 tokenize 一次——下面按 action 算相似度时不用
    # 对同一活动下的同一批关键词重复 split()，没有 bid 的关键词本来也进不了 similar，
    # 分组时就滤掉。
    by_campaign: dict[str, list[tuple[set, float]]] = {}
    for k in keywords:
        if k.get('matchType')!='BROAD' or k.get('state')!='ENABLED' or not k.get('bid'):
            continue
        by_campaign.setdefault(str(k.get('campaignId')), []).append(
            (set(str(k.get('keywordText','')).lower().split()), float(k['bid'])))

    result,skip = [],[]
    for a in actions:
        env = envs[(str(a['来源']),a['词'])]
        words = set(a['词'].split())
        min_score = float(env.get('harvest_similarity_min',0.5))
        similar = [bid for kw, bid in by_campaign.get(str(a['campaignId']), [])
                   if len(words & kw)/max(len(words | kw),1) >= min_score]
        rec = recommendations.get((str(a['campaignId']),str(a['adGroupId']),a['词']),[])
        bid,reason=harvest_bid(env,similar,rec,groups.get(str(a['adGroupId']),{}).get('defaultBid'))
        if bid is None:
            skip.append({'词':a['词'],'来源':a['来源'],'原因':reason})
            continue
        a.update({'新Bid':bid,'竞价依据':reason,'推荐区间':str(rec),'批准':'否' if float(env.get('purchases7d') or 0)<float(env.get('min_winner_orders',2)) else '是'})
        result.append(a)
    return result,skip

def enrich_pt_harvest(actions, envs, client, cfg):
    """给竞品ASIN/类目定位收割算 Bid。跟 enrich_harvest 同一套利润上限逻辑 (harvest_bid)，
    但没有"相似BROAD词"这个参考维度 (ASIN/类目定向跟关键词竞价不是一回事)，只用自身CPC
    + Amazon推荐竞价(按 expression type/value 查) + 兜底默认竞价。
    envs: {(campaignName, 定向值.lower()): env} , 由调用方 (rules.expand_pt_harvest) built好传入。"""
    groups = {str(r['adGroupId']):r for r in read_json(HERE/'data'/'adgroups_all.json',[])}
    recommendations = {}
    if cfg.get('收割竞价',{}).get('获取Amazon推荐',True):
        actions_by_pair: dict[tuple, list] = {}
        for a in actions:
            actions_by_pair.setdefault((a['campaignId'], a['adGroupId']), []).append(a)
        for n,((cid,ag), items) in enumerate(actions_by_pair.items()):
            if n > 0:
                time.sleep(0.5)
            exprs = [{'type':a['匹配'],'value':a['词']} for a in items]
            try:
                rec = client.recommend_bids(cid,ag,exprs)
                for (etype,val),values in rec.items():
                    recommendations[(str(cid),str(ag),etype,val)] = values
            except Exception as exc:
                print(f'[推荐竞价][定位收割] {cid}/{ag}: {exc}；使用自身表现参考')

    result,skip = [],[]
    for a in actions:
        env = envs.get((str(a['来源']),a['词'].lower()), {})
        rec = recommendations.get((str(a['campaignId']),str(a['adGroupId']),a['匹配'],a['词'].lower()),[])
        bid,reason=harvest_bid(env,[],rec,groups.get(str(a['adGroupId']),{}).get('defaultBid'))
        if bid is None:
            skip.append({'词':a['词'],'来源':a['来源'],'原因':reason})
            continue
        a.update({'新Bid':bid,'竞价依据':reason,'推荐区间':str(rec),'批准':'否' if float(env.get('purchases7d') or 0)<float(env.get('min_winner_orders',2)) else '是'})
        result.append(a)
    return result,skip

def cooldown(action, days, success=None):
    """success 不给就读一次 execution_ledger.json——调用方已经手头有加载过的 ledger
    (如 execute.py 的 already / screen() 的 ledger 整体) 时传进来复用, 省掉逐行重读磁盘。"""
    if success is None:
        success = read_json(HERE/'data'/'execution_ledger.json',{'success':{}}).get('success',{})
    for entry in success.values():
        row=entry.get('row',{})
        if action.get('动作')!=row.get('动作') or str(action.get('campaignId'))!=str(row.get('campaignId')):
            continue
        if action.get('动作')=='BID' and str(action.get('targetId'))!=str(row.get('targetId')):
            continue
        try:
            if (datetime.now()-datetime.fromisoformat(entry['time'])).total_seconds()<days*86400:
                return True
        except (KeyError,ValueError):
            pass
    return False

def ledger_index(success: dict) -> dict:
    """execution_ledger.json['success'] -> {(动作,层级,campaignId,targetId): [(time, row), ...]}。

    供 history_feedback 查某个关键词/target 上一次同类调整的决策快照，不预设调用频率，
    每轮 evaluate() 建一次索引即可复用给全部候选行。"""
    idx: dict = {}
    for entry in success.values():
        row = entry.get('row', {})
        try:
            ts = datetime.fromisoformat(entry['time'])
        except (KeyError, ValueError, TypeError):
            continue
        key = (row.get('动作'), row.get('层级'), str(row.get('campaignId')), str(row.get('targetId')))
        idx.setdefault(key, []).append((ts, row))
    return idx


def history_feedback(idx: dict, action_type: str, level: str, campaign_id, target_id, direction: str,
                     current_acos, target_acos, lookback_days: float = 10.0,
                     boost: float = 1.25, damp: float = 0.5) -> tuple:
    """看同一对象上一次同方向调整后 ACOS 相对目标的误差是缩小还是扩大，返回 (倍数, 理由)。

    找不到匹配历史/方向不同/缺指标 -> 中性 (1.0, '')，不影响没有历史数据时的行为。"""
    entries = idx.get((action_type, level, str(campaign_id), str(target_id)))
    if not entries or current_acos is None or not target_acos:
        return 1.0, ''
    ts, row = max(entries, key=lambda x: x[0])
    age_days = (datetime.now() - ts).total_seconds() / 86400
    if age_days < 0 or age_days > lookback_days:
        return 1.0, ''
    try:
        prev_ratio = float(row.get('调整比例') or 0)
        prev_acos = float(row.get('决策ACOS'))
    except (TypeError, ValueError):
        return 1.0, ''
    prev_direction = '加价' if prev_ratio > 0 else ('减价' if prev_ratio < 0 else None)
    if prev_direction is None or prev_direction != direction:
        return 1.0, ''
    err_before = abs(prev_acos / float(target_acos) - 1)
    err_now = abs(float(current_acos) / float(target_acos) - 1)
    if err_now < err_before * 0.97:
        return boost, f'历史反馈:上次{prev_direction}后ACOS {prev_acos:.1%}→{float(current_acos):.1%} 好转,放大本次调整'
    if err_now > err_before * 1.03:
        return damp, f'历史反馈:上次{prev_direction}后ACOS {prev_acos:.1%}→{float(current_acos):.1%} 恶化,收缩本次调整'
    return 1.0, '历史反馈:上次调整后ACOS基本未变'


def compute_joint_bids(base_bid: float, placement_rows: list, current_pcts: dict,
                       target_acos: float, smoothing: float = 0.4,
                       pct_min: int = 0, pct_max: int = 300,
                       min_clicks: int = 15) -> dict:
    """联合优化：给定活动位置报告数据，返回 base bid 调整幅度和各位置倍率。

    算法：目标有效竞价 = 当前有效竞价 × (target_acos / placement_acos)。
    最优 base_bid = 点击量加权的目标有效竞价的几何均值（最小化 Σ w_p·log²(mult_p)）。
    各位置倍率 = 目标有效竞价 / 新 base_bid，再平滑。

    Returns:
        {
          'base_bid_delta_pct': float,  # base bid 建议调整幅度 (%, 正=加价)
          'pcts': {predicate: new_pct}, # 各位置新倍率
          'reasoning': str,             # 推理说明
        }
    or {} if data is insufficient.
    """
    pdata: dict = {}
    for row in placement_rows:
        pl = str(row.get("placement") or "").lower()
        pred = PLACEMENT_LABEL_MAP.get(pl)
        if not pred:
            continue
        clicks = float(row.get("clicks") or 0)
        cost = float(row.get("cost") or 0)
        sales = float(row.get("sales7d") or 0)
        if clicks < min_clicks or sales <= 0:
            continue
        pdata[pred] = {"acos": cost / sales, "clicks": clicks}

    if len(pdata) < 2 or base_bid <= 0:
        return {}

    # Compute target effective bid per placement
    targets: dict = {}
    for pred, d in pdata.items():
        current_eff = base_bid * (1 + current_pcts.get(pred, 0) / 100)
        targets[pred] = {"target_eff": current_eff * (target_acos / d["acos"]),
                         "clicks": d["clicks"], "acos": d["acos"]}

    # Optimal base bid: click-weighted geometric mean of target effective bids
    total_clicks = sum(d["clicks"] for d in targets.values())
    if total_clicks == 0:
        return {}
    try:
        log_w_avg = sum(d["clicks"] * math.log(max(d["target_eff"], 0.01))
                        for d in targets.values()) / total_clicks
        optimal_base = math.exp(log_w_avg)
    except (ValueError, ZeroDivisionError):
        return {}

    new_base = base_bid * (1 - smoothing) + optimal_base * smoothing
    base_delta_pct = round((new_base / base_bid - 1) * 100, 1)

    SHORT = {"PLACEMENT_TOP": "TOS", "PLACEMENT_REST_OF_SEARCH": "ROS",
             "PLACEMENT_PRODUCT_PAGE": "PP"}
    new_pcts: dict = {}
    parts: list = []
    for pred, d in targets.items():
        desired_pct = max(pct_min, min(pct_max, round((d["target_eff"] / new_base - 1) * 100)))
        cur = current_pcts.get(pred, 0)
        new_p = max(pct_min, min(pct_max, round(cur * (1 - smoothing) + desired_pct * smoothing)))
        new_pcts[pred] = new_p
        parts.append(f"{SHORT.get(pred, pred)}: ACOS={d['acos']:.0%} → 倍率{cur}%→{new_p}%")

    reason = (f"联合优化(平滑{smoothing}): base_bid建议{'加' if base_delta_pct>0 else '减'}"
              f"{abs(base_delta_pct):.1f}%; " + "; ".join(parts))
    return {"base_bid_delta_pct": base_delta_pct, "pcts": new_pcts, "reasoning": reason}


_CONF_BASE = {"high": 85, "medium": 65, "low": 40, "needs_human": 20}

def conf_numeric(label: str, env: dict, action_type: str = "") -> tuple:
    """置信分 (0-100) + 理由字符串。"""
    base = _CONF_BASE.get(str(label).lower(), 65)
    reasons: list = []
    delta = 0
    orders = float(env.get("purchases7d") or 0)
    cost = float(env.get("cost") or 0)
    hist_avail = bool(env.get("history_available"))
    hist_orders = float(env.get("history_orders") or 0)

    if action_type == "否定":
        if not hist_avail:
            delta -= 15; reasons.append("历史数据不足")
        elif hist_orders > 0:
            delta -= 30; reasons.append(f"历史有单({hist_orders:.0f})")
        else:
            delta += 10; reasons.append("历史确认无单")
        threshold = float(env.get("no_order_spend_threshold", 30))
        if cost >= threshold * 1.5:
            delta += 5; reasons.append(f"花费超阈{cost:.0f}")
    elif action_type == "收割":
        min_orders = float(env.get("min_winner_orders", 2))
        if not hist_avail:
            delta -= 10; reasons.append("历史数据不足")
        if orders >= min_orders * 2:
            delta += 8; reasons.append(f"高订单({orders:.0f})")
        elif orders < min_orders:
            delta -= 15; reasons.append(f"订单偏少({orders:.0f})")
        acos = env.get("acos7d")
        if acos is not None:
            target = float(env.get("target_acos", 0.3))
            if acos < target * 0.8:
                delta += 5; reasons.append(f"ACOS优({acos:.0%})")
            elif acos > target * 1.3:
                delta -= 10; reasons.append(f"ACOS偏高({acos:.0%})")
    elif action_type in ("BID", "预算"):
        if not hist_avail:
            delta -= 10; reasons.append("历史数据不足")
        else:
            reasons.append("历史确认")

    score = min(max(base + delta, 0), 100)
    return score, ("; ".join(reasons) if reasons else str(label))


def prune_outputs(out_dir, current, keep=2):
    """仅清理有总览/完成标记的日期轮次目录；新轮完成才调用。"""
    if isinstance(keep,bool) or not isinstance(keep,int) or keep<2:
        raise ValueError('保留轮数必须是至少2的整数')
    runs=[p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink()
          and re.fullmatch(r'\d{4}-\d{2}-\d{2} & \d+',p.name)
          and ((p/'总览.md').exists() or (p/'run_complete.json').exists())]
    runs.sort(key=lambda p: (p.name[:10],int(p.name.split(" & ")[1])),reverse=True)
    retained={current.resolve()}
    for p in runs:
        if len(retained)<keep:
            retained.add(p.resolve())
    removed=[]
    for p in runs:
        if p.resolve() not in retained:
            shutil.rmtree(p)
            removed.append(p.name)
    return removed
