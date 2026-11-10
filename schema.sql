-- Phase 1 数据管道 schema。CREATE TABLE IF NOT EXISTS，重复执行安全。
-- 列名用蛇形英文；中文名只留在 config.yaml 的报表.名称 里当人读标签。

-- 任务队列：替代 data/report_cache/pending.json，支持多 profile、断点续跑。
CREATE TABLE IF NOT EXISTS fetch_tasks (
    task_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id        TEXT NOT NULL,
    spec_name         TEXT NOT NULL,          -- config.yaml 报表.名称，如"搜索词"
    digest            TEXT NOT NULL,          -- 原 pull_reports() 里 sha1[:12]，含义不变
    start_date        TEXT NOT NULL,
    end_date          TEXT NOT NULL,
    report_days       INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/POLLING/DONE/FAILED
    amazon_report_id  TEXT,                   -- create_report() 成功后立刻写入
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (profile_id, spec_name, digest)
);

CREATE INDEX IF NOT EXISTS idx_fetch_tasks_status ON fetch_tasks(status);
CREATE INDEX IF NOT EXISTS idx_fetch_tasks_lookup ON fetch_tasks(profile_id, spec_name, digest);

-- 搜索词报表（对应 config.yaml 报表: 搜索词 / 搜索词历史）
CREATE TABLE IF NOT EXISTS fact_search_term (
    profile_id    TEXT NOT NULL,
    campaign_id   TEXT,
    campaign_name TEXT,
    ad_group_id   TEXT,
    ad_group_name TEXT,
    keyword       TEXT,
    match_type    TEXT,
    search_term   TEXT,
    impressions   INTEGER,
    clicks        INTEGER,
    cost          REAL,
    sales7d       REAL,
    purchases7d   INTEGER,
    report_days   INTEGER NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    digest        TEXT NOT NULL,
    pulled_at     TEXT NOT NULL,
    -- 同一搜索词在同一广告组内可能被多个关键词同时匹配到（各自一行报表数据），
    -- 所以 keyword 必须入 PK；少了它会把这些本应并存的行互相覆盖丢数据。
    PRIMARY KEY (profile_id, campaign_id, ad_group_id, keyword, match_type, search_term, start_date, end_date)
);

-- 广告活动报表（对应 报表: 广告活动 / 广告活动历史）
CREATE TABLE IF NOT EXISTS fact_campaign (
    profile_id             TEXT NOT NULL,
    campaign_id            TEXT,
    campaign_name          TEXT,
    impressions            INTEGER,
    clicks                 INTEGER,
    cost                   REAL,
    sales7d                REAL,
    purchases7d            INTEGER,
    campaign_budget_amount REAL,
    campaign_status        TEXT,
    report_days            INTEGER NOT NULL,
    start_date             TEXT NOT NULL,
    end_date               TEXT NOT NULL,
    digest                 TEXT NOT NULL,
    pulled_at              TEXT NOT NULL,
    PRIMARY KEY (profile_id, campaign_id, start_date, end_date)
);

-- 投放报表（对应 报表: AUTO投放/AUTO投放历史/BROAD投放/BROAD投放历史 —
-- 两类共用一张表，用 spec_name 区分来源；AUTO 只填 targeting/keyword_type，
-- BROAD 只填 keyword/match_type，另一侧留 NULL）
CREATE TABLE IF NOT EXISTS fact_targeting (
    profile_id        TEXT NOT NULL,
    spec_name         TEXT NOT NULL,
    campaign_id       TEXT,
    campaign_name     TEXT,
    ad_group_id       TEXT,
    ad_group_name     TEXT,
    keyword_id        TEXT,
    keyword_bid       REAL,
    keyword_type      TEXT,   -- AUTO投放: TARGETING_EXPRESSION / TARGETING_EXPRESSION_PREDEFINED
    targeting         TEXT,   -- AUTO投放: 投放表达式
    keyword           TEXT,   -- BROAD投放: 关键词文本
    match_type        TEXT,   -- BROAD投放: BROAD
    ad_keyword_status TEXT,
    impressions       INTEGER,
    clicks             INTEGER,
    cost               REAL,
    sales7d            REAL,
    purchases7d        INTEGER,
    report_days        INTEGER NOT NULL,
    start_date         TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    digest              TEXT NOT NULL,
    pulled_at           TEXT NOT NULL,
    PRIMARY KEY (profile_id, campaign_id, ad_group_id, keyword_id, start_date, end_date)
);

-- 执行账本镜像（对应 data/execution_ledger.json，JSON 仍是唯一真相源，这张表只是镜像，
-- 供历史/多维分析用；FAILED 是 DB 独有状态，JSON 侧确认失败的动作直接从 pending 弹出不留痕迹，
-- 这里趁机多记一笔，不影响任何现有判断逻辑）
CREATE TABLE IF NOT EXISTS action_ledger (
    action_id   TEXT PRIMARY KEY,
    profile_id  TEXT NOT NULL,
    status      TEXT NOT NULL,      -- PENDING / SUCCESS / FAILED
    action_type TEXT,               -- row.动作
    level       TEXT,               -- row.层级
    campaign_id TEXT,
    target_id   TEXT,
    file        TEXT,               -- 来源 CSV 文件名 (仅 SUCCESS 有)
    row_json    TEXT NOT NULL,      -- 完整 row 字典 JSON 序列化, 跟 JSON ledger 的 'row' 字段等价
    event_time  TEXT NOT NULL,      -- 对应 JSON 条目的 time 字段
    synced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_ledger_status ON action_ledger(status);
CREATE INDEX IF NOT EXISTS idx_action_ledger_lookup
    ON action_ledger(action_type, level, campaign_id, target_id);

-- 位置表现报表（对应 报表: 位置表现 / 位置表现历史）
CREATE TABLE IF NOT EXISTS fact_placement (
    profile_id    TEXT NOT NULL,
    campaign_id   TEXT,
    campaign_name TEXT,
    placement     TEXT,
    impressions   INTEGER,
    clicks        INTEGER,
    cost          REAL,
    sales7d       REAL,
    purchases7d   INTEGER,
    report_days   INTEGER NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    digest        TEXT NOT NULL,
    pulled_at     TEXT NOT NULL,
    PRIMARY KEY (profile_id, campaign_id, placement, start_date, end_date)
);
