# -*- coding: utf-8 -*-
"""
自动商品广告优化 —— 唯一入口。一次只跑一个操作。

    python run.py --list                        有哪些操作
    python run.py suggest                       出全部板块的建议（只读，不改账户）
    python run.py suggest --only 收割            只出收割：只拉搜索词报表，快得多
    python run.py suggest --only BID调整 --product P03
    python run.py execute latest                干跑最新一次运行（不用抄日期）
    python run.py execute --go --only 收割 latest
    python run.py execute --go "2026-10-13 & 1" 或者指名某一次运行
    python run.py dashboard                      不定期跑：重叠分析看板/词库缺口/历史看板
    python run.py <操作> --help                 这个操作自己的参数

规则阈值仍然只在 baseline.yaml / config.yaml 里改；这里只决定"跑哪一步"。
只有带 --go 的 execute 会真正修改广告账户，别的操作一律只读。
产品线重叠/词库缺口/历史看板都不是每天跟 suggest 一起自动生成的——要看最新的，
单独跑一次 python run.py dashboard，想跑多勤随意，跟每日 suggest 互不影响。

多站点（美/英/加/法/西/德……）：任何操作前面加 --site <站点代码>，就会去
sites/<站点代码>/ 下找那个站点自己的 config.yaml / baseline.yaml /
.ads-credentials.env / 产品线.yaml / 产品定位.yaml / 保护词.txt，输出和
data/ 也各自独立，互不影响。不加 --site 时跟以前完全一样，用根目录自己的
那一套（当作默认站点）。

    python run.py --site 英 suggest --only 收割
    python run.py --site 德 execute --go "2026-10-13 & 1"

新增一个站点：在 sites/ 下建一个新文件夹，把根目录(或任意一个现有站点)的
config.yaml / baseline.yaml / 产品线.yaml / 产品定位.yaml / 保护词.txt 复制
过去，改成那个账号自己的 profile_id，再放一份那个账号的 .ads-credentials.env
进去就行——代码本身（run.py/rules.py/...）不用复制，六个站点共用一套。
"""
import os
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))          # so the modules import from any cwd


def _pop_site_flag(argv: list[str]) -> str | None:
    """在 cli.py 看到 argv 之前，先把 --site/-s 摘出来（它不属于任何一个操作
    自己的参数，是所有操作共用的全局开关）。支持 --site 英 / --site=英 两种写法。"""
    out, site, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a in ("--site", "-s"):
            if i + 1 >= len(argv):
                raise SystemExit("--site 后面要跟站点代码，比如 --site 英")
            site = argv[i + 1]
            i += 2
            continue
        if a.startswith("--site="):
            site = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    return site, out


_site, _argv = _pop_site_flag(sys.argv[1:])
sys.argv = [sys.argv[0]] + _argv
if _site:
    site_dir = CODE_DIR / "sites" / _site
    if not (site_dir / "config.yaml").exists():
        raise SystemExit(
            f"[!] 找不到站点「{_site}」：{site_dir / 'config.yaml'} 不存在。\n"
            f"    现有站点: {', '.join(sorted(p.name for p in (CODE_DIR / 'sites').glob('*') if (p / 'config.yaml').exists())) or '(还没有，先在 sites/ 下建一个)'}")
    os.environ["ADS_SITE_DIR"] = str(site_dir.resolve())
    HERE = site_dir.resolve()
else:
    HERE = CODE_DIR

import cli                                 # noqa: E402

ONLY_FLAG = cli.flag("--only", metavar="板块",
                     help="只跑这一个板块：收割 / 加预算 / 减预算 / 否定 / BID调整 / 位置调整")
PRODUCT_FLAG = cli.flag("--product", metavar="CODE",
                        help="只处理这一个产品代码，如 P03 / 101 / NS347")
CAMPAIGN_FLAG = cli.flag("--campaign", metavar="活动ID",
                         help="只执行这一个 campaignId 的行，不管它落在哪个板块文件里"
                              "（比如同时审完某活动的 05 Bid调整 + 06 位置调整，一条命令一起执行）")


# ------------------------------------------------------------------ operations

