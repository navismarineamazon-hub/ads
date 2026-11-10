# -*- coding: utf-8 -*-
"""从 data/ads.db 生成一份自包含 HTML 看板 (output/dashboard.html)。

只读查询，不碰账户、不改数据库。想要看板数据更新，先跑一次
`python run.py suggest`（或任意 --only 板块）让管道把最新报表落进 DB，再跑这个脚本。

    python dashboard.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

# 多站点：见 rules.py 里同名注释。
HERE = Path(os.environ["ADS_SITE_DIR"]) if os.environ.get("ADS_SITE_DIR") else Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "ads.db"
OUT_PATH = HERE / "output" / "dashboard.html"


def product_code(campaign_name: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9]+)", campaign_name or "")
    return (m.group(1) if m else "UNKNOWN").upper()


_TARGETING_VALUE_RE = re.compile(r'^(asin-expanded|asin|category)="([^"]*)"$')


def classify_targeting(targeting: str | None) -> tuple[str, str] | None:
    """AUTO投放 report 的 targeting 原始表达式里，挑出竞品ASIN投放(asin/asin-expanded)
    和类目投放(category)；close-match/substitutes/loose-match/complements/keyword-group=...
    这些常规AUTO自动发现返回 None，不算竞品/类目监控的对象。"""
    if not targeting:
        return None
    m = _TARGETING_VALUE_RE.match(targeting.strip())
    if not m:
        return None
    kind, value = m.group(1), m.group(2)
    return ("竞品ASIN" if kind in ("asin", "asin-expanded") else "类目"), value


def collect(conn: sqlite3.Connection) -> dict:
    """从一个已打开的 ads.db 连接里把看板要的全部数据查出来，返回纯 dict/list（不含
    conn 本身），方便单站点(main)和跨站点汇总(cross_site_dashboard.py)复用同一份查询，
    不用维护两套 SQL。"""
    # 取各表口径最短的那个窗口 (通常是 7d 的"live"报表) 当"当前"数据
    def shortest_window(table: str) -> tuple[str, str, int] | None:
        row = conn.execute(
            f"SELECT start_date, end_date, MIN(report_days) d FROM {table}").fetchone()
        if row is None or row["d"] is None:
            return None
        row2 = conn.execute(
            f"SELECT start_date, end_date FROM {table} WHERE report_days=? LIMIT 1",
            (row["d"],)).fetchone()
        return (row2["start_date"], row2["end_date"], row["d"]) if row2 else None

    camp_win = shortest_window("fact_campaign")
    st_win = shortest_window("fact_search_term")
    tg_win = shortest_window("fact_targeting")

    campaigns = []
    kpis = {"cost": 0.0, "sales7d": 0.0, "purchases7d": 0, "enabled": 0, "total": 0}
    if camp_win:
        rows = conn.execute(
            "SELECT * FROM fact_campaign WHERE start_date=? AND end_date=? ORDER BY cost DESC",
            camp_win[:2]).fetchall()
        for r in rows:
            cost, sales = r["cost"] or 0.0, r["sales7d"] or 0.0
            campaigns.append({
                "campaignName": r["campaign_name"], "cost": round(cost, 2),
                "sales": round(sales, 2), "purchases": r["purchases7d"] or 0,
                "acos": round(cost / sales, 4) if sales else None,
                "budget": r["campaign_budget_amount"],
                "status": r["campaign_status"],
            })
            kpis["cost"] += cost
            kpis["sales7d"] += sales
            kpis["purchases7d"] += r["purchases7d"] or 0
            kpis["total"] += 1
            if r["campaign_status"] == "ENABLED":
                kpis["enabled"] += 1
    kpis["acos"] = (kpis["cost"] / kpis["sales7d"]) if kpis["sales7d"] else None

    # 活动结构: 同一产品代码下有几个活动 -- 直接对应"03 有 1 个 AU 和多个 KT"这类结构排查
    structure: dict[str, dict] = {}
    for c in campaigns:
        code = product_code(c["campaignName"])
        g = structure.setdefault(code, {"productCode": code, "campaigns": [],
                                        "enabled": 0, "paused": 0, "budget": 0.0, "cost": 0.0})
        g["campaigns"].append(c["campaignName"])
        g["enabled"] += 1 if c["status"] == "ENABLED" else 0
        g["paused"] += 1 if c["status"] == "PAUSED" else 0
        g["budget"] += c["budget"] or 0.0
        g["cost"] += c["cost"] or 0.0
    structure_rows = sorted(structure.values(), key=lambda g: len(g["campaigns"]), reverse=True)
    for g in structure_rows:
        g["campaignCount"] = len(g["campaigns"])
        g["budget"] = round(g["budget"], 2)
        g["cost"] = round(g["cost"], 2)

    # 零单浪费搜索词 (当前 7d 窗口, 有花费但 0 单)
    wasted = []
    if st_win:
        rows = conn.execute(
            "SELECT * FROM fact_search_term WHERE start_date=? AND end_date=? "
            "AND purchases7d=0 AND cost>=5 ORDER BY cost DESC LIMIT 50", st_win[:2]).fetchall()
        for r in rows:
            wasted.append({"campaignName": r["campaign_name"], "keyword": r["keyword"],
                          "matchType": r["match_type"], "searchTerm": r["search_term"],
                          "clicks": r["clicks"], "cost": round(r["cost"] or 0.0, 2)})

    # AUTO vs BROAD 投放: 按产品代码汇总花费/销售额/订单 (两边通常是不同活动, 按产品代码对齐)
    auto_vs_broad: dict[str, dict] = {}
    if tg_win:
        rows = conn.execute(
            "SELECT * FROM fact_targeting WHERE start_date=? AND end_date=?", tg_win[:2]).fetchall()
        for r in rows:
            code = product_code(r["campaign_name"])
            g = auto_vs_broad.setdefault(code, {"productCode": code,
                                                "autoCost": 0.0, "autoSales": 0.0, "autoPurchases": 0,
                                                "broadCost": 0.0, "broadSales": 0.0, "broadPurchases": 0})
            cost, sales, purch = r["cost"] or 0.0, r["sales7d"] or 0.0, r["purchases7d"] or 0
            if r["spec_name"] == "AUTO投放":
                g["autoCost"] += cost; g["autoSales"] += sales; g["autoPurchases"] += purch
            else:
                g["broadCost"] += cost; g["broadSales"] += sales; g["broadPurchases"] += purch
    avb_rows = sorted(auto_vs_broad.values(), key=lambda g: g["autoCost"] + g["broadCost"], reverse=True)
    for g in avb_rows:
        for k in ("autoCost", "autoSales", "broadCost", "broadSales"):
            g[k] = round(g[k], 2)

    # 竞品ASIN投放 / 类目投放: 从 targeting 原始表达式里挑出来, 单独汇总——这俩本来就在
    # AUTO投放 报表里, 只是一直跟 close-match/substitutes 混在一起没人单独看过;
    # 商品定位投放(如果拉了的话)专门补手动 Product Targeting 活动那部分。两份报表各自
    # 拉取的时间点不同, 最新窗口的具体日期经常对不上(比如 AUTO投放 还是上次 suggest
    # 跑的 10-19~10-25, 商品定位投放 是刚刚 dashboard 跑的 10-20~10-26)——不能像上面
    # AUTO vs BROAD 那样共用一个 tg_win, 否则窗口不对齐的那份会被整个滤掉, 等于白拉。
    # 所以这里各自用各自 spec_name 自己的最新窗口, 按 keywordId 去重(避免同一条定向
    # 被两份报表都算到的话重复计入)。
    def _spec_window(spec_name: str) -> tuple[str, str] | None:
        row = conn.execute(
            "SELECT start_date, end_date, MIN(report_days) d FROM fact_targeting WHERE spec_name=?",
            (spec_name,)).fetchone()
        if row is None or row["d"] is None:
            return None
        row2 = conn.execute(
            "SELECT start_date, end_date FROM fact_targeting WHERE spec_name=? AND report_days=? LIMIT 1",
            (spec_name, row["d"])).fetchone()
        return (row2["start_date"], row2["end_date"]) if row2 else None

    competitor_asin: dict[tuple, dict] = {}
    category_targeting: dict[tuple, dict] = {}
    seen_targeting_kw: set = set()
    for spec_name in ("AUTO投放", "商品定位投放"):
        win = _spec_window(spec_name)
        if not win:
            continue
        rows = conn.execute(
            "SELECT * FROM fact_targeting WHERE spec_name=? AND start_date=? AND end_date=?",
            (spec_name, *win)).fetchall()
        for r in rows:
            kw_id = r["keyword_id"]
            if kw_id in seen_targeting_kw:
                continue
            seen_targeting_kw.add(kw_id)
            cls = classify_targeting(r["targeting"])
            if not cls:
                continue
            kind, value = cls
            code = product_code(r["campaign_name"])
            cost, sales, purch = r["cost"] or 0.0, r["sales7d"] or 0.0, r["purchases7d"] or 0
            bucket = competitor_asin if kind == "竞品ASIN" else category_targeting
            b = bucket.setdefault((code, value), {"productCode": code, "value": value,
                                                   "cost": 0.0, "sales7d": 0.0, "purchases7d": 0,
                                                   "clicks": 0})
            b["cost"] += cost; b["sales7d"] += sales; b["purchases7d"] += purch
            b["clicks"] += r["clicks"] or 0

    def _finalize_targeting(bucket: dict) -> list[dict]:
        out = sorted(bucket.values(), key=lambda g: g["cost"], reverse=True)
        for g in out:
            g["cost"] = round(g["cost"], 2)
            g["sales7d"] = round(g["sales7d"], 2)
            g["acos"] = round(g["cost"] / g["sales7d"], 4) if g["sales7d"] else None
        return out

    competitor_asin_rows = _finalize_targeting(competitor_asin)
    category_targeting_rows = _finalize_targeting(category_targeting)

    freshness = conn.execute(
        "SELECT spec_name, status, start_date, end_date, updated_at FROM fetch_tasks "
        "ORDER BY updated_at DESC").fetchall()
    freshness = [dict(r) for r in freshness]

    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": kpis, "campaigns": campaigns, "structure": structure_rows,
        "wasted": wasted, "autoVsBroad": avb_rows, "freshness": freshness,
        "competitorAsin": competitor_asin_rows, "categoryTargeting": category_targeting_rows,
        "windows": {"campaign": camp_win, "searchTerm": st_win, "targeting": tg_win},
    }


def main(db_path: Path | None = None, out_path: Path | None = None) -> None:
    db_path = db_path or DB_PATH
    out_path = out_path or OUT_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        data = collect(conn)
    finally:
        conn.close()

    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"看板已生成: {out_path}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>广告数据看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1"></script>
<style>
:root {
  --bg-primary:#f8f9fa; --bg-card:#ffffff; --bg-header:#1a1a2e;
  --text-primary:#212529; --text-secondary:#6c757d; --text-on-dark:#ffffff;
  --c1:#4C72B0; --c2:#DD8452; --c3:#55A868; --c4:#C44E52; --c5:#8172B3;
  --positive:#28a745; --negative:#dc3545; --gap:16px; --radius:8px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;
  background:var(--bg-primary);color:var(--text-primary);line-height:1.5}
.container{max-width:1400px;margin:0 auto;padding:var(--gap)}
.header{background:var(--bg-header);color:var(--text-on-dark);padding:20px 24px;
  border-radius:var(--radius);margin-bottom:var(--gap);display:flex;justify-content:space-between;
  align-items:center;flex-wrap:wrap;gap:12px}
.header h1{font-size:20px;font-weight:600}
.header .meta{font-size:12px;color:rgba(255,255,255,.7)}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:var(--gap);margin-bottom:var(--gap)}
.card{background:var(--bg-card);border-radius:var(--radius);padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kpi-label{font-size:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.kpi-value{font-size:26px;font-weight:700}
.section-title{font-size:15px;font-weight:600;margin-bottom:12px}
.chart-row{display:grid;grid-template-columns:1.3fr 1fr;gap:var(--gap);margin-bottom:var(--gap)}
.chart-row .card canvas{max-height:320px}
.table-wrap{overflow-x:auto;max-height:480px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{position:sticky;top:0;background:var(--bg-card);text-align:left;padding:8px 10px;
  border-bottom:2px solid #dee2e6;color:var(--text-secondary);font-weight:600;font-size:11px;
  text-transform:uppercase;cursor:pointer;white-space:nowrap}
thead th:hover{color:var(--text-primary)}
tbody td{padding:7px 10px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
tbody tr:hover{background:#f8f9fa}
.acos-bad{color:var(--negative);font-weight:600}
.acos-good{color:var(--positive);font-weight:600}
.status-ENABLED{color:var(--positive)}.status-PAUSED{color:var(--text-secondary)}
.tag{display:inline-block;background:#eef1f6;border-radius:4px;padding:1px 6px;font-size:11px;margin-right:4px}
.section{margin-bottom:var(--gap)}
.campaign-list{font-size:12px;color:var(--text-secondary);white-space:normal;max-width:420px}
.footer{font-size:11px;color:var(--text-secondary);text-align:center;padding:12px}
@media(max-width:900px){.chart-row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>广告数据看板</h1>
    <div class="meta" id="meta"></div>
  </div>

  <div class="kpi-row">
    <div class="card"><div class="kpi-label">总花费 (当前窗口)</div><div class="kpi-value" id="kpi-cost"></div></div>
    <div class="card"><div class="kpi-label">总销售额</div><div class="kpi-value" id="kpi-sales"></div></div>
    <div class="card"><div class="kpi-label">总订单</div><div class="kpi-value" id="kpi-purchases"></div></div>
    <div class="card"><div class="kpi-label">整体 ACOS</div><div class="kpi-value" id="kpi-acos"></div></div>
    <div class="card"><div class="kpi-label">活动数 (启用/总数)</div><div class="kpi-value" id="kpi-campaigns"></div></div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">活动结构检查 —— 同一产品代码下有几个活动（排查"1个AU+多个KT"式冗余）</div>
      <div class="table-wrap">
        <table id="tbl-structure"><thead><tr>
          <th data-k="productCode">产品代码</th><th data-k="campaignCount">活动数量</th>
          <th data-k="enabled">启用</th><th data-k="paused">暂停</th>
          <th data-k="budget">总预算</th><th data-k="cost">总花费</th><th>活动名称</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>

  <div class="chart-row">
    <div class="card">
      <div class="section-title">花费 Top 15 活动（当前窗口）</div>
      <canvas id="chart-campaigns"></canvas>
    </div>
    <div class="card">
      <div class="section-title">AUTO vs BROAD 花费对比（按产品代码）</div>
      <canvas id="chart-avb"></canvas>
    </div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">活动明细（当前窗口，点列头排序）</div>
      <div class="table-wrap">
        <table id="tbl-campaigns"><thead><tr>
          <th data-k="campaignName">活动名称</th><th data-k="status">状态</th>
          <th data-k="cost">花费</th><th data-k="sales">销售额</th>
          <th data-k="purchases">订单</th><th data-k="acos">ACOS</th><th data-k="budget">预算</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">零单浪费搜索词 Top 50（有花费、0 订单，排查否定候选）</div>
      <div class="table-wrap">
        <table id="tbl-wasted"><thead><tr>
          <th data-k="campaignName">活动</th><th data-k="keyword">投放关键词</th>
          <th data-k="matchType">匹配类型</th><th data-k="searchTerm">搜索词</th>
          <th data-k="clicks">点击</th><th data-k="cost">花费</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>

  <div class="chart-row">
    <div class="card">
      <div class="section-title">竞品 ASIN 投放表现（AUTO投放里的 asin/asin-expanded 定向，按花费排序）</div>
      <div class="table-wrap">
        <table id="tbl-competitor"><thead><tr>
          <th data-k="productCode">产品代码</th><th data-k="value">竞品 ASIN</th>
          <th data-k="clicks">点击</th><th data-k="cost">花费</th>
          <th data-k="sales7d">销售额</th><th data-k="purchases7d">订单</th><th data-k="acos">ACOS</th>
        </tr></thead><tbody></tbody></table>
      </div>
      <div style="font-size:11px;color:var(--text-secondary);margin-top:8px">
        花了钱没转化的 ASIN 是否定候选；ACOS 低、订单稳定的可以考虑单独建 PT 活动加码。
      </div>
    </div>
    <div class="card">
      <div class="section-title">类目投放表现（AUTO投放里的 category 定向，Amazon自己判定的类目）</div>
      <div class="table-wrap">
        <table id="tbl-category"><thead><tr>
          <th data-k="productCode">产品代码</th><th data-k="value">Amazon判定类目</th>
          <th data-k="clicks">点击</th><th data-k="cost">花费</th>
          <th data-k="sales7d">销售额</th><th data-k="purchases7d">订单</th><th data-k="acos">ACOS</th>
        </tr></thead><tbody></tbody></table>
      </div>
      <div style="font-size:11px;color:var(--text-secondary);margin-top:8px">
        这是 Amazon 广告引擎认为你产品该归属的类目/场景——如果跟你的产品定位明显不搭
        （比如 workwear 产品却大量落在 hiking/outdoor 类目），值得去核对 listing 的
        类目和关键词是否让 Amazon 判断错了。
      </div>
    </div>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
const DATA = __DATA__;
const COLORS = ['#4C72B0','#DD8452','#55A868','#C44E52','#8172B3','#937860'];

function fmtMoney(v){ if(v==null) return '-'; return '$'+v.toLocaleString(undefined,{maximumFractionDigits:2}); }
function fmtPct(v){ if(v==null) return '-'; return (v*100).toFixed(1)+'%'; }
function fmtNum(v){ if(v==null) return '-'; return v.toLocaleString(); }

document.getElementById('meta').textContent =
  '生成时间: ' + DATA.generatedAt +
  (DATA.windows.campaign ? '　当前窗口: ' + DATA.windows.campaign[0] + ' ~ ' + DATA.windows.campaign[1] : '');

document.getElementById('kpi-cost').textContent = fmtMoney(DATA.kpis.cost);
document.getElementById('kpi-sales').textContent = fmtMoney(DATA.kpis.sales7d);
document.getElementById('kpi-purchases').textContent = fmtNum(DATA.kpis.purchases7d);
document.getElementById('kpi-acos').textContent = fmtPct(DATA.kpis.acos);
document.getElementById('kpi-campaigns').textContent = DATA.kpis.enabled + ' / ' + DATA.kpis.total;

function acosClass(v){ if(v==null) return ''; return v>0.35?'acos-bad':(v<0.2?'acos-good':''); }

function renderTable(tableId, rows, renderRow){
  const tbody = document.querySelector('#'+tableId+' tbody');
  tbody.innerHTML = rows.map(renderRow).join('');
  const ths = document.querySelectorAll('#'+tableId+' thead th[data-k]');
  let sortKey=null, sortDir=-1;
  ths.forEach(th=>{
    th.addEventListener('click', ()=>{
      const k = th.dataset.k;
      sortDir = (sortKey===k) ? -sortDir : -1;
      sortKey = k;
      rows.sort((a,b)=>{
        const av=a[k], bv=b[k];
        if(av==null) return 1; if(bv==null) return -1;
        if(typeof av==='string') return sortDir*av.localeCompare(bv);
        return sortDir*(av-bv);
      });
      tbody.innerHTML = rows.map(renderRow).join('');
    });
  });
}

renderTable('tbl-structure', DATA.structure, g => `<tr>
  <td>${g.productCode}</td><td>${g.campaignCount}</td><td>${g.enabled}</td><td>${g.paused}</td>
  <td>${fmtMoney(g.budget)}</td><td>${fmtMoney(g.cost)}</td>
  <td class="campaign-list">${g.campaigns.join('　')}</td></tr>`);

renderTable('tbl-campaigns', DATA.campaigns, c => `<tr>
  <td>${c.campaignName}</td><td class="status-${c.status}">${c.status}</td>
  <td>${fmtMoney(c.cost)}</td><td>${fmtMoney(c.sales)}</td><td>${fmtNum(c.purchases)}</td>
  <td class="${acosClass(c.acos)}">${fmtPct(c.acos)}</td><td>${fmtMoney(c.budget)}</td></tr>`);

renderTable('tbl-wasted', DATA.wasted, r => `<tr>
  <td>${r.campaignName}</td><td>${r.keyword||'-'}</td><td>${r.matchType||'-'}</td>
  <td>${r.searchTerm}</td><td>${fmtNum(r.clicks)}</td><td>${fmtMoney(r.cost)}</td></tr>`);

const targetingRow = g => `<tr>
  <td>${g.productCode}</td><td>${g.value}</td><td>${fmtNum(g.clicks)}</td>
  <td>${fmtMoney(g.cost)}</td><td>${fmtMoney(g.sales7d)}</td><td>${fmtNum(g.purchases7d)}</td>
  <td class="${acosClass(g.acos)}">${fmtPct(g.acos)}</td></tr>`;
renderTable('tbl-competitor', DATA.competitorAsin, targetingRow);
renderTable('tbl-category', DATA.categoryTargeting, targetingRow);

const top15 = [...DATA.campaigns].sort((a,b)=>b.cost-a.cost).slice(0,15);
new Chart(document.getElementById('chart-campaigns'), {
  type:'bar',
  data:{ labels: top15.map(c=>c.campaignName),
         datasets:[{label:'花费', data: top15.map(c=>c.cost), backgroundColor:'#4C72B0CC'}]},
  options:{ responsive:true, maintainAspectRatio:false, indexAxis:'y',
            plugins:{legend:{display:false}},
            scales:{x:{beginAtZero:true}}}
});

const avb = DATA.autoVsBroad.slice(0,15);
new Chart(document.getElementById('chart-avb'), {
  type:'bar',
  data:{ labels: avb.map(g=>g.productCode),
         datasets:[
           {label:'AUTO 花费', data: avb.map(g=>g.autoCost), backgroundColor:'#4C72B0CC'},
           {label:'BROAD 花费', data: avb.map(g=>g.broadCost), backgroundColor:'#DD8452CC'},
         ]},
  options:{ responsive:true, maintainAspectRatio:false,
            plugins:{legend:{position:'top'}},
            scales:{y:{beginAtZero:true}}}
});

document.getElementById('footer').textContent =
  '数据来源: data/ads.db（Phase 1/2 管道落地，持续积累历史）。重新生成: python dashboard.py';
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
