# -*- coding: utf-8 -*-
"""执行建议表: 把 suggest 出的板块 CSV 落到亚马逊广告账户。

用法 (都走 run.py, 它是唯一入口):
    python run.py execute "2026-10-08 & 2"                # 干跑整个运行目录, 不改任何数据
    python run.py execute 01-收割.csv                      # 干跑单个板块
    python run.py execute --go 01-收割.csv                 # 正式执行单个板块
    python run.py execute --go --only 收割 "2026-10-08 & 2" # 整批里只跑一个板块
    python run.py execute --go "2026-10-08 & 2"            # 正式执行目录内全部未执行板块
    python run.py execute --go --campaign 123456 "2026-10-08 & 2"
        # 不分板块, 把这个 campaignId 在 01~06 里已批准的行一起执行
        # (比如同一活动联合算出来的 05 BID调整 + 06 位置调整, 一条命令一起落账户)

安全机制:
  - 默认干跑, 不带 --go 不会发任何写请求。
  - 正式执行先发首条试探, 失败则整批中止 (exit 2)。
  - 每种动作单批上限 (config 安全.单批上限)。
  - 批准=否 的行跳过; 一个板块文件里批准的行全部执行成功后改名 .done 防重复执行——
    用 --campaign 只执行了文件里一部分活动时, 该文件暂不改名, 等其余活动也执行完。
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import yaml

from ads_api import AdsClient
from optimization import (sync_keywords, keyword_index, read_json, write_json, cooldown, filtered_list)
from rules import EXCEL_TEXT_GUARD_FIELDS


# 多站点：见 rules.py 里同名注释。
HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else Path(__file__).resolve().parent
ORDER = ["01-收割", "02-加预算", "03-减预算", "04-否定", "05-BID调整", "06-位置调整"]


_RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} & \d+$")


def _latest_run_dir() -> Path | None:
    """output/ 下最新的一个 "yyyy-mm-dd & N" 运行目录(按日期、再按N比大小)，
    免得每次都要去 output/ 里翻日期——execute 的 target 传 latest/最新 就行。"""
    out_dir = HERE / "output"
    if not out_dir.is_dir():
        return None
    dirs = [p for p in out_dir.iterdir() if p.is_dir() and _RUN_DIR_RE.match(p.name)]
    if not dirs:
        return None
    return max(dirs, key=lambda p: (p.name.split(" & ")[0], int(p.name.split(" & ")[1])))


def resolve_target(arg: str) -> tuple[list[Path], str]:
    """返回 (板块CSV列表, 描述)。支持: 板块csv / 运行目录 / output下的目录名 /
    latest(最新一次运行，不用每次手动抄日期)。"""
    if arg in ("latest", "最新"):
        latest = _latest_run_dir()
        if latest is None:
            print("output/ 下没有任何 \"yyyy-mm-dd & N\" 运行目录，先跑一次 suggest。")
            sys.exit(1)
        print(f"[latest] 解析为最新运行目录: {latest.name}")
        arg = latest.name
    cands = [Path(arg), HERE / arg, HERE / "output" / arg]
    target = next((c for c in cands if c.exists()), None)
    if not target:
        print(f"找不到目标: {arg} (试过 {[str(c) for c in cands]})")
        sys.exit(1)
    if target.is_dir():
        files = sorted(p for p in target.glob("*.csv") if not p.name.endswith(".done"))
        files.sort(key=lambda p: next((i for i, o in enumerate(ORDER) if p.name.startswith(o)), 99))
        return files, f"目录 {target.name}"
    return [target], target.name


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        rows = [dict(r) for r in csv.DictReader(f)]
    for r in rows:
        for k in EXCEL_TEXT_GUARD_FIELDS:
            v = r.get(k)
            if v and v.startswith("'"):
                r[k] = v[1:]
    approved = [r for r in rows if (r.get("批准") or "").strip() in ("是", "y", "Y", "yes", "1")]
    print(f"  读取 {path.name}: 共 {len(rows)} 行, 批准 {len(approved)}, 跳过 {len(rows) - len(approved)}")
    return approved


def load_ledger() -> dict:
    p = HERE / "data" / "execution_ledger.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"success": {}}


def save_ledger(ledger: dict, cfg: dict | None = None, conn=None) -> None:
    p = HERE / "data" / "execution_ledger.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    _dual_write_ledger(ledger, cfg, conn)


def _dual_write_ledger(ledger: dict, cfg: dict | None, conn=None) -> None:
    """Phase 3 数据管道：JSON 落盘的同一时刻把 ledger 状态镜像进 SQLite。
    cfg 为 None 或任何异常都只打印警告、不中断 —— 执行账本的安全语义永远以上面的 JSON 为准。

    conn 由调用方传入 (execute_file 一次执行里会连续调用好几次 save_ledger——每个批次
    试探一次、正式批一次) 时直接复用、关闭由调用方负责；不传就按原来的样子自己开关一次。"""
    if cfg is None:
        return
    owns_conn = conn is None
    try:
        import pipeline
        if owns_conn:
            conn = pipeline.open_conn(cfg, HERE)
        if conn is not None:
            pipeline.sync_ledger(conn, str(cfg["profile_id"]), ledger)
    except Exception as exc:
        print(f"[管道][警告] 执行账本镜像到 SQLite 失败，不影响本次执行: {exc}")
    finally:
        if owns_conn and conn is not None:
            conn.close()


def execute_file(client: AdsClient, path: Path, go: bool, cap: int,
                 campaign: str | None = None) -> bool:
    """执行单个板块 CSV。返回 True=全部成功 (可标 .done)。

    campaign 给了就只执行该 campaignId 的行, 其余行(其它活动, 批准=是但没选中)留在
    文件里——跟 批准 列一样是"只挑这一批"的过滤器, 两者取交集。这种情况下, 即使
    挑中的行全部成功, 文件也不会被标 .done (还有别的活动的已批准行没处理), 等
    那些活动也执行完、或者这一轮没有别的活动等着了, 才会真正标 .done。"""
    rows = load_rows(path)
    ledger = load_ledger()
    already = ledger.setdefault("success", {})
    before = len(rows)
    rows = [r for r in rows if not r.get("动作ID") or r.get("动作ID") not in already]
    if before != len(rows):
        print(f"  已确认成功动作跳过 {before - len(rows)} 条（执行账本）")
    leftover_other_campaign = 0
    if campaign:
        before_c = len(rows)
        rows = [r for r in rows if str(r.get("campaignId") or "") == str(campaign)]
        leftover_other_campaign = before_c - len(rows)
        print(f"  --campaign {campaign}: {before_c} 条里挑中 {len(rows)} 条"
              + (f" (另有 {leftover_other_campaign} 条属于其它活动, 本次不动)"
                 if leftover_other_campaign else ""))
    negs = [r for r in rows if r.get("动作") == "否定"]
    negasins = [r for r in rows if r.get("动作") == "否定ASIN"]
    kws = [r for r in rows if r.get("动作") == "收割"]
    pts = [r for r in rows if r.get("动作") == "收割ASIN"]
    buds = [r for r in rows if r.get("动作") == "预算"]
    bids = [r for r in rows if r.get("动作") == "BID" and r.get("层级") == "AUTO_TARGET"]
    kwbids = [r for r in rows if r.get("动作") == "BID" and r.get("层级") == "KEYWORD"]
    pbids = [r for r in rows if r.get("动作") == "位置BID"]
    unknown = [r for r in rows if r.get("动作") not in ("否定", "否定ASIN", "收割", "收割ASIN", "预算", "BID", "位置BID")
               or (r.get("动作") == "BID" and r.get("层级") not in ("AUTO_TARGET", "KEYWORD"))]
    for r in unknown:
        print(f"  !! 未知动作, 跳过: {r}")
    print(f"  计划: 否定 {len(negs)} | 否定ASIN {len(negasins)} | 收割 {len(kws)} | 收割ASIN {len(pts)} | "
          f"预算 {len(buds)} | BID {len(bids)} | 关键词BID {len(kwbids)} | 位置BID {len(pbids)}")
    if any(len(x) > cap for x in (negs, negasins, kws, pts, buds, bids, kwbids, pbids)):
        print("  !! 超过单批上限, 拒绝执行该板块。")
        return False
    if not (negs or negasins or kws or pts or buds or bids or kwbids or pbids):
        print("  没有可执行的动作。")
        return True

    def preview(items, n=5):
        for r in items[:n]:
            if r["动作"] == "预算":
                print(f"    活动 {r['campaignId']} {str(r.get('来源',''))[:28]}: {r.get('原预算')} → {r.get('新预算')}")
            else:
                print(f"    [{r['匹配']}] {str(r['词'])[:42]}  ← {str(r.get('来源',''))[:28]} (ag={r.get('adGroupId')})")

    if negs:
        print("  --- 否定词 ---"); preview(negs)
    if negasins:
        print("  --- 否定ASIN/类目定位 ---"); preview(negasins)
    if kws:
        print("  --- 收割词 ---"); preview(kws)
    if pts:
        print("  --- 收割ASIN/类目定位 ---"); preview(pts)
    if buds:
        print("  --- 预算调整 ---"); preview(buds)
    if bids:
        print("  --- AUTO target Bid ---")
        for r in bids[:5]:
            print(f"    target {r.get('targetId')}: {r.get('原Bid')} → {r.get('新Bid')} ({r.get('调整比例')})")
    if kwbids:
        print("  --- BROAD keyword Bid ---")
        for r in kwbids[:5]:
            print(f"    keyword {r.get('targetId')} [{str(r.get('投放组',''))[:28]}]: {r.get('原Bid')} → {r.get('新Bid')} ({r.get('调整比例')})")
    if pbids:
        print("  --- 位置竞价调整 ---")
        for r in pbids[:5]:
            print(f"    活动 {str(r.get('来源',''))[:32]}  {r.get('投放位置','?')}: {r.get('原倍率')}% → {r.get('新倍率')}%")

    if not go:
        print("  [干跑] 未对账户做任何修改。")
        return True

    # 写入前刷新涉及对象，检查父层状态、重复关键词/否词、预算和竞价变化。
    cfg = yaml.safe_load((HERE/'config.yaml').read_text(encoding='utf-8'))
    from rules import baseline_env, load_baseline
    defaults,all_base = load_baseline(HERE/cfg['基线文件'])
    try:
        involved=sorted({str(r['campaignId']) for r in kws+pts+negs+negasins+buds+bids+kwbids})
        filter_body={'campaignIdFilter':{'include':involved}}
        campaigns = {str(x['campaignId']):x for x in filtered_list(client.list_campaigns,filter_body)}
        groups = {str(x['adGroupId']):x for x in filtered_list(client.list_ad_groups,filter_body)}
        keywords = filtered_list(client.list_keywords,filter_body) if (kws or kwbids) else []
        negative_rows = filtered_list(client.list_negative_keywords,filter_body) if negs else []
        pt_targets = filtered_list(client.list_targets,filter_body) if pts else []
        negative_asin_rows = filtered_list(client.list_negative_targets,filter_body) if negasins else []
    except Exception as exc:
        print(f'执行前核查失败，未提交: {exc}')
        return False
    for r in kws+pts+negs+negasins+buds+bids+kwbids:
        c=campaigns.get(str(r.get('campaignId')),{})
        g=groups.get(str(r.get('adGroupId')), {})
        if c.get('state')!='ENABLED' or (r.get('adGroupId') and (g.get('state')!='ENABLED' or str(g.get('campaignId'))!=str(r.get('campaignId')))):
            print('父活动/广告组未启用或不匹配，停止该板块')
            return False
        base=baseline_env(r.get('产品码',''),defaults,all_base)
        if r['动作'] in ('BID','收割','收割ASIN'):
            try:
                value=float(r['新Bid'])
                if not base['bid_min']<=value<=base['bid_max']:
                    raise ValueError('超出上下限')
            except (KeyError,ValueError,TypeError):
                print('竞价缺失或超出产品限幅，停止该板块')
                return False
        if r['动作'] in ('BID','预算') and cooldown(r,float(base.get('bid_cooldown_days',3)),already):
            print('冷却期内，停止该板块')
            return False
        if r['动作']=='预算' and abs(float(c.get('budget',{}).get('budget',-1))-float(r['原预算']))>0.0001:
            print('活动预算已变化，请重新生成建议')
            return False
        if r['动作']=='收割':
            duplicate=[k for k in keywords if str(k.get('campaignId'))==str(r['campaignId']) and k.get('matchType')=='BROAD' and str(k.get('keywordText','')).strip().lower()==r['词'].strip().lower()]
            if duplicate:
                print('目标活动已存在关键词，状态='+str([k.get('state') for k in duplicate])+'；重新生成建议')
                return False
        if r['动作']=='收割ASIN':
            duplicate=[t for t in pt_targets if str(t.get('campaignId'))==str(r['campaignId'])
                       and str(t.get('adGroupId'))==str(r.get('adGroupId'))
                       and any(str(e.get('type'))==r.get('匹配') and str(e.get('value','')).strip().lower()==r['词'].strip().lower()
                               for e in (t.get('expression') or []))]
            if duplicate:
                print('目标广告组已存在该定向，状态='+str([t.get('state') for t in duplicate])+'；重新生成建议')
                return False
        if r['动作']=='否定' and any(str(k.get('campaignId'))==str(r['campaignId']) and str(k.get('adGroupId') or '') in ('',str(r.get('adGroupId') or '')) and k.get('state')=='ENABLED' and k.get('matchType')==(r.get('匹配') or 'NEGATIVE_EXACT') and str(k.get('keywordText','')).strip().lower()==r['词'].strip().lower() for k in negative_rows):
            print('已存在启用的否定词，请重新生成建议')
            return False
        if r['动作']=='否定ASIN':
            duplicate=[t for t in negative_asin_rows if str(t.get('campaignId'))==str(r['campaignId'])
                       and str(t.get('adGroupId'))==str(r.get('adGroupId'))
                       and any(str(e.get('type'))==r.get('匹配') and str(e.get('value','')).strip().lower()==r['词'].strip().lower()
                               for e in (t.get('expression') or []))]
            if duplicate:
                print('已存在该负向定位，状态='+str([t.get('state') for t in duplicate])+'；重新生成建议')
                return False
    failures: list[str] = []
    ok_count = 0

    if bids:
        try:
            latest = {str(x.get("targetId")): x for x in filtered_list(client.list_targets,filter_body)}
        except Exception as e:
            failures.append(f"BID 当前值核查失败: {e}")
            latest = {}
        checked = []
        for r in bids:
            cur = latest.get(str(r.get("targetId")))
            try:
                if not cur or str(cur.get("state", "")).upper() != "ENABLED":
                    failures.append(f"BID [{r.get('targetId')}] 当前对象缺失或未启用，跳过")
                    continue
                effective = cur.get("bid") if cur.get("bid") is not None else groups.get(str(cur.get("adGroupId")),{}).get("defaultBid")
                if abs(float(effective) - float(r.get("原Bid"))) > 0.0001:
                    failures.append(f"BID [{r.get('targetId')}] 当前值已变化 ({cur.get('bid')} != {r.get('原Bid')})，跳过")
                    continue
            except (TypeError, ValueError):
                failures.append(f"BID [{r.get('targetId')}] 当前Bid无效，跳过")
                continue
            checked.append(r)
        bids = checked
        if not bids:
            print("  BID 没有通过执行前当前值核查的条目。")

    if kwbids:
        latest_kw = {str(k.get("keywordId")): k for k in keywords}
        checked = []
        for r in kwbids:
            cur = latest_kw.get(str(r.get("targetId")))
            try:
                if not cur or str(cur.get("state", "")).upper() != "ENABLED" or str(cur.get("matchType", "")).upper() != "BROAD":
                    failures.append(f"关键词BID [{r.get('targetId')}] 当前对象缺失/未启用/非BROAD，跳过")
                    continue
                effective = cur.get("bid") if cur.get("bid") is not None else groups.get(str(cur.get("adGroupId")),{}).get("defaultBid")
                if abs(float(effective) - float(r.get("原Bid"))) > 0.0001:
                    failures.append(f"关键词BID [{r.get('targetId')}] 当前值已变化 ({cur.get('bid')} != {r.get('原Bid')})，跳过")
                    continue
            except (TypeError, ValueError):
                failures.append(f"关键词BID [{r.get('targetId')}] 当前Bid无效，跳过")
                continue
            checked.append(r)
        kwbids = checked
        if not kwbids:
            print("  关键词BID 没有通过执行前当前值核查的条目。")

    if pbids:
        checked_p = []
        for r in pbids:
            cid = str(r.get("campaignId", ""))
            c = campaigns.get(cid, {})
            pred = r.get("投放位置", "")
            cur_pct = 0
            for adj in (c.get("bidding") or {}).get("adjustments") or []:
                if adj.get("predicate") == pred:
                    cur_pct = int(adj.get("percentage") or 0)
                    break
            try:
                expected = int(r.get("原倍率") or 0)
            except (TypeError, ValueError):
                failures.append(f"位置BID [{cid}:{pred}] 原倍率字段无效，跳过")
                continue
            if abs(cur_pct - expected) > 1:
                failures.append(f"位置BID [{cid}:{pred}] 当前倍率已变化 ({cur_pct}% ≠ {expected}%)，请重新生成建议")
                continue
            checked_p.append(r)
        pbids = checked_p
        if not pbids:
            print("  位置BID 没有通过执行前核查的条目。")

    # 下面 solo_probe/run_batch 每跑一批就调一次 save_ledger (试探一次、正式批一次)，
    # 这一个 SQLite 连接整个板块复用，不逐次开关；跑完在 finally 里关一次。
    import pipeline
    ledger_conn = pipeline.open_conn(cfg, HERE)

    def call(kind: str, part: list[dict]):
        for r in part:
            if r.get('动作ID') in ledger.setdefault('pending',{}):
                raise RuntimeError('该动作上次提交结果不确定；请先在广告后台核实，再处理 data/execution_ledger.json 的 pending 条目')
            ledger['pending'][r['动作ID']]={'row':r,'time':time.strftime('%Y-%m-%dT%H:%M:%S')}
        save_ledger(ledger, cfg, ledger_conn)
        if kind == "收割":
            return client.create_broad_keywords([{"adGroupId": r["adGroupId"], "campaignId": r["campaignId"],
                "keywordText": r["词"], "matchType": "BROAD", "bid":float(r["新Bid"]), "state": "ENABLED"} for r in part])
        if kind == "收割ASIN":
            return client.create_targets([{"campaignId": r["campaignId"], "adGroupId": r["adGroupId"],
                "expression": [{"type": r["匹配"], "value": r["词"]}], "expressionType": "MANUAL",
                "bid": float(r["新Bid"]), "state": "ENABLED"} for r in part])
        if kind == "否定":
            return client.create_negative_exact([{"campaignId": r["campaignId"], "adGroupId": r.get("adGroupId") or None,
                "keywordText": r["词"], "matchType": r.get("匹配") or "NEGATIVE_EXACT", "state": "ENABLED"} for r in part])
        if kind == "否定ASIN":
            return client.create_negative_targets([{"campaignId": r["campaignId"], "adGroupId": r["adGroupId"],
                "expression": [{"type": r["匹配"], "value": r["词"]}], "state": "ENABLED"} for r in part])
        if kind == "预算":
            return client.update_campaigns([{"campaignId": r["campaignId"], "name": r.get("来源"),
                "budget": {"budgetType": "DAILY", "budget": float(r["新预算"])} } for r in part])
        if kind == "位置BID":
            # Group by campaignId: multiple placements for the same campaign → one API call
            by_cid: dict = {}
            for r in part:
                cid = str(r["campaignId"])
                if cid not in by_cid:
                    c = campaigns.get(cid, {})
                    by_cid[cid] = {
                        "campaignId": cid,
                        "bidding": {
                            "strategy": (c.get("bidding") or {}).get("strategy", "LEGACY_FOR_SALES"),
                            "adjustments": [dict(a) for a in (c.get("bidding") or {}).get("adjustments") or []],
                        },
                        "_rows": [],
                    }
                pred = r["投放位置"]
                adjs = by_cid[cid]["bidding"]["adjustments"]
                for adj in adjs:
                    if adj["predicate"] == pred:
                        adj["percentage"] = int(r["新倍率"])
                        break
                else:
                    adjs.append({"predicate": pred, "percentage": int(r["新倍率"])})
                by_cid[cid]["_rows"].append(r)
            cid_list = list(by_cid.keys())
            items = [{"campaignId": v["campaignId"], "bidding": v["bidding"]} for v in by_cid.values()]
            ok_raw, err_raw = client.update_campaigns(items)
            # Remap campaign-level indices to row-level indices in `part`
            ok_out, err_out = [], []
            for entry in ok_raw:
                idx = entry.get("index")
                if isinstance(idx, int) and 0 <= idx < len(cid_list):
                    for i, r in enumerate(part):
                        if str(r["campaignId"]) == cid_list[idx]:
                            ok_out.append({**entry, "index": i})
            for entry in err_raw:
                idx = entry.get("index") if isinstance(entry, dict) else None
                if isinstance(idx, int) and 0 <= idx < len(cid_list):
                    for i, r in enumerate(part):
                        if str(r["campaignId"]) == cid_list[idx]:
                            err_out.append({**(entry if isinstance(entry, dict) else {"detail": str(entry)}), "index": i})
            return ok_out, err_out
        if kind == "关键词BID":
            return client.update_keywords([{"keywordId": r["targetId"], "bid": float(r["新Bid"])} for r in part])
        return client.update_targets([{"targetId": r["targetId"], "bid": float(r["新Bid"])} for r in part])

    def apply_result(kind: str, part: list[dict], ok: list, err: list) -> None:
        nonlocal ok_count
        ok_indices = {x.get("index") for x in ok if isinstance(x, dict) and isinstance(x.get("index"), int)}
        ok_count += len(ok_indices)
        for idx in ok_indices:
            if 0 <= idx < len(part) and part[idx].get("动作ID"):
                already[part[idx]["动作ID"]] = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                 "动作": kind, "file": path.name, "row":part[idx]}
                ledger.setdefault("pending",{}).pop(part[idx]["动作ID"],None)
        if kind=='收割':
            cache=read_json(HERE/'data'/'keywords_state.json',[])
            for entry in ok:
                idx=entry.get('index')
                if isinstance(idx,int) and 0<=idx<len(part):
                    r=part[idx]
                    cache.append({'keywordId':entry.get('keywordId'), 'campaignId':r['campaignId'],
                                  'adGroupId':r['adGroupId'], 'keywordText':r['词'],
                                  'matchType':'BROAD','state':'ENABLED','bid':float(r['新Bid'])})
            write_json(HERE/'data'/'keywords_state.json',cache)
        # 明确API失败可重试；不确定结果保留pending，禁止盲目重试。
        for e in err:
            idx=e.get('index') if isinstance(e,dict) else None
            if isinstance(idx,int) and 0<=idx<len(part) and e.get('code') not in ('UNCONFIRMED','INVALID_JSON','UNKNOWN_RESPONSE'):
                ledger.setdefault('pending',{}).pop(part[idx].get('动作ID'),None)
        save_ledger(ledger, cfg, ledger_conn)
        for e in err:
            idx = e.get("index") if isinstance(e, dict) else None
            r = part[idx] if isinstance(idx, int) and 0 <= idx < len(part) else {}
            failures.append(f"{kind} [{r.get('词') or r.get('targetId') or r.get('campaignId', '?')}] "
                            f"{e.get('code', '') if isinstance(e, dict) else ''}: {str(e.get('detail', ''))[:150] if isinstance(e, dict) else e}")

    def solo_probe(batch: list[dict], kind: str) -> bool:
        nonlocal ok_count
        if not batch:
            return True
        first = batch[0]
        try:
            ok, err = call(kind, [first])
        except Exception as e:
            print(f"  试探异常: {e}")
            return False
        apply_result(kind, [first], ok, err)
        if err or not ok:
            print(f"  试探失败/无法确认 ({kind}): {err[0] if err else '空成功列表'}")
            print("  按安全阀规则整批中止, 未改动其余条目。")
            return False
        print(f"  试探通过 ({kind}), 继续批量...")
        time.sleep(1)
        return True

    def run_batch(kind: str, batch: list[dict]) -> None:
        nonlocal ok_count
        chunk = 100
        for i in range(1, len(batch), chunk) if len(batch) > 1 else range(0):
            part = batch[i:i + chunk]
            try:
                ok, err = call(kind, part)
                apply_result(kind, part, ok, err)
            except Exception as e:
                failures.append(f"{kind} 批次异常: {e}")
            time.sleep(1)

    try:
        if not solo_probe(kws, "收割") or not solo_probe(pts, "收割ASIN") or not solo_probe(negs, "否定") \
                or not solo_probe(negasins, "否定ASIN") or not solo_probe(buds, "预算") \
                or not solo_probe(bids, "BID") or not solo_probe(kwbids, "关键词BID") or not solo_probe(pbids, "位置BID"):
            write_fail_log(failures, ok_count, path.parent)
            return False
        run_batch("收割", kws)
        run_batch("收割ASIN", pts)
        run_batch("否定", negs)
        run_batch("否定ASIN", negasins)
        run_batch("预算", buds)
        run_batch("BID", bids)
        run_batch("关键词BID", kwbids)
        run_batch("位置BID", pbids)
        write_fail_log(failures, ok_count, path.parent)
        if failures:
            print(f"  完成 {ok_count} 条, 失败 {len(failures)} 条 (见 result_*.txt)。CSV 保留可修正重跑。")
            return False
        if leftover_other_campaign:
            print(f"  全部成功: {ok_count} 条。{path.name} 还有 {leftover_other_campaign} 条其它活动的"
                  "已批准行没处理，暂不标 .done，等那些也执行完再标。")
            return True
        done = path.with_suffix(path.suffix + ".done")
        shutil.move(str(path), str(done))
        print(f"  全部成功: {ok_count} 条。{path.name} → {done.name}")
        return True
    finally:
        if ledger_conn is not None:
            ledger_conn.close()


def write_fail_log(failures: list[str], ok_count: int, run_dir: Path) -> None:
    p = run_dir / "执行结果.txt"
    p.parent.mkdir(exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"\n=== 执行 {time.strftime('%H:%M:%S')} 成功 {ok_count} 失败 {len(failures)} ===\n")
        for line in failures:
            f.write(line + "\n")


def run(target: str, go: bool = False, only: str | None = None,
        campaign: str | None = None) -> int:
    """执行一个板块 CSV, 或一个运行目录里的全部板块。返回退出码。

    campaign 给了就不按板块挑, 而是扫目标里的每个板块文件, 把 campaignId 匹配的
    (已批准的) 行一起执行——同一个活动的 05 BID调整 + 06 位置调整 这类联合算出来
    的建议, 不用先 --only 跑一遍再 --only 跑一遍。"""
    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    cap = cfg["安全"].get("单批上限", 200)
    # 干跑不初始化 API 客户端, 因此没有凭据也能校验/预览 CSV。
    client = (AdsClient(cfg["profile_id"], (HERE / cfg["凭据文件"]).resolve(), HERE / "data",
                        api_base=cfg.get("api_base"))
              if go else None)

    files, desc = resolve_target(target)
    if only:
        files = [f for f in files if only in f.name]
        if not files:
            print(f"--only {only}: 没有匹配的板块文件。")
            return 1
    mode = "正式执行" if go else "干跑"
    print(f"目标: {desc} | 板块 {len(files)} 个 | 模式: {mode}"
          + (f" | --campaign {campaign}" if campaign else ""))
    if go:
        print("[正式执行] 先发首条试探...")

    failed_files = []
    for f in files:
        print(f"\n=== {f.name} ===")
        if not execute_file(client, f, go, cap, campaign):
            failed_files.append(f.name)

    print("\n==== 汇总 ====")
    for f in files:
        print(f"  {'✗' if f.name in failed_files else '✓'} {f.name}")
    if failed_files:
        print(f"失败板块 {len(failed_files)} 个; 其余如已批准可单独重跑。")
        return 2
    print("全部板块处理完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        "\nexecute.py 现在是被 run.py 调用的一步, 不要直接跑它。\n"
        '  python run.py execute "2026-10-08 & 2"        干跑整个目录\n'
        '  python run.py execute --go --only 收割 "..."   只正式执行一个板块\n'
        "  python run.py --list                          有哪些操作\n")
