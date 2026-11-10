# 多站点配置

每个子文件夹是一个独立站点（一个 Amazon Ads profile / 一个账号），跟根目录共用同一套
代码（run.py / rules.py / execute.py / ...），互不影响。用法：

    python run.py --site 英 suggest --only 收割
    python run.py --site 德 execute --go "2026-10-13 & 1"

不加 `--site` 时，还是用根目录自己那一套（美站，当作默认站点）。

## 现有站点

- 美：根目录本身（已在跑，无需 --site）
- 英/加/法/西/德：已从根目录复制了 config.yaml / baseline.yaml / 产品线.yaml /
  产品定位.yaml / 保护词.txt 作为起点，**上线前还要做两件事**：
  1. 打开该站点的 `config.yaml`，把 `profile_id` 改成那个账号自己的（当前是占位的
     "请改成-XX-站点自己的-profile_id"，不改会直接报错，不会误跑错账号）。
     非美区站点还要加一行区域端点：`api_base: https://advertising-api-eu.amazon.com`
     （欧洲站）/ `api_base: https://advertising-api-fe.amazon.com`（日本/澳洲站），
     美区（美/加/墨/巴西）不用加，默认就是 advertising-api.amazon.com。
  2. 把那个账号自己申请到的 Amazon Ads API 凭据存成
     `sites/<站点>/.ads-credentials.env`（格式跟根目录的 `.ads-credentials.env`
     一样：CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN）。

如果某个站点卖的产品、类目分级跟美站不一样，还要按需改那个站点自己的
`产品线.yaml` / `产品定位.yaml` / `保护词.txt` / `baseline.yaml` 阈值——这几份
复制过去之后是美站的内容，只是起点，不会自动跟着美站改动同步。

`output/`、`data/`（包括 SQLite 历史库、映射缓存等）每个站点各自独立生成在自己的
文件夹下，第一次跑 suggest 会自动建好，不用手动创建。

## 跨站点汇总看板

`python run.py sites-dashboard` 把所有站点(根目录+sites/下每一个)自己的
`data/ads.db` 挨个只读打开，按站点标签拼成一份对比看板
(`output/跨站点汇总看板.html`)——花费/销售额/订单/ACOS 站点对比图，加一张跨站点
零单浪费搜索词 Top100。不是真的建一个共享数据库，各站点的 ads.db 还是各自独立、
各自的 suggest/dashboard 各自刷新；这个命令永远汇总全部站点，跟有没有带 --site
无关。缺某个站点的 ads.db(还没跑过 suggest)会跳过并提示，不会报错中止。