def _suggest_wizard():
    """裸跑 python run.py suggest（不带 --only/--range/--product、且在交互终端里）时
    先问一遍要跑哪些板块/多长时间跨度/只看哪个产品，回车即用默认值。定时任务等非
    交互场景（stdin 不是 tty）或已经带了任何一个参数都不会走到这里，照旧直接跑。"""
    import rules

    print("=" * 50)
    print("suggest 启动向导（直接回车 = 用默认值；带了 --only/--range/--product 就不会问这个）")
    print("=" * 50)

    print("\n要跑哪些板块？")
    for i, b in enumerate(rules.BOARD_ORDER, 1):
        print(f"  {i}. {b}")
    raw = input("输入编号，可逗号分隔多个，回车 = 全部板块: ").strip()
    only = None
    if raw:
        try:
            only = [rules.BOARD_ORDER[int(s.strip()) - 1] for s in raw.split(",") if s.strip()]
        except (ValueError, IndexError):
            print(f"[!] 没看懂「{raw}」，按全部板块处理。")

    raw = input("\n时间跨度？回车 = 每份报表用 config.yaml 里自己的默认窗口；"
                "也可以输 7d / 30d / YYYY-MM-DD:YYYY-MM-DD: ").strip()
    date_range = raw or None

    raw = input("\n只看一个产品代码？回车 = 全部产品（如 P03 / 160 / NS347）: ").strip()
    product = raw or None

    print()
    return product, date_range, only


def suggest(product=None, date_range=None, only=None):
    """拉数据 → 跑规则 → 出建议表（CSV + Markdown）。对广告账户零写入。

    --only 收割 这种只跑那一个板块的规则，而且只去拉它需要的报表 —— 全量要等
    10~25 分钟，单板块通常只等一两份报表。出完建议去审查 output/<目录>/总览.md，
    再用 run.py execute 执行。裸跑（不带任何参数）且在交互终端里会先弹一个小向导
    问要跑哪些板块/时间跨度/产品，回车用默认值；定时任务或带了参数就不会弹。"""
    import rules
    if product is None and date_range is None and only is None and sys.stdin.isatty():
        product, date_range, only = _suggest_wizard()
    return rules.suggest(product=product, date_range=date_range, only=only)


def execute(target, go=False, only=None, campaign=None):
    """把审查过的建议表落到广告账户。不带 --go 就是干跑，一个写请求都不发。

    target 可以是一个运行目录名（2026-10-13 & 1，整批）、单个板块 CSV
    （01-收割.csv），或 latest/最新——自动取 output/ 下最新一次运行，不用
    每次手动抄日期。加 --only 收割 则在整批里只跑那一个板块。

    加 --campaign <活动ID> 则不分板块，把整批里 campaignId 匹配的行（不管在
    01~06 哪个文件）一起挑出来执行——比如某活动的 05 BID调整 和 06 位置调整
    是联合算出来的，不用先 --only BID调整 再 --only 位置调整 跑两遍，一条命令
    就够。CSV 里的 批准 列要求照旧：没标批准=是的行不会被这个过滤器带出来。"""
    import execute as ex
    return ex.run(target, go=go, only=only, campaign=campaign)


def dashboard(product=None):
    """不定期刷新产品线重叠/词库缺口相关的全部产出：重叠分析看板.html（词根占用热力图）、
    output/产品线重叠.md、output/词库缺口.md、dashboard.html（SQLite 历史趋势/竞品ASIN/
    类目投放表现）。这些都不跟每日 suggest 捆在一起自动生成——按需手动跑这一个命令，
    想跑多勤随意，也可以单独给它排一个更低频的定时任务。"""
    import rules
    return rules.refresh_dashboards(product=product)


def cvr_trend(product=None):
    """不定期跑：转化率下滑横向分析 (IRREG1)。当前7天 vs 紧邻的前一个7天，按
    广告类型(AUTO/BROAD/PT定位) -> 类目 -> 阶段 三层下钻CVR对比，定位拖累整体转化率
    下降最严重的具体分组。只读，不改账户，不跟每日 suggest 捆在一起。
    输出 output/转化率下滑分析.md；原始两期数据滚动存 data/cvr_trend_history/(最近2轮)。"""
    import cvr_trend as ct
    return ct.run(product=product)


