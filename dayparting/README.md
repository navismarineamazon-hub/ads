# 时段预算控制（实验性，独立于主流程）

和 `run.py suggest/execute` 完全分开：不读写 `output/`、不碰 `baseline.yaml`、
`execution_ledger.json`。规则在 `schedule.yaml`，执行记录在 `data/ledger.json`。

## 为什么只做到"星期几"，不是"几点"

Amazon Ads 报表接口（`timeUnit`）只支持 `SUMMARY`/`DAILY`，没有小时粒度，所以
`analyze_weekday.py` 的结论只能到"星期几"这一层，是目前能拿到的、统计上站得住
脚的最细颗粒度。真正的"21点之后"这个时间点来自用户在广告后台实时花费曲线上的
观察，不是本系统算出来的——`apply_schedule.py` 的窗口支持任意时钟时间，但具体
几点生效/还原仍要靠人工判断，脚本只负责"到了设定时间该怎么调"。

亚马逊确实记录每笔订单的精确下单时间（卖家后台订单数据，到秒），但那是
Seller Central / SP-API 的订单数据，这套系统目前只接了 Ads Reporting API，没有
接 SP-API 订单或 Amazon Marketing Stream（能给到小时/实时级广告事件的官方
方案，但需要额外的 AWS 基础设施，这里没有做）。如果要把"21点后不转化"坐实到
小时级，需要先做那层集成——目前先用日级数据筛出"值得试"的产品，用实际调整
的效果去间接验证。

## 用法

```bash
# 1. 读60天日度数据，按产品+星期算ACOS，挑出候选 → dayparting/reports/*.md
python dayparting/analyze_weekday.py --days 60

# 2. 干跑：只打印当前时间会命中哪条窗口规则、预算会从多少改成多少
python dayparting/apply_schedule.py

# 3. 确认干跑结果没问题后，真正执行
python dayparting/apply_schedule.py --go
```

`apply_schedule.py` 本身不会"自动在21点触发"——它只回答"现在调用我该做什么"。
要做到真正定时，需要外部定时器反复调用（例如 Windows 任务计划每15分钟跑一次
`python dayparting/apply_schedule.py --go`）。注册这个常驻任务前务必先手动跑
几轮干跑+真实执行确认没问题，本脚本不会自己写系统任务计划。

## 改规则

编辑 `schedule.yaml`，每条窗口独立：产品码、星期（ISO 1-7）、开始/结束时钟时间
（允许跨午夜）、预算倍率。多个产品/多条窗口照着现有格式加就行，互不影响。

## 安全边界

- 默认干跑，`--go` 才写账户，和 `execute.py` 的习惯一致。
- 只调 `update_campaigns`（预算），不动 Bid/关键词/否词。
- 每次只处理 `schedule.yaml` 里显式列出的产品码，不会误伤其它活动。
- 写入前实时拉活动状态；非 ENABLED 的活动不会被写（见 `by_product` 过滤逻辑
  ——`stateFilter: ENABLED` 已经排除停用活动）。
- 失败的条目不会写入账本，下次重跑会按"还没处理"重试，不会出现"以为已还原
  但其实没还原"的状态漂移。
