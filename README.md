# 自动商品广告优化系统 — 操作手册

> 适用版本：pro1-update（2026-11 精简版）
> 功能范围：SP 自动广告 → 规则引擎出建议（每日 01~06 六个板块）→ 人工批准 → 一键执行；
> 产品线重叠/词库缺口/历史看板另外不定期手动刷新，跟每日建议各自独立
> **唯一入口**：`python run.py --list` 列出所有操作，一次只跑一个
> **只读模式**：除了带 `--go` 的 `run.py execute`，所有操作对广告账户零写入

---

## 目录

1. [文件结构](#1-文件结构)
2. [一次性配置：凭据与环境](#2-一次性配置凭据与环境)
3. [可调参数速查表](#3-可调参数速查表)
4. [报表时间窗口设置](#4-报表时间窗口设置)
5. [输出目录与保留机制](#5-输出目录与保留机制)
6. [日常流程](#6-日常流程)
7. [报表字段说明](#7-报表字段说明)
8. [规则写法参考](#8-规则写法参考)
9. [安全机制与边界](#9-安全机制与边界)
10. [数据缓存说明](#10-数据缓存说明)
11. [常见问题](#11-常见问题)

---

## 1 文件结构

```
pro1-update/
├── .ads-credentials.env     Ads API 凭据（CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN，必填）
├── sp_api.env               SP-API 凭据（weekly-report 的①③两节用，见 2 节/6末尾）
├── asin_map.yaml            我方ASIN→产品码（weekly-report 的①③两节用，需手动维护）
├── config.yaml              报表配置 / 规则条件 / 安全限制（改规则只动它）
├── baseline.yaml            阈值基线（default + 按产品代码覆盖）
├── 产品线.yaml              马甲活动代码 → 产品线映射（同产品线同词竞价梯度，见 6.4）
├── 产品定位.yaml            产品代码 → 类目/阶段/价格梯度组（重叠检测/词库缺口用）
├── 否定词组.yaml            类目词/尺码词表（"类目尺码不符-否定"规则用，见 8 节）
├── 保护词.txt               品牌词/战略词，命中不生成建议
├── run.py                   【唯一入口】python run.py --list 看有哪些操作
├── cli.py                   入口约定：--list / 一次一个操作 / 每个操作自己的 --help
├── rules.py                 拉数据 → 跑规则 → 生成建议目录（只读，不写账户）
├── execute.py               干跑校验 / --go 正式执行
├── optimization.py          收割竞价、证据检查、历史反馈、输出保留等核心逻辑
├── overlap_dashboard.py     产品线重叠/词库缺口的持久化 HTML 看板生成器（不定期跑，见6）
├── dashboard.py             SQLite 历史趋势 + 竞品ASIN/类目投放表现看板（同上，不定期跑）
├── cross_site_dashboard.py  跨站点汇总看板（run.py sites-dashboard）
├── cvr_trend.py             转化率下滑横向分析（run.py cvr-trend，不定期跑，见8节最后一段）
├── weekly_report.py         周报：①品牌分析搜索词 ②EXACT覆盖缺口 ③竞品ASIN定位（run.py
│                             weekly-report，每周跑，见6节最后一段）
├── sp_api_client.py         SP-API 客户端（Catalog Items API + Reports API/品牌分析），
│                             只给 weekly_report.py 的①③两节用，跟 ads_api.py 彻底分开
├── pipeline.py / db.py      Phase 1 数据管道：报表落一份到 SQLite，供上面两个看板查询
├── ads_api.py                Amazon Ads API 客户端（token 缓存、报表异步、批量执行）
├── requirements.txt         Python 依赖
├── data/
│   ├── 手动映射.json        手动指定 AUTO→BROAD 活动映射（优先于自动映射）
│   ├── execution_ledger.json 执行账本（成功/pending/冷却，也是历史反馈的数据来源）
│   ├── cvr_trend_history/   cvr-trend 两期原始campaign级数据，滚动保留最近2轮
│   ├── weekly_report_history/ weekly-report ②节原始搜索词数据，滚动保留最近2轮
│   └── report_cache/        报表缓存（20h 内复用，免重等待）
├── output/                  每次运行生成 yyyy-mm-dd & N/ 目录
├── sites/                   多站点：每个子目录一个账号自己的 config/baseline/凭据/
│                             产品配置，见 sites/README.md；不加 --site 用根目录这套
└── tests/                   离线回归测试（不联网）
```

多站点（美/英/加/法/西/德……同账号体系下的多个 Amazon Ads profile）：所有操作前面
加 `--site 站点代码`，就会整套换成 `sites/<站点代码>/` 下那个账号自己的配置/凭据/
产品映射/输出/数据，互相完全隔离；不加就是根目录自己这套（默认站点）。新增站点、
需要填什么，看 `sites/README.md`。

```bash
python run.py --site 英 suggest --only 收割
python run.py --site 德 execute --go "2026-10-13 & 1"
```

---

## 2 一次性配置：凭据与环境

```bash
python -m pip install -r requirements.txt
```

**填写 API 凭据**（`.ads-credentials.env`，三个值都要填，不加引号）：

```env
CLIENT_ID=amzn1.application-oa2-client.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CLIENT_SECRET=amzn1.oa2-cs.v1.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
REFRESH_TOKEN=Atzr|xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

从 [Amazon Advertising Console](https://advertising.amazon.com) → 账户 → Manage API access →
Security Profile 获取 Client ID/Secret，LWA 授权流程获取 Refresh Token（不会自动过期，
被撤销后需重新授权）。

**配置 Profile ID**（`config.yaml` 第一行，广告账户 URL 里能找到）：

```yaml
profile_id: '3869405584605651'
```

**（可选）SP-API 凭据** —— 只给 `weekly-report` 的①品牌分析搜索词 / ③竞品ASIN定位两节用，
不配置这两节就只在 `output/周报.md` 里显示"未配置"，不影响②节或其它任何命令：

```env
# sp_api.env
LWA_CLIENT_ID=amzn1.application-oa2-client.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LWA_CLIENT_SECRET=amzn1.oa2-cs.v1.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SP_API_REFRESH_TOKEN=Atzr|xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SP_API_REFRESH_TOKEN_EU=Atzr|xxxx...   # DE/FR/ES/UK 站点用；美/加用上面那个
```

这是跟 `.ads-credentials.env`（Ads API）完全独立的另一个 LWA 应用/另一套凭据（SP-API，
`sellingpartnerapi-*.amazon.com`），从 Seller Central → Apps & Services → Develop Apps 走
LWA 授权流程拿。还需要填 `asin_map.yaml`（我方ASIN → 产品码，手动维护，见6节最后一段
的说明）这两节才会真正出数据。

---

## 3 可调参数速查表

所有阈值集中在 **`baseline.yaml`**。`default` 段是全局默认值，同文件加一段跟 `default`
平级的产品代码段可以只覆盖该产品（见 3.1）。

```yaml
default:
  # 核心盈利约束
  target_acos: 0.30              # 目标 ACOS（按利润率设置）
  target_cpa: 15.0                # 可接受的每单广告花费上限（美元）

  # 否定词触发阈值
  no_order_spend_floor: 15.0      # 零单触发否定的最低花费，与 target_cpa 取较大值
  high_spend_cpa_multiplier: 2.0  # 高花费零单门槛 = target_cpa × 本值
  no_order_click_min: 10          # 零单触发的最低点击数
  expected_cvr: 0.08               # 预期转化率（算零单点击门槛用）
  no_order_probability: 0.10      # 统计置信度，不建议改

  # 收割词阈值与竞价权重（三项权重之和应=1.0）
  harvest_min_orders: 1           # 触发收割的最低订单数（批准默认=否）
  min_winner_orders: 2            # ≥本值才默认批准=是
  harvest_acos_multiplier: 1.2    # 允许收割的 ACOS 上限 = target_acos × 本值
  harvest_cpc_factor: 0.85
  harvest_own_weight: 0.60
  harvest_similar_weight: 0.25
  harvest_recommendation_weight: 0.15

  # AUTO 目标 Bid 调整
  bid_increase_cap: 0.20          # 单次最大加/降价比例
  bid_decrease_cap: 0.20
  bid_min: 0.20                    # 竞价绝对范围（美元）
  bid_max: 20.0
  bid_min_clicks: 15               # 调价需要的最低点击数
  bid_response_strength: 0.50     # ACOS 偏离对调价比例的响应强度（0~1）
  bid_cooldown_days: 3            # 成功执行 BID/预算后的冷却天数

  # BROAD 关键词 Bid 调整（已上线关键词，区别于"收割"只给初始 Bid）
  broad_bid_min_clicks: 20
  broad_bid_response_strength: 0.35
  broad_joint_weight: 0.30        # 与位置竞价联合优化的权重（0=只看自身ACOS）
  history_feedback_lookback_days: 10  # 回看最近几天内上一次同方向调整的效果
  history_feedback_boost: 1.25    # 上次调整后变好 → 本次幅度 ×boost
  history_feedback_damp: 0.50     # 变差 → ×damp
  product_line_gradient_step: 0.15 # 产品线梯度：非头部活动Bid上限逐级打折

  # 产品线重叠检测（见 6.4 后一节）
  product_line_overlap_min_clicks: 5  # 看板默认阈值，前端滑杆可自主调

  # 预算调整
  budget_utilization: 0.90        # 日花费≥预算×本值时触发加预算
  budget_increase_cap: 0.20
  budget_decrease_cap: 0.20
  budget_min: 1.0
```

### 3.1 按产品代码覆盖参数

产品代码从活动名第一段提取（`P03-AUTO-260613` → `P03`，`03-AU-0924` → `03`）。数字
代码需加引号：

```yaml
# 追加在 default 段之后，只写需要覆盖的字段，未覆盖的自动继承 default

P03:
  target_acos: 0.25
  target_cpa: 12
  bid_max: 2.50

"03":
  target_acos: 0.28
  target_cpa: 10
```

---

## 4 报表时间窗口设置

在 `config.yaml` 的 `报表` 段修改数据窗口。**主窗口**（7天）和**历史确认窗口**（30天）
需配套修改：

```yaml
报表:
  - 名称: 搜索词
    报告类型: spSearchTerm
    天数: 7                 # 报表拉取的日期范围
    归因等待天数: 7         # 排除最近 N 天点击，等转化归因完成
    仅自动广告: true

  - 名称: 搜索词历史
    报告类型: spSearchTerm
    天数: 30                # 历史确认窗口，与主窗口结束日期相同，确认该词是否曾出单
    归因等待天数: 7
    仅自动广告: true
```

- `天数: 7` + `归因等待天数: 7` → 实际取 **8~14 天前**的 7 天数据
- `sales7d`/`purchases7d` 字段名里的数字是 Amazon API 固定的归因窗口，不受 `天数:` 影响
- 缩短归因等待会包含更近的数据，但可能漏计尚未归因的转化

---

## 5 输出目录与保留机制

每次运行在 `output/` 下生成 **`yyyy-mm-dd & N`**（当日第 N 次运行）目录：

```
output/
  2026-10-13 & 1/
    总览.md          板块索引 + 跳过明细 + 观察名单 + 执行命令
    01-收割.csv/.md  02-加预算.csv  03-减预算.csv  04-否定.csv
    05-BID调整.csv   06-位置调整.csv
    run_complete.json 完成标记（用于保留判断）
  2026-10-13 & 2/    同天第二次运行
output/产品线重叠.md     只读报告，不在轮次目录里、不会被清理，不随 suggest 自动生成，
                          要靠 python run.py dashboard 单独刷新（见 6 节最后几段）
output/词库缺口.md       同上
output/重叠分析看板.html  同上
output/dashboard.html    同上
output/转化率下滑分析.md  只读报告，要靠 python run.py cvr-trend 单独刷新（见 6 节最后一段）
output/周报.md            只读报告，要靠 python run.py weekly-report 单独刷新（每次覆盖，
                          见 6 节最后一段）
```

这四份都是 `python run.py dashboard` 一次性刷新的，跟每天跑的 `suggest` 完全独立——
suggest 只管 01~06 这六个板块文件，不碰这四份。`转化率下滑分析.md`/`周报.md` 分别由独立的
`python run.py cvr-trend`/`python run.py weekly-report` 刷新，跟 `dashboard`/`suggest` 都无关。

**保留轮数**（跑完第 N+1 轮后自动删除最旧的一轮，未完成的目录不计入）：

```yaml
# config.yaml
输出:
  保留轮数: 2   # 最小值 2
```

---

## 6 日常流程

```bash
python run.py --list      # 看有哪些操作

# 第一步：生成建议（10~25分钟等Amazon报表，20h内缓存秒回；不带任何参数会先弹一个
#         小向导问"跑哪些板块/时间跨度/产品"，回车即默认值；定时任务/已带参数不会弹）
python run.py suggest

# 第二步：审查 output/yyyy-mm-dd & N/总览.md
#   删掉不要执行的行，或把"批准"列改成"否"
#   高花费零单否词 / 仅一单收割词 默认批准=否，需手动确认

# 第三步a：干跑校验（不发任何写请求）
python run.py execute latest        # 自动取最新一次运行，不用手动抄日期

# 第三步b：正式执行（整目录或逐板块）
python run.py execute --go latest
python run.py execute --go --only 收割 latest
python run.py execute --go "2026-11-02 & 1"   # 也可以指名某一次历史运行(目录名含空格要加引号)
```

### 6.1 只生成某一个板块（强烈推荐）

`--only <板块>` 只跑那个板块的规则，**也只去拉它需要的报表**——没选中的报表不会去
亚马逊排队，省下大部分等待时间：

| 命令                      | 跑几条规则 | 拉几份报表 |
|---------------------------|------------|------------|
| `suggest`（全量）         | 14         | 10         |
| `suggest --only 收割`     | 1          | 2          |
| `suggest --only 否定`     | 1          | 2          |
| `suggest --only BID调整`  | 6          | 4          |
| `suggest --only 加预算`   | 2          | 2          |
| `suggest --only 位置调整` | 3          | 2          |

板块名写一段也认（`--only BID` 命中 BID调整，`--only 预算` 同时命中加/减预算）：

```bash
python run.py suggest --only BID调整 --product P03   # 只出某产品的调价
python run.py suggest --only 否定 --range 14d         # 只出否词，窗口改14天
```

只出的板块会记进 `run_complete.json` 的 `boards` 字段，`总览.md` 顶部也会标明，
以免把单板块一轮当成全量看。

### 6.2 不出建议也能单独跑的操作

| 命令              | 要联网吗 | 什么时候用                                                        |
|-------------------|----------|---------------------------------------------------------------------|
| `dashboard`       | 要       | 不定期刷新：重叠分析看板.html、产品线重叠.md、词库缺口.md、dashboard.html（不跑规则、不出建议，见本节最后几段） |
| `sites-dashboard` | 不用     | 汇总 sites/ 下各站点的历史数据库做对比，跟 --site 无关，永远看全部站点 |
| `cvr-trend`       | 要       | 不定期刷新：转化率下滑横向分析（见6节最后一段）                      |
| `weekly-report`   | 要       | 每周跑：①品牌分析搜索词 ②EXACT覆盖缺口 ③竞品ASIN定位（见6节最后一段） |
| `test`            | 不用     | 改完规则/阈值后的离线回归                                            |

`suggest` 不会顺手生成这四份东西——以前每次跑完都会重算一遍，拖慢/拖长了默认每日
跑的那次。日常只看 01~06 板块建议不用等它；想看最新的重叠/缺口/历史数据，不定期
单独跑一次 `dashboard` 就行，想跑多勤随意，跟 `suggest` 的调度完全独立。

### 6.3 PyCharm 快速运行

`.run/` 下已配好每个操作，Run 下拉框直接选（00 List / 01 Suggest全量 / 02 Suggest收割 /
06 Test / 07 Suggest-BID）。要跑配置之外的组合（比如 `dashboard`/`execute`），改某个
配置的 *Parameters* 或直接用终端。常用操作可在 `Settings → Keymap` 搜配置名绑快捷键。

### 6.4 BROAD 关键词竞价调整

`收割` 只给新收割的 BROAD 关键词一个初始 Bid，之后不再动；这里是已上线 BROAD 关键词的
持续调价（`05-BID调整.csv`，`层级` 列区分 `AUTO_TARGET`/`KEYWORD`），复用 AUTO Bid
那套比例/封顶/置信逻辑，并叠加三层：

1. **联合位置优化**——关键词 Bid 中位数和位置表现报表一起算联合最优解，按
   `broad_joint_weight` 加权，只放大/收缩幅度、不反转方向。
2. **历史反馈**——同一关键词上次同方向调整后 ACOS 误差是缩小还是扩大，决定这次
   ×`history_feedback_boost` 放大或 ×`history_feedback_damp` 收缩（看
   `execution_ledger.json`，见 10 节；新关键词无历史不受影响）。目前只接在这里，AUTO
   目标Bid/否定/收割/位置调整/预算调整都还没接这个反馈环。
3. **产品线梯度**——同一产品挂多个马甲活动代码（`产品线.yaml` 登记）且同时投同一个
   BROAD 词时，非头部活动的 Bid 被压出梯度上限，行标 `规则名=产品线梯度`。

```bash
python run.py suggest --only BID调整              # AUTO target + BROAD 关键词调价
python run.py suggest --only BID调整 --product 105  # 只看某产品（带出同产品线梯度行）
```

首次使用需确认 `产品线.yaml` 马甲映射齐全（没登记的代码各自独立）。

### 产品线关键词重叠检测（只读报告，不是执行板块）

`产品线重叠.md`：同一**真实搜索词**（按词根归并，忽略虚词和单复数/进行时变形，不是语言
学精确的词干提取）被同产品线下多个马甲活动同时抓到时，说明两个 listing 在抢同一次
曝光。数据来源 `搜索词历史`（30天，`仅自动广告: false`，覆盖 AUTO+BROAD+手动）。

按 `产品定位.yaml`（产品代码→类目/阶段/价格梯度组）分三种性质处理：

1. **同一价格梯度组**（如03/05/07）：故意的价格分层，标"梯度内竞争(正常)"，不建议动。
2. **同类目不同梯度组**：真内耗，按订单优先/ACOS次之排头部（保留）和其余（建议减少/否定）。
3. **不同类目**（如sailing蹭workwear的词）：标"跨类目(观察)"，不建议切断，需人工核算
   该词在该类目自己的转化质量。

阶段=`weak`/`new` 的产品不管落哪种情况都不建议减少——缺的是词，去看 `词库缺口.md`。
不随 `suggest` 生成，单独跑 `python run.py dashboard` 才会重新拉 `搜索词历史` 重新算、
重新覆盖 `output/产品线重叠.md`（跟 `suggest` 跑不跑、跑哪个板块完全无关）。

### 词库缺口 / 新词挖掘（面向弱势/新品）

`词库缺口.md` 给阶段=weak/new 的产品找机会词：**同类目已验证**（同类目别的产品已
出单、这个产品没投过，按订单/ACOS排名，每产品最多 `MAX_GAP_PER_PRODUCT` 条，默认20）
和 **种子词库(未验证)**（内置的美区购物习惯候选词 `SEED_VOCAB`，整账户历史都没出现
过才建议，无数据支撑，需人工判断再小范围试投）。同样不随 `suggest` 生成，只靠
`python run.py dashboard` 刷新。

### 重叠分析看板（持久化 HTML，随数据更新）

`output/产品线重叠.md`/`output/词库缺口.md`/`output/重叠分析看板.html` 都放在
`output/` 根目录，不会被"保留轮数"清理，也**不随 `suggest` 自动重写**——单独跑
`python run.py dashboard` 才会重新拉一份 `搜索词历史` 重新算、重新覆盖。重叠分析
看板.html 内容：

- 按类目的重叠关系堆叠柱状图 + 点击量/展示量/订单量阈值滑杆（服务端几乎不过滤，筛选
  在前端自主调，拖动或输数字实时重新统计）。
- **重叠明细 + 词库缺口 一览**：合并成一张表，按产品/类目/角色/词根筛选、按列排序。
- **词根 × 产品 占用热力图**：横轴产品、纵轴词根（按点击量排前N，可调），格子颜色=
  角色/来源、深浅=数据强度，一眼看出哪个词被谁占着、谁独占、谁是机会词。页面里嵌了
  详细的"怎么看/对应什么操作"说明（默认展开），不在此重复。
- **按产品比较表现**：勾选几个产品看整体对比图 + 各自独有词根（listing/A+差异化参考）。
- **按词根组比较**：勾选具体词根组，把跨产品明细摆一张表对比。

打开方式跟 `output/dashboard.html` 一样，直接在浏览器里打开本地文件。

### 竞品 ASIN / 类目投放表现（dashboard.html 里的两张表）

`AUTO投放` 报表的 `targeting` 字段里混着 close-match/substitutes 这类常规自动发现，
也混着 `asin=`/`asin-expanded=`(竞品ASIN定向)和 `category=`(Amazon自己判定的类目定向)。
`python run.py dashboard` 会把这两类单独摘出来汇总成表：

- **竞品ASIN投放表现**：产品代码/竞品ASIN/点击/花费/销售额/订单/ACOS，按花费排序——
  花了钱没转化的是否定候选，ACOS低订单稳的可以考虑单独建PT活动加码。
- **类目投放表现**：产品代码/Amazon判定类目/花费/订单/ACOS——如果跟产品实际定位明显
  不搭(比如workwear产品大量落在hiking/outdoor类目)，是listing类目/关键词可能设置
  错了的信号，值得去Amazon后台核对。

### 转化率下滑横向分析（IRREG1，`python run.py cvr-trend`）

账户整体转化率下降时，光看一个总数猜不出是哪里拖累的（常见原因之一是新品变多，
新品本身CVR天然偏低，会拉低总体均值，但不代表老品真的变差）。`cvr-trend` 现查
**当前7天 vs 紧邻的前一个7天**（两个不重叠的窗口，不是现有报表体系里那种首尾相叠的
7d/14d"历史"口径）两期 campaign级 `spCampaigns` 数据，按：

1. **广告类型**：AUTO / BROAD及其它手动关键词活动 / PT(商品定位)——判定规则跟
   `rules.pt_campaign_index`(见 8 节"竞品ASIN/类目定位收割")同一套"名字含
   POSITION/ASIN即算定位活动"，不是另外发明一套。
2. **类目** (`产品定位.yaml` 的 `类目`字段，workwear/fishing/sailing/...)
3. **阶段** (`产品定位.yaml` 的 `阶段`字段，mature/weak/new/unknown——新品的CVR偏低
   如果是这个字段驱动的，看这一列就知道"降CVR是不是新品拉的")

三层下钻汇总每组的 CVR，按"流失订单估计"(`当前期clicks × (对比期CVR − 当前期CVR)`，
正数=这组CVR下降在吃订单，负数=这组反而在涨，不是下滑来源)降序排列，写一份
`output/转化率下滑分析.md`（每次跑直接覆盖，不按轮次保留）。

跟其它"不定期手动跑"的报告（`dashboard`）不一样的是缓存策略：两期原始 campaign级
数据会存进 `data/cvr_trend_history/<时间戳>.json`，
跟每日 `suggest` 的 `output/` 保留轮数清理逻辑完全分开，只滚动保留最近 2 轮——这是给
以后想回看"上几次分析用的原始数据"用的，报告本身(`output/转化率下滑分析.md`)永远是
覆盖式的最新一份。

但 AUTO投放 报表本身只拉"仅自动广告"的活动(`仅自动广告: true`)，独立的手动"商品
定位"(Product Targeting)活动——比如账户里已经在跑的 `W319-ASIN-0402`、
`03/05-ASIN-position-0817` 这种——不会被它拉到。所以 `config.yaml` 里加了一份
`商品定位投放` 报表(同样的 targeting 维度/列/过滤器，但 `仅自动广告: false`)专门
补这块；`dashboard` 命令会在存在这份配置时一起拉，两份报表按 `keywordId` 去重，
不会把同一条定向算两次。没配这份报表(比如新站点还没加)时会打印提示并跳过，两张表
只看得到 AUTO投放 自己带出来的那部分。

### 周报（REG，`python run.py weekly-report`）

每周跑一次，产出合并成一份 `output/周报.md`（每次覆盖），只读、不接 `execute.py`。
三节按阅读顺序排版：

1. **① 品牌分析搜索词报告**——Brand Analytics `GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT`
   (SP-API Reports API，按日历周 WEEK 对齐)，拉回整个市场的热搜词，本地按 `asin_map.yaml`
   过滤出命中我方ASIN的行（请求层面不支持按ASIN过滤，只能下载后过滤）。
2. **② 高转化词缺少EXACT覆盖**——账户目前完全没有 EXACT 收割（只有 BROAD/PT 收割），
   这节用跟 `出单词-收割BROAD` 同一套阈值找出"转化好但从没建过 EXACT 活动"的词，只读
   建议、不自动建活动，人工判断要不要单独开 EXACT。报表口径特意不带"仅自动广告"限制
   （克隆 config.yaml 的「搜索词」规格，不改原规格），覆盖 BROAD/PT 来源的转化词。
3. **③ 竞品ASIN商品目录定位**——SP-API Catalog Items API，按我方ASIN的标题关键词+类目
   搜出候选竞品ASIN，供人工判断是否值得加商品定位(PT)，不自动建定向。

①③需要 2 节开头配置的 `sp_api.env` + `asin_map.yaml`（我方ASIN→产品码，格式见
`asin_map.yaml` 文件里的注释）；缺哪个就只在对应节写清楚原因，不会让②节或整份报告跑不出来。
②节原始搜索词数据存进 `data/weekly_report_history/<时间戳>.json`，跟 `cvr_trend_history/`
同一套滚动保留最近 2 轮的写法，跟每日 `suggest` 的 `output/` 保留逻辑完全分开。

```bash
python run.py weekly-report                  # 全部产品
python run.py weekly-report --product NW007  # 只看一个产品
```

定时任务：`scheduled_weekly_report.bat`，跟 `scheduled_suggest.bat`/`scheduled_dashboard.bat`
同构，在 Windows 任务计划程序里新建一个触发器类型选"每周"的任务指向它即可（那两个
每日任务不受影响）。

---

## 7 报表字段说明

### 7.1 当前使用的列（搜索词报表）

`campaignId` `campaignName`（产品码从这里提取）`adGroupId` `adGroupName` `keyword`
`matchType`（BROAD/PHRASE/EXACT）`searchTerm`（买家实际搜索词）`impressions` `clicks`
`cost` `sales7d` `purchases7d`

### 7.2 可选追加的列

在 `config.yaml` 对应报表的 `列:` 块追加即可，不影响现有规则：

`keywordId`（修改/暂停该词需要）`keywordBid`（当前出价）`sales14d`/`purchases14d`
（转化周期长的品类更准）`sales30d`/`purchases30d`（慢决策类目）`unitsSoldClicks7d`
（捆绑包/多件装）`roas7d`（= sales7d/cost）

**推荐**：决策周期 > 7天的产品追加 `sales14d`+`purchases14d`，规则条件里用
`purchases14d` 替代 `purchases7d` 更准。

### 7.3 时间设置与归因窗口的关系

两个维度相互独立：`天数: 7` 控制报表拉取的日期范围；`归因等待天数: 7` 排除最近N天
等归因完成；`sales7d` 字段后缀是 Amazon 固定的归因窗口，不可修改。`天数:30` +
`sales7d` = 拉30天点击各自看7天转化（数据量更多）；`天数:7` + `purchases14d` = 拉7天
点击、归因窗口扩到14天（捕捉慢转化）。

---

## 8 规则写法参考

规则在 `config.yaml` 的 `规则` 段定义，条件是可读的 Python 表达式：

```yaml
规则:
  - 名称: 出单词-收割BROAD
    数据: 搜索词
    条件: >
      purchases7d >= harvest_min_orders
      and acos7d <= target_acos * harvest_acos_multiplier
      and not 保护词
    动作: 收割
    置信: "high if purchases7d >= min_winner_orders else 'medium'"

  - 名称: 零单浪费-否定精确
    数据: 搜索词
    条件: >
      purchases7d == 0
      and (cost >= no_order_spend_threshold and clicks >= no_order_click_threshold
           or cost >= target_cpa * high_spend_cpa_multiplier)
      and not 保护词
    动作: 否定
    置信: needs_human          # 批准列默认=否，强制人工确认
```

**可用变量**：报表指标列（`cost` `clicks` `impressions` `sales7d` `purchases7d`）、
派生列（`acos7d` `cpc7d` `avgDailyCost`）、baseline 变量（`target_acos` `target_cpa`
`harvest_min_orders` 等）、内置（`保护词` bool、`abs` `round` `min` `max`）。

> `05-BID调整.csv` 里 `规则名=产品线梯度` 的行不是从 `规则` 段跑出来的，是
> `expand_product_line_gradient`（见 6.4）在其它规则跑完后额外追加/覆盖的。

**词组否定**（`04-否定.csv` 里 `匹配=NEGATIVE_PHRASE` 的行）：某条 `否定` 规则加
`动作参数: 词组否定` 后，除了对命中的整句 `searchTerm` 做 `NEGATIVE_EXACT`，还会对
`否定词组.yaml`（类目词/尺码词，尺码词可按产品码排除）里实际命中的那个词组本身额外生成
一条 `NEGATIVE_PHRASE`——同一个词换个搭配出现不用再等零单一轮才被否掉。词表改了不用碰
`条件:`。目前只接在"类目尺码不符-否定"这条规则上。

**AUTO↔BROAD 否定同步**：`否定` 只会从 AUTO 的`搜索词`报表触发（BROAD 没有搜索词粒度
的报表），生成的否定（精确+词组）会按 `安全.映射文件`（跟 6.4/收割 复用同一份映射）
自动镜像一条到对应 BROAD 活动的广告组——否则同一批无关词只在 AUTO 侧被否，BROAD 自己
的流量照样会浪费在这些词上。没有映射或广告组待映射时跳过镜像，不影响 AUTO 侧本身的否定。

**竞品ASIN/类目定位收割**（`01-收割.csv` 里 `动作=收割ASIN` 的行，跟 BROAD 关键词收割
并入同一个板块）：`AUTO投放`报表里 `targeting` 字段形如 `asin="B0..."` /
`asin-expanded="B0..."` / `category="..."` 才算竞品ASIN/类目定位（close-match 等常规
AUTO自动发现不算，由 `rules.classify_targeting` 判定），出单达标后收割到对应的 PT(商品
定位) 活动——目标活动不走映射文件，是按活动名识别：`targetingType=MANUAL` 且名字含
`POSITION`/`ASIN` 关键字，取跟来源AUTO同一个产品码的那个（`rules.pt_campaign_index`，
跟 ONE1 批量建PT活动时同一套命名规则）。没有对应PT活动 / 候选活动名字歧义 / 目标广告组
缺失或有多个启用广告组 / PT活动里已存在同一条定向，都跳过不乱猜。判重用的"PT活动当前
定向"是现查（`client.list_targets`），不走本地快照——这些活动数量少、刷新也不频繁，
专门维护一份快照不值得。Bid 算法跟 BROAD 收割同一套利润上限逻辑（`harvest_bid`），只是
没有"相似BROAD词"这个参考维度。

---

## 9 安全机制与边界

| 机制           | 说明                                                  |
|----------------|-------------------------------------------------------|
| `suggest` 只读 | 只调查询和报告接口，不写账户                          |
| 干跑模式       | `execute` 不带 `--go` 只打印预览，不发写请求          |
| 首条试探       | `--go` 执行时先发一条试探；失败整批中止               |
| 证据检查       | 关键指标缺失/历史窗口未出单的操作自动进跳过明细       |
| 冷却账本       | 成功执行的 BID/预算在冷却期内不会再次触发             |
| 执行账本       | pending 状态需人工核实后才能重试                      |
| ASIN 保护      | `安全.忽略ASIN: true` → B0开头商品词不收割、不否定    |
| 单批上限       | `安全.单批上限: 200`（自动模式建议改30）              |
| 保护词清单     | `保护词.txt` 中的词不会被否定或覆盖（品牌词必须加入） |

---

## 10 数据缓存说明

| 文件                                 | 内容                    | 有效期                                                           |
|--------------------------------------|-------------------------|------------------------------------------------------------------|
| `data/report_cache/*.json`           | API 报表结果            | 每次 `suggest`/`dashboard` 重新拉取，20h 内复用免重等待          |
| `data/campaigns_all.json` 等结构快照 | 活动/广告组/AUTO target | `同步.结构缓存小时`（默认6h）                                    |
| `data/keywords_state.json`           | 全状态关键词            | `同步.关键词缓存小时`（默认24h）                                 |
| `data/.token_cache.json`             | Ads API LWA Access Token| 约1小时（自动续期）                                              |
| `data/sp_api_token_cache.json`       | SP-API LWA Access Token | 约1小时（自动续期，跟上面那个是两套独立token）                   |
| `data/weekly_report_history/*.json`  | weekly-report ②节原始搜索词数据 | 滚动保留最近2轮                                        |

```yaml
# config.yaml
同步:
  结构缓存小时: 6     # 改0每次强制刷新
  关键词缓存小时: 24
轮询:
  初始间隔秒: 30      # 等待报表的轮询间隔，自动增长到最大值
  最大间隔秒: 120
  超时秒: 3600        # 单份报表等待上限，超时视为失败
```

---

## 11 常见问题

**Q：运行后 output/ 找不到今天的目录？**
A：规则未命中任何数据行仍会生成目录（只含 `总览.md`/`run_complete.json`），确认
`报表.天数` 和账户数据范围是否一致。

**Q：Refresh Token 失效？**
A：重新走 LWA 授权，更新 `.ads-credentials.env` 的 `REFRESH_TOKEN`。Token 本身不
过期，但密码改变或 Security Profile 被删除时失效。

**Q：收割词进了"跳过明细"，原因是"无对应 BROAD 活动"？**
A：在 `data/手动映射.json` 填该 AUTO 活动对应的 BROAD 活动ID和广告组ID：

```json
{"map": {"你的AUTO活动ID": {"broad": [{"cid": "你的BROAD活动ID", "state": "ENABLED"}],
                          "adGroupIds": ["你的BROAD广告组ID"]}}}
```

**Q：如何只针对一个产品运行？**
A：`--product P03` 就只处理这个产品，也能和 `--only` 一起用。要固化进规则本身，
在条件里加 `产品码 == 'P03'`（`产品码` 是从活动名自动提取的变量）。

---

*最后更新：2026-11-09 | 版本：pro1-update*