def weekly_report(product=None):
    """每周跑一次(REG)：①品牌分析搜索词报告(命中我方ASIN的热搜词) + ②高转化词缺少
    EXACT覆盖(只读建议，不接execute.py) + ③竞品ASIN商品目录定位(候选竞品ASIN，供人工
    判断是否加PT定位) + ④变体转化偏移(同SKU vs 其它SKU成交占比，判断广告挂的颜色/
    尺码是不是该换)。①③需要 sp_api.env(已配置) + asin_map.yaml(我方ASIN->产品码，
    需自行维护；缺失时对应节只会说明原因，不会让整份周报失败)。全程只读，不改账户
    任何设置。输出 output/周报.md；②④节原始数据滚动存 data/weekly_report_history/(最近2轮)。"""
    import weekly_report as wr
    return wr.run(product=product)


def sites_dashboard():
    """跨站点汇总看板：挨个打开 sites/*/data/ads.db(+根目录自己的), 按站点标签拼成
    一份对比表+图(花费/销售额/订单/ACOS, 外加跨站点零单浪费搜索词 Top100)。

    只读，不建任何真正的跨站点数据库——各站点的 ads.db 还是各自独立由各自的
    suggest/dashboard 刷新；这个命令永远看全部站点，跟有没有带 --site 无关。
    缺某个站点的 ads.db(还没跑过 suggest)会跳过并提示，不会报错中止。"""
    import cross_site_dashboard
    cross_site_dashboard.main()
    return 0


def test():
    """离线回归测试（不联网、不读凭据）。改完规则或阈值之后跑它。"""
    import subprocess

    env = dict(os.environ, PYTHONIOENCODING="utf-8")   # 子进程也要能打中文
    env.pop("ADS_SITE_DIR", None)   # 回归测试固定用根目录自己的 config/baseline，跟 --site 无关
    ok = True
    for script in ("tests/regr_test.py", "tests/test_upgrade.py"):
        print(f"\n=== {script} ===", flush=True)
        if subprocess.run([sys.executable, script], cwd=CODE_DIR, env=env).returncode != 0:
            ok = False
    print("\n全部通过。" if ok else "\n有测试失败，见上面的输出。")
    return 0 if ok else 1


OPS = [
    cli.Op("suggest", "出建议：拉数据 → 跑规则 → 板块 CSV（只读，不改账户）",
           suggest, doc=(suggest.__doc__ or ""),
           flags=[PRODUCT_FLAG, ONLY_FLAG,
                  cli.flag("--range", dest="date_range", metavar="RANGE",
                           help="报告时间范围：7d / 30d / Nd / YYYY-MM-DD:YYYY-MM-DD")]),
    cli.Op("execute", "执行建议：干跑校验，或 --go 真正写广告账户",
           execute, doc=(execute.__doc__ or ""),
           flags=[cli.flag("target", metavar="目录名或CSV/latest",
                           help='运行目录名（"2026-10-13 & 1"）、单个板块 CSV，'
                                '或 latest/最新（自动取最新一次运行，不用手动抄日期）'),
                  cli.flag("--go", action="store_true",
                           help="真的写广告账户。不加就只干跑，一个写请求都不发"),
                  ONLY_FLAG, CAMPAIGN_FLAG]),
    cli.Op("dashboard", "不定期跑：重叠分析看板.html + dashboard.html + 产品线重叠/词库缺口.md",
           dashboard, doc=(dashboard.__doc__ or ""),
           flags=[PRODUCT_FLAG]),
    cli.Op("sites-dashboard", "跨站点汇总看板：汇总 sites/ 下各站点的历史数据库做对比",
           sites_dashboard, doc=(sites_dashboard.__doc__ or "")),
    cli.Op("cvr-trend", "不定期跑：转化率下滑横向分析 (广告类型→类目→阶段三层下钻)",
           cvr_trend, doc=(cvr_trend.__doc__ or ""), flags=[PRODUCT_FLAG]),
    cli.Op("weekly-report", "每周跑：①品牌分析搜索词 + ②EXACT覆盖缺口 + ③竞品ASIN定位 + ④变体转化偏移",
           weekly_report, doc=(weekly_report.__doc__ or ""), flags=[PRODUCT_FLAG]),
    cli.Op("test", "离线回归测试（不联网）", test, doc=(test.__doc__ or "")),
]


if __name__ == "__main__":
    sys.exit(cli.main("run.py", "自动商品广告优化。一次只跑一个操作。", OPS))
