# -*- coding: utf-8 -*-
"""跨站点汇总看板：把 sites/*/data/ads.db（以及根目录自己的 data/ads.db，当默认站点）
各自查一遍，在 Python 里按站点标签合并成一份自包含 HTML。

不是真的建一个跨站点数据库——每个站点的 ads.db 还是各自独立、各自的 suggest/dashboard
各自刷新；这里只是只读地挨个打开、跑 dashboard.py 已有的同一套查询(collect())、按站点
打标签拼起来，纯汇总展示用。缺某个站点的 ads.db（还没跑过 suggest）就跳过，不报错。

    python run.py sites-dashboard
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import dashboard

CODE_DIR = Path(__file__).resolve().parent
OUT_PATH = CODE_DIR / "output" / "跨站点汇总看板.html"


def _site_dirs() -> list[tuple[str, Path]]:
    """(站点标签, 站点根目录) 列表。根目录本身算默认站点"美"；sites/ 下每个有自己
    config.yaml 的子目录算一个站点，按名字排序，默认站点排最前。"""
    out = [("默认", CODE_DIR)]
    sites_root = CODE_DIR / "sites"
    if sites_root.is_dir():
        for p in sorted(sites_root.iterdir()):
            if p.is_dir() and (p / "config.yaml").exists():
                out.append((p.name, p))
    return out


def collect_all() -> list[dict]:
    """每个站点开自己的 ads.db 跑 dashboard.collect()，没有 db 文件就跳过并打印提示。"""
    results = []
    for label, root in _site_dirs():
        db_path = root / "data" / "ads.db"
        if not db_path.exists():
            print(f"[跨站点看板] 站点「{label}」还没有 {db_path}（没跑过 suggest/dashboard），跳过")
            continue
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            data = dashboard.collect(conn)
        finally:
            conn.close()
        data["site"] = label
        results.append(data)
    return results


def main(out_path: Path | None = None) -> None:
    out_path = out_path or OUT_PATH
    per_site = collect_all()
    if not per_site:
        print("[跨站点看板] 一个站点的 ads.db 都没找到，没法生成。先跑至少一个站点的 "
              "suggest 或 dashboard。")
        return

    site_kpis = []
    wasted_all = []
    for d in per_site:
        k = d["kpis"]
        site_kpis.append({
            "site": d["site"], "cost": round(k["cost"], 2), "sales7d": round(k["sales7d"], 2),
            "purchases7d": k["purchases7d"], "acos": k["acos"],
            "enabled": k["enabled"], "total": k["total"],
        })
        for r in d["wasted"]:
            wasted_all.append({**r, "site": d["site"]})
    wasted_all.sort(key=lambda r: r["cost"], reverse=True)

    data = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "siteKpis": site_kpis,
        "wasted": wasted_all[:100],
        "siteCount": len(per_site),
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"跨站点汇总看板已生成（{len(per_site)} 个站点）: {out_path}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>跨站点汇总看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1"></script>
<style>
:root {
  --bg-primary:#f8f9fa; --bg-card:#ffffff; --bg-header:#1a1a2e;
  --text-primary:#212529; --text-secondary:#6c757d; --text-on-dark:#ffffff;
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
.card{background:var(--bg-card);border-radius:var(--radius);padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:var(--gap)}
.section-title{font-size:15px;font-weight:600;margin-bottom:12px}
.chart-row{display:grid;grid-template-columns:1fr 1fr;gap:var(--gap);margin-bottom:var(--gap)}
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
.site-tag{display:inline-block;background:#eef1f6;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:600}
.footer{font-size:11px;color:var(--text-secondary);text-align:center;padding:12px}
@media(max-width:900px){.chart-row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>跨站点汇总看板</h1>
    <div class="meta" id="meta"></div>
  </div>

  <div class="card">
    <div class="section-title">站点对比（各自当前窗口，口径可能不完全对齐，仅供横向参考）</div>
    <div class="table-wrap">
      <table id="tbl-sites"><thead><tr>
        <th data-k="site">站点</th><th data-k="cost">花费</th><th data-k="sales7d">销售额</th>
        <th data-k="purchases7d">订单</th><th data-k="acos">ACOS</th>
        <th data-k="enabled">启用活动</th><th data-k="total">总活动</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class="chart-row">
    <div class="card">
      <div class="section-title">各站点花费 / 销售额</div>
      <canvas id="chart-cost"></canvas>
    </div>
    <div class="card">
      <div class="section-title">各站点 ACOS</div>
      <canvas id="chart-acos"></canvas>
    </div>
  </div>

  <div class="card">
    <div class="section-title">跨站点零单浪费搜索词 Top 100（有花费、0 订单，按花费排序）</div>
    <div class="table-wrap">
      <table id="tbl-wasted"><thead><tr>
        <th data-k="site">站点</th><th data-k="campaignName">活动</th>
        <th data-k="searchTerm">搜索词</th><th data-k="clicks">点击</th><th data-k="cost">花费</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
const DATA = __DATA__;

function fmtMoney(v){ if(v==null) return '-'; return '$'+v.toLocaleString(undefined,{maximumFractionDigits:2}); }
function fmtPct(v){ if(v==null) return '-'; return (v*100).toFixed(1)+'%'; }
function fmtNum(v){ if(v==null) return '-'; return v.toLocaleString(); }
function acosClass(v){ if(v==null) return ''; return v>0.35?'acos-bad':(v<0.2?'acos-good':''); }

document.getElementById('meta').textContent =
  '生成时间: ' + DATA.generatedAt + '　汇总站点数: ' + DATA.siteCount;

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

renderTable('tbl-sites', DATA.siteKpis, s => `<tr>
  <td><span class="site-tag">${s.site}</span></td><td>${fmtMoney(s.cost)}</td>
  <td>${fmtMoney(s.sales7d)}</td><td>${fmtNum(s.purchases7d)}</td>
  <td class="${acosClass(s.acos)}">${fmtPct(s.acos)}</td>
  <td>${s.enabled}</td><td>${s.total}</td></tr>`);

renderTable('tbl-wasted', DATA.wasted, r => `<tr>
  <td><span class="site-tag">${r.site}</span></td><td>${r.campaignName}</td>
  <td>${r.searchTerm}</td><td>${fmtNum(r.clicks)}</td><td>${fmtMoney(r.cost)}</td></tr>`);

new Chart(document.getElementById('chart-cost'), {
  type:'bar',
  data:{ labels: DATA.siteKpis.map(s=>s.site),
         datasets:[
           {label:'花费', data: DATA.siteKpis.map(s=>s.cost), backgroundColor:'#4C72B0CC'},
           {label:'销售额', data: DATA.siteKpis.map(s=>s.sales7d), backgroundColor:'#55A868CC'},
         ]},
  options:{ responsive:true, maintainAspectRatio:false,
            plugins:{legend:{position:'top'}}, scales:{y:{beginAtZero:true}}}
});

new Chart(document.getElementById('chart-acos'), {
  type:'bar',
  data:{ labels: DATA.siteKpis.map(s=>s.site),
         datasets:[{label:'ACOS', data: DATA.siteKpis.map(s=>s.acos),
                    backgroundColor: DATA.siteKpis.map(s=>acosClass(s.acos)==='acos-bad'?'#dc3545CC':'#4C72B0CC')}]},
  options:{ responsive:true, maintainAspectRatio:false,
            plugins:{legend:{display:false}},
            scales:{y:{beginAtZero:true,ticks:{callback:v=>(v*100).toFixed(0)+'%'}}}}
});

document.getElementById('footer').textContent =
  '数据来源: 各站点自己的 data/ads.db。重新生成: python run.py sites-dashboard';
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
