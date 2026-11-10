# -*- coding: utf-8 -*-
"""从 rules.py 算出来的 overlap_rows/gap_rows 生成一份自包含 HTML 看板
(output/重叠分析看板.html)。只读渲染, 不查账户, 不改数据。

这是独立于 output/<run_dir>/产品线重叠.md 和 词库缺口.md 的一份持久化文件——那两份
在轮次目录里, 会跟着"输出.保留轮数"被清理掉; 这份在 output/ 根目录, 每次 suggest()
只要拉到新鲜的 搜索词历史 数据就覆盖重写, 不会被清理, 随数据更新。

    由 rules.suggest() 调用, 一般不用手动跑。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _role_bucket(role: str) -> str:
    if "梯度内" in role:
        return "梯度内竞争(正常)"
    if "跨类目" in role:
        return "跨类目(观察)"
    if "不建议减少" in role:
        return "弱势/新品保护"
    if "头部" in role:
        return "头部(建议保留)"
    if "建议减少" in role:
        return "建议减少/否定"
    return "其他"


def main(overlap_rows: list[dict], gap_rows: list[dict], product_summary: list[dict],
        product_stems: dict[str, list[dict]], day: str, out_path: Path,
        default_min_clicks: int = 5) -> None:
    # 1) 类目 x 关系类型 计数矩阵 —— 服务端只算一版"全量"(阈值=1click)的, 前端拿
    #    unifiedTable 自己按当前阈值重新统计, 这里只是给没碰过滑杆之前的初始渲染用。
    buckets = ["梯度内竞争(正常)", "头部(建议保留)", "建议减少/否定",
              "跨类目(观察)", "弱势/新品保护"]

    # 2) 重叠明细 + 词库缺口 合并成一份统一表, 每行带"类型"区分来源, 供前端用同一套
    #    产品勾选/点击量/展示量/订单量阈值筛选, 一目了然地对比。
    unified_table = []
    for r in overlap_rows:
        unified_table.append({
            "产品码": r["产品码"], "类目": r["类目"], "类型": "重叠竞争",
            "词根": r["词根组"], "角色": r["角色"], "活动": r["活动"],
            "impressions": r.get("impressions"), "clicks": r["clicks"], "cost": r["cost"],
            "purchases7d": r["purchases7d"], "acos": r["acos"],
        })
    for r in gap_rows:
        is_seed = r["来源"] != "同类目已验证"
        unified_table.append({
            "产品码": r["产品码"], "类目": r["类目"],
            "类型": "词库缺口-种子词库(未验证)" if is_seed else "词库缺口-同类目已验证",
            "词根": r["词根"], "角色": "种子词库(未验证)" if is_seed else "同类目已验证",
            "活动": r.get("验证活动") or "",
            "impressions": r.get("impressions"), "clicks": r.get("clicks"), "cost": r.get("cost"),
            "purchases7d": r.get("purchases7d"), "acos": r.get("acos"),
        })

    data = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "day": day,
        "buckets": buckets,
        "unifiedTable": unified_table,
        "overlapGroups": len({r["词根组"] for r in overlap_rows}),
        "gapProducts": len({r["产品码"] for r in gap_rows}),
        "productSummary": product_summary,
        "productStems": product_stems,
        "defaultMinClicks": default_min_clicks,
        "defaultMinImpressions": 0,
        "defaultMinOrders": 0,
    }

    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"重叠分析看板已生成: {out_path}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>关键词重叠 / 词库缺口看板</title>
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
.container{max-width:1500px;margin:0 auto;padding:var(--gap)}
.header{background:var(--bg-header);color:var(--text-on-dark);padding:20px 24px;
  border-radius:var(--radius);margin-bottom:var(--gap);display:flex;justify-content:space-between;
  align-items:center;flex-wrap:wrap;gap:12px}
.header h1{font-size:20px;font-weight:600}
.header .meta{font-size:12px;color:rgba(255,255,255,.7)}
.card{background:var(--bg-card);border-radius:var(--radius);padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.section{margin-bottom:var(--gap)}
.section-title{font-size:15px;font-weight:600;margin-bottom:12px}
.section-sub{font-size:12px;color:var(--text-secondary);margin-bottom:12px}
.chart-row{display:grid;grid-template-columns:1fr;gap:var(--gap);margin-bottom:var(--gap)}
.chart-row .card canvas{max-height:320px}
.table-wrap{overflow-x:auto;max-height:520px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{position:sticky;top:0;background:var(--bg-card);text-align:left;padding:8px 10px;
  border-bottom:2px solid #dee2e6;color:var(--text-secondary);font-weight:600;font-size:11px;
  text-transform:uppercase;cursor:pointer;white-space:nowrap}
thead th:hover{color:var(--text-primary)}
tbody td{padding:7px 10px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
tbody tr:hover{background:#f8f9fa}
.tag{display:inline-block;background:#eef1f6;border-radius:4px;padding:3px 8px;font-size:12px;margin:3px}
.type-overlap{color:var(--c1);font-weight:600}
.type-gap-verified{color:var(--c2);font-weight:600}
.type-gap-seed{color:var(--text-secondary)}
.role-ladder{color:var(--c3);font-weight:600}
.role-cross{color:var(--c5)}
.role-protect{color:var(--c2)}
.role-head{color:var(--positive);font-weight:600}
.role-reduce{color:var(--negative)}
.filters{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.filters select,.filters input[type=text]{padding:6px 10px;border:1px solid #dee2e6;border-radius:6px;font-size:13px}
.threshold-box{display:flex;gap:18px;flex-wrap:wrap;align-items:center;background:#f3f5f9;
  border-radius:8px;padding:10px 14px;margin-bottom:12px}
.threshold-box .item{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-secondary)}
.threshold-box input[type=range]{width:140px}
.threshold-box input[type=number]{width:64px;padding:4px 6px;border:1px solid #dee2e6;border-radius:6px;font-size:12px}
.threshold-box .val{font-weight:600;color:var(--text-primary);min-width:28px;display:inline-block}
.product-block{margin-bottom:18px;padding-bottom:12px;border-bottom:1px solid #f0f0f0}
.product-block:last-child{border-bottom:none}
.product-title{font-weight:600;margin-bottom:8px}
.chip-check{display:inline-flex;align-items:center;gap:5px;background:#eef1f6;border-radius:14px;
  padding:5px 12px;font-size:12px;cursor:pointer;user-select:none}
.chip-check input{cursor:pointer}
.chip-check.checked{background:#4C72B0;color:#fff}
.exclusive-col{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:14px}
.exclusive-card{background:#f3faf4;border:1px solid #cfe8d2;border-radius:8px;padding:12px}
.exclusive-card h4{font-size:13px;margin-bottom:8px;color:#2e8b4f}
.exclusive-card .term-row{font-size:12px;padding:3px 0;border-bottom:1px dashed #e3f0e4;
  display:flex;justify-content:space-between;gap:6px}
.shared-note{font-size:12px;color:var(--text-secondary);margin:10px 0}
.footer{font-size:11px;color:var(--text-secondary);text-align:center;padding:12px}
.heatmap-controls{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.heatmap-controls input[type=number]{width:64px;padding:4px 6px;border:1px solid #dee2e6;border-radius:6px;font-size:12px}
.legend-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:12px;font-size:12px;color:var(--text-secondary)}
.legend-swatch{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.heatmap-wrap{overflow:auto;max-height:620px;margin-bottom:var(--gap)}
.guide{background:#f3f5f9;border:1px solid #e3e7ee;border-radius:8px;padding:12px 16px;margin-bottom:14px}
.guide summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--text-primary);list-style:none}
.guide summary::-webkit-details-marker{display:none}
.guide summary::before{content:'▸ ';color:var(--text-secondary)}
.guide[open] summary::before{content:'▾ '}
.guide .guide-body{margin-top:12px;font-size:12.5px;color:var(--text-primary)}
.guide .guide-body h5{font-size:12.5px;font-weight:600;margin:14px 0 6px}
.guide .guide-body h5:first-child{margin-top:0}
.guide .guide-body p,.guide .guide-body ul{margin-bottom:8px}
.guide .guide-body ul{padding-left:18px}
.guide .guide-body li{margin-bottom:4px}
table.guide-table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:8px}
table.guide-table th,table.guide-table td{border:1px solid #dfe3ea;padding:6px 8px;text-align:left;
  vertical-align:top;white-space:normal;position:static}
table.guide-table th{background:#eceff5;font-weight:600;color:var(--text-primary);
  text-transform:none;cursor:default}
.swatch-inline{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:4px;vertical-align:-1px}
table.heatmap{border-collapse:separate;border-spacing:2px;font-size:12px}
table.heatmap th{position:sticky;top:0;background:var(--bg-card);z-index:2;padding:4px 6px;
  font-size:11px;font-weight:600;color:var(--text-secondary);white-space:nowrap;text-align:center;
  cursor:default;text-transform:none;border-bottom:none}
table.heatmap th.corner{left:0;z-index:3;text-align:left}
table.heatmap td.row-label{position:sticky;left:0;background:var(--bg-card);z-index:1;
  padding:4px 10px;font-size:12px;white-space:nowrap;max-width:220px;overflow:hidden;
  text-overflow:ellipsis;text-align:left;border-bottom:none;cursor:default}
table.heatmap td.heat-cell{width:54px;height:28px;min-width:54px;text-align:center;
  border-radius:4px;color:#fff;font-size:11px;font-weight:600;cursor:default;border-bottom:none}
table.heatmap td.heat-blank{background:#f1f3f5}
@media(max-width:900px){.chart-row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>关键词重叠 / 词库缺口看板</h1>
    <div class="meta" id="meta"></div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">按类目的重叠关系分布</div>
      <div class="section-sub">
        绿=梯度内竞争(正常,如03/05/07价格梯度) ｜ 蓝紫=跨类目(观察,如sailing蹭workwear的词,不建议切断)
        ｜ 橙=弱势/新品保护(不建议减少,去看词库缺口) ｜ 深绿=头部建议保留 ｜ 红=建议减少/否定 ｜
        跟下面表格共用同一组点击量/展示量/订单量阈值滑杆, 调阈值这张图也会跟着变。
      </div>
      <div style="height:360px"><canvas id="chart-matrix"></canvas></div>
    </div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">重叠明细 + 词库缺口 一览 (<span id="row-count"></span> 行)</div>
      <div class="section-sub">
        重叠竞争(来自产品线重叠检测) 和 词库缺口(弱势/新品的机会词) 合在一张表里,
        按产品/类目/类型筛选, 用下面的滑杆自主调点击量/展示量/订单量阈值——阈值越低看到的
        长尾词越多, 越高越聚焦在真正有数据支撑的词上。
      </div>
      <div class="threshold-box">
        <div class="item">点击量 ≥ <input type="range" id="th-clicks-range" min="1" max="200" step="1">
          <input type="number" id="th-clicks-num" min="1"><span class="val" id="th-clicks-val"></span></div>
        <div class="item">展示量 ≥ <input type="range" id="th-impr-range" min="0" max="5000" step="10">
          <input type="number" id="th-impr-num" min="0"><span class="val" id="th-impr-val"></span></div>
        <div class="item">订单量(purchases7d) ≥ <input type="range" id="th-orders-range" min="0" max="50" step="1">
          <input type="number" id="th-orders-num" min="0"><span class="val" id="th-orders-val"></span></div>
        <div class="item" style="color:var(--text-secondary)">(词库缺口里没有点击/展示/订单数据的行——比如种子词库未验证词——不受阈值影响, 始终显示)</div>
      </div>
      <div class="filters">
        <select id="f-type"><option value="">全部类型</option></select>
        <select id="f-role"><option value="">全部角色/来源</option></select>
        <select id="f-cat"><option value="">全部类目</option></select>
        <input type="text" id="f-term" placeholder="搜词根...">
      </div>
      <div class="filters" id="f-product-checks"></div>

      <div class="section-title" style="font-size:13px;margin-top:4px">词根 × 产品 占用热力图</div>
      <div class="section-sub">
        横轴=产品(勾选上面的产品框可以精确选列; 不勾选默认按花费排前<span id="heat-topn-products-label"></span>个产品)、
        纵轴=词根(按点击量排序, 只显示前N个, 可调)。格子颜色=该词在该产品上的状态和强弱:
        深浅由数值(点击量/订单数)决定, 色系看图例。空白=这个词跟该产品没关系。
      </div>

      <details class="guide" open>
        <summary>📖 热力图怎么看 / 不同表现的词该对应什么操作（详细说明，点击收起）</summary>
        <div class="guide-body">
          <h5>先看坐标和格子本身</h5>
          <ul>
            <li><b>横轴</b>每一列是一个产品代码；<b>纵轴</b>每一行是一个"词根"
              （已经把单复数/进行时等词形变化合并、去掉 for/a/the 这类虚词——
              "rain jacket for men" 和 "rain jackets men" 会算同一行）。</li>
            <li><b>颜色</b> = 这个词在这个产品身上处于什么状态（重叠竞争的哪种角色，还是词库缺口）；
              <b>深浅</b> = 数据强度（点击量，或词库缺口里验证来源的订单数），颜色越深代表越值得优先看。</li>
            <li><b>空白格</b> = 当前筛选/阈值下，这个词跟这个产品没关系——不是它在抢的词，也不是它的缺口机会。</li>
            <li>鼠标悬停在任意格子上可以看到完整数字（clicks / cost / purchases7d / ACOS，或验证来源的订单数）。</li>
          </ul>

          <h5>颜色 + 深浅 对应什么操作</h5>
          <table class="guide-table">
            <thead><tr><th>颜色</th><th>深浅代表</th><th>什么意思</th><th>建议操作</th></tr></thead>
            <tbody>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(46,139,79)"></span>深绿 头部(建议保留)</td>
                <td>点击量</td>
                <td>这个产品在这个词上同类目排第一、表现最好</td>
                <td>保留投放；表现稳定的话可以适度加预算/提价抢更多曝光</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(85,168,104)"></span>浅绿 梯度内竞争(正常)</td>
                <td>点击量</td>
                <td>03/05/07这类价格梯度内部同时出现——是故意分层帮用户比价，不是内耗</td>
                <td>不用动，这是设计好的正常现象</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(196,78,82)"></span>红 建议减少/否定</td>
                <td>点击量</td>
                <td>真正同类目内耗——这个产品在这个词上排名靠后，抢的是同类目里表现更好那个产品的量</td>
                <td>去下面"明细表"按这个词根搜索，找到同词根里表现更好的那个活动，
                  把<b>这一行（排名靠后的活动）</b>里的词降价或否定掉</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(129,114,179)"></span>蓝紫 跨类目(观察)</td>
                <td>点击量</td>
                <td>不同类目在抢同一个词（比如sailing蹭了workwear的词），可能是在借量导流</td>
                <td>先看这个词在该类目自己的转化质量（ACOS/购买数）——质量好就留着，
                  不建议直接切断；质量差再单独否定这一条词</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(221,132,82)"></span>橙 弱势/新品保护</td>
                <td>点击量</td>
                <td>产品阶段=weak(卖得差)/new(新品)，它缺的是词、不是要收缩投放</td>
                <td>不建议减少投放；去同一产品的蓝色/灰色格子（词库缺口）里找机会词补上</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(41,128,185)"></span>深蓝 词库缺口-同类目已验证</td>
                <td>验证来源订单数</td>
                <td>这个产品自己没用过这个词，但同类目别的产品已经靠它出单了</td>
                <td>优先尝试：把这个词加到这个产品里（抄同类目作业）——颜色越深说明验证来源
                  订单数越多，越值得优先测试</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:rgb(149,165,166)"></span>灰 词库缺口-种子词库(未验证)</td>
                <td>固定浅灰, 无强度</td>
                <td>账户历史里完全没出现过——来自美区购物搜索习惯补充的候选词，没有任何数据支撑</td>
                <td>需要人工判断是否贴合这个产品的卖点，小范围/低预算试投，不要批量上</td>
              </tr>
              <tr>
                <td><span class="swatch-inline" style="background:#f1f3f5;border:1px solid #dee2e6"></span>空白</td>
                <td>—</td>
                <td>这个词跟该产品没关系——不在它的竞争范围，也不是它的缺口机会</td>
                <td>不用管</td>
              </tr>
            </tbody>
          </table>

          <h5>几个一眼就能看出来的典型场景</h5>
          <ul>
            <li><b>同一行好几个产品都是深绿/浅绿</b> → 典型价格梯度内竞争，正常，不用处理。</li>
            <li><b>同一行只有1个产品有颜色，其它全空白</b> → 这是该产品独占的搜索需求，
              适合直接写进 listing 标题/五点/A+ 做差异化卖点（对应下面"按产品比较表现"
              区块里的"独有词根"卡片，可以批量导出看）。</li>
            <li><b>同一行红色+绿色同时出现</b> → 真正内耗：绿色那个保留，红色那个该降价/否定。</li>
            <li><b>同一行只有蓝紫（跨类目）</b> → 别的类目在蹭这个词，先确认它自己的转化质量再决定要不要动。</li>
            <li><b>某个弱势/新品那一列几乎全是橙色、很少深绿</b> → 这个产品的词还没跑出头部，
              去它那一列的蓝色/灰色格子（词库缺口）里找机会词补进去，而不是在重叠竞争里找。</li>
          </ul>

          <h5>配合其它控件一起用</h5>
          <ul>
            <li>调低点击量/展示量/订单量阈值滑杆 → 能看到更多长尾词（噪音也会变多）；调高 → 只聚焦真正有数据支撑的词。</li>
            <li>不勾选产品 = 默认显示花费最高的12个产品；勾选具体产品 = 精确控制要横向对比谁。</li>
            <li>热力图看出问题后，去下面"明细表"按产品码/词根搜索定位到具体的活动名字，
              再去 Amazon 后台做实际调整；也可以用更下面的"按产品比较表现"/"按词根组比较"
              两个区块做更细的交叉对比。</li>
          </ul>
        </div>
      </details>

      <div class="heatmap-controls">
        <div>纵轴显示词根数(按点击量topN) <input type="number" id="heat-topn" min="5" max="200" value="40"></div>
      </div>
      <div class="legend-row" id="heat-legend"></div>
      <div class="heatmap-wrap" id="heatmap-wrap">
        <table class="heatmap" id="tbl-heatmap"></table>
      </div>

      <div class="section-title" style="font-size:13px;margin-top:4px">明细表(可排序/逐行查看)</div>
      <div class="table-wrap">
        <table id="tbl-unified"><thead><tr>
          <th data-k="产品码">产品码</th><th data-k="类目">类目</th><th data-k="类型">类型</th>
          <th data-k="词根">词根</th><th data-k="角色">角色/来源</th><th data-k="活动">活动</th>
          <th data-k="impressions">impressions</th><th data-k="clicks">clicks</th>
          <th data-k="cost">cost</th><th data-k="purchases7d">purchases7d</th><th data-k="acos">ACOS</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">按产品比较表现</div>
      <div class="section-sub">
        勾选几个产品——先看整体表现对比，再看这几个产品之间"共同竞争的词根"
        vs "各自独有、没被组内其它产品瓜分的词根"：独有词是 listing/A+ 差异化定位的参考——
        这是该产品在这个比较范围里真正"属于自己"的搜索需求。
      </div>
      <div class="filters" id="product-checks"></div>
      <div id="product-compare-result"></div>
    </div>
  </div>

  <div class="section">
    <div class="card">
      <div class="section-title">按词根组比较</div>
      <div class="section-sub">勾几个词根组，把它们的完整竞争明细摆到一起看。</div>
      <input type="text" id="term-search" placeholder="搜词根组, 回车或点下面列表勾选...">
      <div class="filters" id="term-checks" style="max-height:140px;overflow-y:auto;margin-top:8px"></div>
      <div class="table-wrap" style="margin-top:12px">
        <table id="tbl-term-compare"><thead><tr>
          <th>词根组</th><th>活动</th><th>产品码</th><th>类目</th><th>角色</th>
          <th>clicks</th><th>cost</th><th>purchases7d</th><th>ACOS</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
const DATA = __DATA__;
const COLORS = {
  "梯度内竞争(正常)":"#55A868CC", "头部(建议保留)":"#2e8b4fCC", "建议减少/否定":"#C44E52CC",
  "跨类目(观察)":"#8172B3CC", "弱势/新品保护":"#DD8452CC",
};
// 热力图用的 RGB 三元组(跟上面 COLORS 同色系, 方便拼 rgba() 动态调透明度做深浅)。
const ROLE_RGB = {
  "梯度内竞争(正常)":[85,168,104], "头部(建议保留)":[46,139,79], "建议减少/否定":[196,78,82],
  "跨类目(观察)":[129,114,179], "弱势/新品保护":[221,132,82],
};
const GAP_VERIFIED_RGB = [41,128,185];   // 词库缺口-同类目已验证: 蓝色, 深浅=验证来源订单数
const GAP_SEED_RGB = [149,165,166];      // 词库缺口-种子词库(未验证): 灰色, 无数据支撑

function bucketOf(role) {
  if (role.includes('梯度内')) return '梯度内竞争(正常)';
  if (role.includes('跨类目')) return '跨类目(观察)';
  if (role.includes('不建议减少')) return '弱势/新品保护';
  if (role.includes('头部')) return '头部(建议保留)';
  if (role.includes('建议减少')) return '建议减少/否定';
  return '其他';
}

document.getElementById('meta').textContent =
  '生成时间: ' + DATA.generatedAt + '　数据轮次: ' + DATA.day;
document.getElementById('footer').textContent =
  '数据来源: 30天 搜索词历史 报表 (全账户)。由 python run.py suggest 每次跑完自动重新生成, 不会被轮次清理。';

// 搜索词/活动名是账户里的真实文本, 可能带 <, >, &, " 这类字符(真实客户搜索词常见
// 英文引号/符号) —— 用 innerHTML 拼表格前必须转义, 否则会拼坏HTML甚至截断渲染。
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function roleClass(role) {
  if (role.includes('梯度内')) return 'role-ladder';
  if (role.includes('跨类目')) return 'role-cross';
  if (role.includes('不建议减少')) return 'role-protect';
  if (role.includes('头部')) return 'role-head';
  if (role.includes('建议减少')) return 'role-reduce';
  return '';
}
function typeClass(t) {
  if (t === '重叠竞争') return 'type-overlap';
  if (t === '词库缺口-同类目已验证') return 'type-gap-verified';
  if (t === '词库缺口-种子词库(未验证)') return 'type-gap-seed';
  return '';
}
function fmtMoney(v){ if(v==null) return '-'; return '$'+v.toLocaleString(undefined,{maximumFractionDigits:2}); }
function fmtPct(v){ if(v==null) return '-'; return (v*100).toFixed(1)+'%'; }
function fmtNum(v){ return v==null ? '-' : v; }

// ---------------- 阈值滑杆 (点击量/展示量/订单量), 自主调, 实时过滤表格+重算矩阵图 ----------------
const thClicksRange = document.getElementById('th-clicks-range');
const thClicksNum = document.getElementById('th-clicks-num');
const thClicksVal = document.getElementById('th-clicks-val');
const thImprRange = document.getElementById('th-impr-range');
const thImprNum = document.getElementById('th-impr-num');
const thImprVal = document.getElementById('th-impr-val');
const thOrdersRange = document.getElementById('th-orders-range');
const thOrdersNum = document.getElementById('th-orders-num');
const thOrdersVal = document.getElementById('th-orders-val');
[thClicksRange, thClicksNum].forEach(el => { el.value = DATA.defaultMinClicks; });
[thImprRange, thImprNum].forEach(el => { el.value = DATA.defaultMinImpressions; });
[thOrdersRange, thOrdersNum].forEach(el => { el.value = DATA.defaultMinOrders; });
thClicksVal.textContent = DATA.defaultMinClicks;
thImprVal.textContent = DATA.defaultMinImpressions;
thOrdersVal.textContent = DATA.defaultMinOrders;

function syncThreshold(rangeEl, numEl, valEl, cb) {
  const sync = (v) => { rangeEl.value = v; numEl.value = v; valEl.textContent = v; cb(); };
  rangeEl.addEventListener('input', () => sync(rangeEl.value));
  numEl.addEventListener('input', () => { if (numEl.value !== '') sync(numEl.value); });
}
const onThresholdChange = () => { renderUnified(); renderMatrix(); renderHeatmap(); };
syncThreshold(thClicksRange, thClicksNum, thClicksVal, onThresholdChange);
syncThreshold(thImprRange, thImprNum, thImprVal, onThresholdChange);
syncThreshold(thOrdersRange, thOrdersNum, thOrdersVal, onThresholdChange);

function passesThreshold(r) {
  const minClicks = Number(thClicksNum.value) || 0;
  const minImpr = Number(thImprNum.value) || 0;
  const minOrders = Number(thOrdersNum.value) || 0;
  // 没有点击/展示/订单数据的行(种子词库未验证词)不受阈值卡——本来就没数据支撑, 阈值无意义。
  if (r.clicks == null && r.impressions == null && r.purchases7d == null) return true;
  return (r.clicks == null || r.clicks >= minClicks) &&
         (r.impressions == null || r.impressions >= minImpr) &&
         (r.purchases7d == null || r.purchases7d >= minOrders);
}

// ---------------- 按类目的重叠关系分布矩阵图 (只统计"重叠竞争"行, 跟阈值联动) ----------------
let matrixChart = null;
function renderMatrix() {
  const rows = DATA.unifiedTable.filter(r => r['类型'] === '重叠竞争' && passesThreshold(r));
  const byCat = {};
  rows.forEach(r => {
    const cat = r['类目'] || 'unknown';
    const g = byCat[cat] || (byCat[cat] = {});
    g[r['角色']] = (g[r['角色']] || 0) + 1;
  });
  const bucketCounts = {};
  Object.entries(byCat).forEach(([cat, roles]) => {
    const g = bucketCounts[cat] || (bucketCounts[cat] = Object.fromEntries(DATA.buckets.map(b => [b, 0])));
    Object.entries(roles).forEach(([role, n]) => {
      const b = bucketOf(role);
      if (DATA.buckets.includes(b)) g[b] += n;
    });
  });
  const cats = Object.keys(bucketCounts).sort();
  const labels = cats;
  const datasets = DATA.buckets.map(b => ({
    label: b, data: cats.map(c => bucketCounts[c][b]), backgroundColor: COLORS[b],
  }));
  if (matrixChart) {
    matrixChart.data.labels = labels;
    matrixChart.data.datasets = datasets;
    matrixChart.update();
  } else {
    matrixChart = new Chart(document.getElementById('chart-matrix'), {
      type: 'bar',
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top' } },
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
      },
    });
  }
}

// ---------------- 重叠明细 + 词库缺口 一览 ----------------
const typeSel = document.getElementById('f-type');
const roleSel = document.getElementById('f-role');
const catSel = document.getElementById('f-cat');
[...new Set(DATA.unifiedTable.map(r => r['类型']))].sort().forEach(v => {
  const o = document.createElement('option'); o.value = v; o.textContent = v; typeSel.appendChild(o);
});
[...new Set(DATA.unifiedTable.map(r => r['角色']))].sort().forEach(v => {
  const o = document.createElement('option'); o.value = v; o.textContent = v; roleSel.appendChild(o);
});
[...new Set(DATA.unifiedTable.map(r => r['类目']))].sort().forEach(v => {
  const o = document.createElement('option'); o.value = v; o.textContent = v; catSel.appendChild(o);
});

const selectedFilterProducts = new Set();
const fProductChecksEl = document.getElementById('f-product-checks');
[...new Set(DATA.unifiedTable.map(r => r['产品码']))].sort().forEach(code => {
  const label = document.createElement('label');
  label.className = 'chip-check';
  label.innerHTML = `<input type="checkbox" value="${esc(code)}"> ${esc(code)}`;
  label.querySelector('input').addEventListener('change', (e) => {
    label.classList.toggle('checked', e.target.checked);
    if (e.target.checked) selectedFilterProducts.add(code); else selectedFilterProducts.delete(code);
    renderUnified();
    renderHeatmap();
  });
  fProductChecksEl.appendChild(label);
});

function renderUnified(){
  const type = typeSel.value, role = roleSel.value, cat = catSel.value;
  const term = document.getElementById('f-term').value.toLowerCase();
  const rows = DATA.unifiedTable.filter(r =>
    (!type || r['类型']===type) && (!role || r['角色']===role) && (!cat || r['类目']===cat) &&
    (selectedFilterProducts.size===0 || selectedFilterProducts.has(r['产品码'])) &&
    (!term || r['词根'].toLowerCase().includes(term)) &&
    passesThreshold(r));
  document.getElementById('row-count').textContent = rows.length;
  const tbody = document.querySelector('#tbl-unified tbody');
  tbody.innerHTML = rows.map(r => `<tr>
    <td>${esc(r['产品码'])}</td><td>${esc(r['类目'])}</td>
    <td class="${typeClass(r['类型'])}">${esc(r['类型'])}</td>
    <td>${esc(r['词根'])}</td>
    <td class="${roleClass(r['角色'])}">${esc(r['角色'])}</td>
    <td>${esc(r['活动'])}</td>
    <td>${fmtNum(r.impressions)}</td><td>${fmtNum(r.clicks)}</td><td>${fmtMoney(r.cost)}</td>
    <td>${fmtNum(r.purchases7d)}</td><td>${fmtPct(r.acos)}</td></tr>`).join('');
}
typeSel.addEventListener('change', () => { renderUnified(); renderHeatmap(); });
roleSel.addEventListener('change', () => { renderUnified(); renderHeatmap(); });
catSel.addEventListener('change', () => { renderUnified(); renderHeatmap(); });
document.getElementById('f-term').addEventListener('input', () => { renderUnified(); renderHeatmap(); });

const ths = document.querySelectorAll('#tbl-unified thead th[data-k]');
let sortKey=null, sortDir=-1;
ths.forEach(th=>{
  th.addEventListener('click', ()=>{
    const k = th.dataset.k;
    sortDir = (sortKey===k) ? -sortDir : -1;
    sortKey = k;
    DATA.unifiedTable.sort((a,b)=>{
      const av=a[k], bv=b[k];
      if(av==null) return 1; if(bv==null) return -1;
      if(typeof av==='string') return sortDir*av.localeCompare(bv);
      return sortDir*(av-bv);
    });
    renderUnified();
  });
});

// ---------------- 词根 × 产品 占用热力图 ----------------
// 横轴=产品(勾选 f-product-checks 精确选列, 不勾选默认用花费最高的12个产品);
// 纵轴=词根(按点击量topN, 可调); 格子颜色=角色/来源色系, 深浅=数值(点击量/订单数)大小。
// 跟"明细表"共用同一套 type/role/类目/term/阈值 筛选, 保持两边数据口径一致。
const DEFAULT_HEAT_PRODUCT_COUNT = 12;

function renderHeatmap() {
  const type = typeSel.value, role = roleSel.value, cat = catSel.value;
  const term = document.getElementById('f-term').value.toLowerCase();
  const topN = Number(document.getElementById('heat-topn').value) || 40;

  let products = selectedFilterProducts.size > 0
    ? [...selectedFilterProducts]
    : DATA.productSummary.slice(0, DEFAULT_HEAT_PRODUCT_COUNT).map(p => p['产品码']);
  products = [...new Set(products)];
  document.getElementById('heat-topn-products-label').textContent = DEFAULT_HEAT_PRODUCT_COUNT;

  const rows = DATA.unifiedTable.filter(r =>
    products.includes(r['产品码']) &&
    (!type || r['类型']===type) && (!role || r['角色']===role) && (!cat || r['类目']===cat) &&
    (!term || r['词根'].toLowerCase().includes(term)) &&
    passesThreshold(r));

  const cellMap = new Map();   // key: 词根 + '' + 产品码
  const termTotalClicks = new Map();
  rows.forEach(r => {
    const key = r['词根'] + '' + r['产品码'];
    const isOverlap = r['类型'] === '重叠竞争';
    const c = cellMap.get(key) || {
      overlapClicks: 0, overlapCost: 0, overlapPurchases: 0, overlapRole: null, overlapMaxClicks: -1,
      gapType: null, gapClicks: 0, gapPurchases: 0,
    };
    if (isOverlap) {
      c.overlapClicks += (r.clicks || 0);
      c.overlapCost += (r.cost || 0);
      c.overlapPurchases += (r.purchases7d || 0);
      if ((r.clicks || 0) >= c.overlapMaxClicks) { c.overlapMaxClicks = r.clicks || 0; c.overlapRole = r['角色']; }
    } else {
      c.gapType = r['类型'];
      c.gapClicks += (r.clicks || 0);
      c.gapPurchases += (r.purchases7d || 0);
    }
    cellMap.set(key, c);
    termTotalClicks.set(r['词根'], (termTotalClicks.get(r['词根']) || 0) + (isOverlap ? (r.clicks || 0) : 0));
  });

  let terms = [...termTotalClicks.keys()];
  terms.sort((a, b) => (termTotalClicks.get(b) - termTotalClicks.get(a)) || a.localeCompare(b));
  terms = terms.slice(0, topN);

  renderHeatmapTable(terms, products, cellMap);
}

function renderHeatmapTable(terms, products, cellMap) {
  const wrap = document.getElementById('tbl-heatmap');
  if (terms.length === 0 || products.length === 0) {
    wrap.innerHTML = '<tr><td style="padding:12px;color:var(--text-secondary)">没有满足当前筛选条件的数据。</td></tr>';
    return;
  }
  let maxOverlapClicks = 1, maxGapPurchases = 1;
  cellMap.forEach(c => {
    if (c.overlapClicks > maxOverlapClicks) maxOverlapClicks = c.overlapClicks;
    if (c.gapPurchases > maxGapPurchases) maxGapPurchases = c.gapPurchases;
  });

  const thead = '<thead><tr><th class="corner">词根 \\ 产品</th>' +
    products.map(p => `<th>${esc(p)}</th>`).join('') + '</tr></thead>';
  const tbody = '<tbody>' + terms.map(t => {
    const cells = products.map(p => {
      const c = cellMap.get(t + '' + p);
      if (!c) return '<td class="heat-cell heat-blank"></td>';
      if (c.overlapRole) {
        const bucket = bucketOf(c.overlapRole);
        const rgb = ROLE_RGB[bucket] || [108, 117, 125];
        const alpha = (0.35 + 0.65 * Math.min(1, c.overlapClicks / maxOverlapClicks)).toFixed(2);
        const title = `${p} / ${t}\n角色: ${c.overlapRole}\nclicks: ${c.overlapClicks}  cost: $${c.overlapCost.toFixed(2)}  purchases7d: ${c.overlapPurchases}`;
        return `<td class="heat-cell" style="background:rgba(${rgb.join(',')},${alpha})" title="${esc(title)}">${c.overlapClicks}</td>`;
      }
      if (c.gapType) {
        const isSeed = c.gapType.includes('种子');
        const rgb = isSeed ? GAP_SEED_RGB : GAP_VERIFIED_RGB;
        const alpha = isSeed ? '0.45' : (0.35 + 0.65 * Math.min(1, c.gapPurchases / maxGapPurchases)).toFixed(2);
        const label = isSeed ? '机会' : String(c.gapPurchases);
        const title = isSeed ? `${p} / ${t}\n种子词库(未验证, 无数据)` :
          `${p} / ${t}\n同类目已验证机会词\n验证来源 clicks: ${c.gapClicks}  purchases7d: ${c.gapPurchases}`;
        return `<td class="heat-cell" style="background:rgba(${rgb.join(',')},${alpha})" title="${esc(title)}">${label}</td>`;
      }
      return '<td class="heat-cell heat-blank"></td>';
    }).join('');
    return `<tr><td class="row-label" title="${esc(t)}">${esc(t)}</td>${cells}</tr>`;
  }).join('') + '</tbody>';

  wrap.innerHTML = thead + tbody;
}

function buildHeatLegend() {
  const legend = document.getElementById('heat-legend');
  const roleItems = [
    "梯度内竞争(正常)", "头部(建议保留)", "建议减少/否定", "跨类目(观察)", "弱势/新品保护",
  ];
  legend.innerHTML = roleItems.map(label =>
    `<span><span class="legend-swatch" style="background:rgb(${ROLE_RGB[label].join(',')})"></span>${esc(label)} (深浅=点击量)</span>`
  ).join('') +
    `<span><span class="legend-swatch" style="background:rgb(${GAP_VERIFIED_RGB.join(',')})"></span>词库缺口-同类目已验证 (深浅=验证来源订单数)</span>` +
    `<span><span class="legend-swatch" style="background:rgb(${GAP_SEED_RGB.join(',')})"></span>词库缺口-种子词库(未验证, 无数据)</span>` +
    `<span><span class="legend-swatch" style="background:#f1f3f5;border:1px solid #dee2e6"></span>空白=跟该产品无关</span>`;
}
document.getElementById('heat-topn').addEventListener('input', renderHeatmap);

renderUnified();
renderMatrix();
buildHeatLegend();
renderHeatmap();

// ---------------- 按产品比较 ----------------
let productCompareChart = null;
const productChecksEl = document.getElementById('product-checks');
DATA.productSummary.forEach(p => {
  const label = document.createElement('label');
  label.className = 'chip-check';
  label.innerHTML = `<input type="checkbox" value="${esc(p['产品码'])}"> ${esc(p['产品码'])} (${esc(p['类目'])}/${esc(p['阶段'])})`;
  label.querySelector('input').addEventListener('change', (e) => {
    label.classList.toggle('checked', e.target.checked);
    renderProductCompare();
  });
  productChecksEl.appendChild(label);
});

function selectedCodes(containerEl) {
  return [...containerEl.querySelectorAll('input:checked')].map(i => i.value);
}

function renderProductCompare() {
  const codes = selectedCodes(productChecksEl);
  const resultEl = document.getElementById('product-compare-result');
  if (codes.length === 0) { resultEl.innerHTML = ''; return; }
  const picked = DATA.productSummary.filter(p => codes.includes(p['产品码']));

  // 1) 整体表现对比图
  if (productCompareChart) productCompareChart.destroy();
  resultEl.innerHTML = `<div style="height:300px;margin-top:14px"><canvas id="chart-product-compare"></canvas></div>
    <div class="shared-note" id="exclusive-note"></div>
    <div class="exclusive-col" id="exclusive-cards"></div>`;
  productCompareChart = new Chart(document.getElementById('chart-product-compare'), {
    type: 'bar',
    data: {
      labels: picked.map(p => p['产品码']),
      datasets: [
        { label: 'clicks', data: picked.map(p => p.clicks), backgroundColor: '#4C72B0CC' },
        { label: 'cost', data: picked.map(p => p.cost), backgroundColor: '#DD8452CC' },
        { label: 'purchases7d', data: picked.map(p => p.purchases7d*10), backgroundColor: '#55A868CC' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top' },
                 tooltip: { callbacks: { afterLabel: (ctx) => {
                   const p = picked[ctx.dataIndex];
                   return `sales7d: $${p.sales7d} | ACOS: ${p.acos!=null?(p.acos*100).toFixed(1)+'%':'-'} | 词根数: ${p['词根数']}` +
                          (ctx.dataset.label==='purchases7d' ? ' (purchases7d已 x10放大方便对比)' : '');
                 } } } },
      scales: { y: { beginAtZero: true } },
    },
  });

  // 2) 共同竞争词 vs 各自独有词 (按词根_key做集合运算)
  if (codes.length < 2) {
    document.getElementById('exclusive-note').textContent = '勾选至少2个产品才能算共同/独有词根。';
    return;
  }
  const stemMaps = codes.map(c => {
    const m = new Map();
    (DATA.productStems[c] || []).forEach(r => m.set(r._key, r));
    return m;
  });
  let sharedCount = 0;
  const cardsEl = document.getElementById('exclusive-cards');
  cardsEl.innerHTML = '';
  codes.forEach((code, idx) => {
    const exclusive = [];
    stemMaps[idx].forEach((row, key) => {
      const inOthers = stemMaps.some((m, j) => j !== idx && m.has(key));
      if (inOthers) { if (idx === 0) sharedCount++; }
      else exclusive.push(row);
    });
    exclusive.sort((a, b) => b.clicks - a.clicks);
    const card = document.createElement('div');
    card.className = 'exclusive-card';
    card.innerHTML = `<h4>${esc(code)} 独有词根 (${exclusive.length} 个, 未被选中的其它产品瓜分)</h4>` +
      exclusive.slice(0, 20).map(r => `<div class="term-row"><span>${esc(r['词根'])}</span>` +
        `<span>clicks ${r.clicks} / 单 ${r.purchases7d}</span></div>`).join('') +
      (exclusive.length === 0 ? '<div class="term-row">(没有——词根全部跟选中的其它产品重叠)</div>' : '');
    cardsEl.appendChild(card);
  });
  document.getElementById('exclusive-note').textContent =
    `选中的 ${codes.length} 个产品里，共有 ${sharedCount} 个词根互相竞争（见上面"按产品比较"图的clicks/cost），` +
    `下面每张卡片是该产品自己独有、没被这几个产品里的其它人瓜分的词根——是 listing/A+ 差异化定位的参考。`;
}

// ---------------- 按词根组比较 ----------------
// 选中状态单独存在 selectedTerms 里, 不靠读DOM——否则搜索框一过滤, 不在当前显示的200条
// 里的勾选项会在重新渲染时丢失(reconstructing "checked" from the rendered DOM only
// sees what's currently rendered)。
const allTerms = [...new Set(DATA.unifiedTable.filter(r => r['类型']==='重叠竞争').map(r => r['词根']))].sort();
const termChecksEl = document.getElementById('term-checks');
const selectedTerms = new Set();
function buildTermChecks(filter) {
  termChecksEl.innerHTML = '';
  allTerms.filter(t => !filter || t.includes(filter)).slice(0, 200).forEach(t => {
    const label = document.createElement('label');
    label.className = 'chip-check';
    const isChecked = selectedTerms.has(t);
    if (isChecked) label.classList.add('checked');
    label.innerHTML = `<input type="checkbox" value="${esc(t)}" ${isChecked ? 'checked' : ''}> ${esc(t)}`;
    label.querySelector('input').addEventListener('change', (e) => {
      label.classList.toggle('checked', e.target.checked);
      if (e.target.checked) selectedTerms.add(t); else selectedTerms.delete(t);
      renderTermCompare();
    });
    termChecksEl.appendChild(label);
  });
}
buildTermChecks('');
document.getElementById('term-search').addEventListener('input', (e) => buildTermChecks(e.target.value.toLowerCase()));

function renderTermCompare() {
  const terms = [...selectedTerms];
  const tbody = document.querySelector('#tbl-term-compare tbody');
  if (terms.length === 0) { tbody.innerHTML = ''; return; }
  const rows = DATA.unifiedTable.filter(r => r['类型']==='重叠竞争' && terms.includes(r['词根']));
  tbody.innerHTML = rows.map(r => `<tr>
    <td>${esc(r['词根'])}</td><td>${esc(r['活动'])}</td><td>${esc(r['产品码'])}</td><td>${esc(r['类目'])}</td>
    <td class="${roleClass(r['角色'])}">${esc(r['角色'])}</td>
    <td>${r.clicks}</td><td>${fmtMoney(r.cost)}</td><td>${r.purchases7d}</td>
    <td>${fmtPct(r.acos)}</td></tr>`).join('');
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("这个脚本由 rules.suggest() 调用, 需要 overlap_rows/gap_rows 数据, 不支持单独运行。")
