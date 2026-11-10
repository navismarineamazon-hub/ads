# -*- coding: utf-8 -*-
"""Amazon Selling Partner API (SP-API) 客户端 —— Catalog Items API + Reports API(品牌分析)。

跟 ads_api.AdsClient(Amazon Ads API, advertising-api.amazon.com) 完全分开: 不同的
LWA 应用、不同的域(sellingpartnerapi-na/-eu.amazon.com)、不同的凭据文件。

凭据: 从 <project>/sp_api.env 读取 LWA_CLIENT_ID / LWA_CLIENT_SECRET /
SP_API_REFRESH_TOKEN (+ 可选 SP_API_REFRESH_TOKEN_EU，给 DE/FR/ES/UK 用，
跟 project/sp-api/shared/spapi.py 用的是同一份凭据文件/同一套 LWA 应用)。
凭据值不打印、不写日志。

认证/请求/报告轮询这套逻辑直接照抄 project/sp-api/shared/spapi.py 里已经在本账户
跑通的 access()/call()/异步报告三步(create→poll→download) ——这是唯一一处"跟同级
sp-api 项目保持一致"的地方，其余(凭据加载方式/token磁盘缓存)跟本项目 ads_api.py
风格保持一致，不跨项目 import。
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import requests

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
HOST = {"na": "sellingpartnerapi-na.amazon.com", "eu": "sellingpartnerapi-eu.amazon.com"}

# 站点代码(跟 run.py --site 用的中文代码一致) -> (region, marketplaceId)。
# marketplaceId 数值取自 project/sp-api/shared/spapi.py 的 MARKETS 表，跟同账户已经
# 跑通的 SP-API 调用用的是同一批数值，不用重新查。法/西两站暂时没有 ASIN 源文件
# (见 weekly_report.py 的 _my_asins())，但市场本身的 marketplaceId 先一并列出。
SITE_MARKET = {
    "美": ("na", "ATVPDKIKX0DER"), None: ("na", "ATVPDKIKX0DER"),
    "加": ("na", "A2EUQ1WTGCTBG2"),
    "德": ("eu", "A1PA6795UKMFR9"),
    "法": ("eu", "A13V1IB3VIYZZH"),
    "西": ("eu", "A1RKKUPIHCS9HS"),
    "英": ("eu", "A1F83G8C2ARO7P"),
}


def site_market(site_code: str | None) -> tuple[str, str]:
    """run.py --site 的中文代码 -> (region, marketplaceId)。不认识的代码报错，
    不默默当成默认站点——免得配错站点却悄悄用了美国市场的数据。"""
    if site_code in SITE_MARKET:
        return SITE_MARKET[site_code]
    raise ValueError(f"sp_api_client 不认识站点代码 {site_code!r}，"
                     f"已知: {sorted(k for k in SITE_MARKET if k)}")


class SpApiCredentialsMissing(RuntimeError):
    """sp_api.env 不存在或缺字段。"""


class NotAuthorized(RuntimeError):
    """这个 SP-API 角色/权限这个账户没开通 (HTTP 403)。带开通步骤提示。"""


def load_sp_credentials(path: str | Path) -> dict:
    """解析 KEY=VALUE 格式的凭据文件，跟 ads_api.load_credentials 同样的写法。"""
    path = Path(path)
    if not path.exists():
        raise SpApiCredentialsMissing(f"SP-API 凭据文件不存在: {path}")
    creds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip()] = v.strip().strip('"').strip("'")
    for required in ("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "SP_API_REFRESH_TOKEN"):
        if not creds.get(required):
            raise SpApiCredentialsMissing(f"SP-API 凭据文件缺少 {required}: {path}")
    return creds


class _TokenManager:
    """LWA access token, 按 region 分开缓存 (eu/na 用不同的 refresh token)。"""

    def __init__(self, creds: dict, cache_file: Path):
        self.creds = creds
        self.cache_file = cache_file
        self._tokens: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            self._tokens = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            self._tokens = {}

    def _save_cache(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self._tokens), encoding="utf-8")
        except Exception:
            pass

    def get(self, region: str) -> str:
        entry = self._tokens.get(region, {})
        if entry.get("access_token") and time.time() < float(entry.get("expiry", 0)) - 300:
            return entry["access_token"]
        refresh_token = (self.creds.get("SP_API_REFRESH_TOKEN_EU") if region == "eu"
                         else self.creds.get("SP_API_REFRESH_TOKEN"))
        if not refresh_token:
            raise SpApiCredentialsMissing(
                f"sp_api.env 缺少 region={region} 对应的 refresh token "
                f"({'SP_API_REFRESH_TOKEN_EU' if region == 'eu' else 'SP_API_REFRESH_TOKEN'})")
        resp = requests.post(LWA_TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.creds["LWA_CLIENT_ID"], "client_secret": self.creds["LWA_CLIENT_SECRET"],
        }, timeout=30)
        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = f"{err.get('error')}: {err.get('error_description')}"
            except Exception:
                msg = resp.text[:300]
            raise RuntimeError(f"SP-API LWA 换 token 失败 HTTP {resp.status_code}: {msg}")
        data = resp.json()
        self._tokens[region] = {"access_token": data["access_token"],
                                "expiry": time.time() + int(data.get("expires_in", 3600))}
        self._save_cache()
        return self._tokens[region]["access_token"]


class SpApiClient:
    def __init__(self, credentials_file: str | Path, cache_dir: Path):
        self.credentials_file = Path(credentials_file)
        self.cache_dir = Path(cache_dir)
        self._creds: dict | None = None
        self._tokens: _TokenManager | None = None

    def _ensure(self) -> None:
        if self._creds is None:
            self._creds = load_sp_credentials(self.credentials_file)
            self._tokens = _TokenManager(self._creds, self.cache_dir / "sp_api_token_cache.json")

    def call(self, region: str, path: str, params: dict | None = None,
             method: str = "GET", body: dict | None = None, tries: int = 7) -> dict:
        """通用请求 + 限流重试，照抄 project/sp-api/shared/spapi.py 里已经跑通的套路，
        唯一区别: 403 包成 NotAuthorized 带开通提示，而不是裸 HTTPError。"""
        self._ensure()
        url = f"https://{HOST[region]}{path}"
        resp = None
        for i in range(tries):
            headers = {"x-amz-access-token": self._tokens.get(region),
                      "user-agent": "pro1-update-weekly-report/1.0",
                      "content-type": "application/json"}
            resp = requests.request(method, url, headers=headers, params=params,
                                     json=body, timeout=90)
            if resp.status_code == 403:
                raise NotAuthorized(
                    f"SP-API {method} {path} 返回 403 (权限/角色不够)。\n"
                    f"去 Seller Central -> Apps & Services -> Develop Apps -> 编辑这个应用的角色，"
                    f"加上对应角色(Catalog: Product Listing; 品牌分析报告: Brand Analytics)，"
                    f"重新授权后把新 refresh token 填进 sp_api.env 再重试。\n原始响应: {resp.text[:300]}")
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(2 ** i * 2, 70))
                continue
            resp.raise_for_status()
            return resp.json() if resp.text else {}
        raise RuntimeError(f"{method} {path} 重试后仍失败: {resp.status_code} {resp.text[:300]}")

    # ------------------------------------------------------------- Catalog Items API

    def get_catalog_item(self, asin: str, marketplace_id: str, region: str,
                         included_data: tuple[str, ...] = ("summaries", "salesRanks")) -> dict:
        """GET /catalog/2022-04-01/items/{asin} —— 取单个 ASIN 的标题/品牌/销售排名等。"""
        return self.call(region, f"/catalog/2022-04-01/items/{asin}",
                         params={"marketplaceIds": marketplace_id,
                                "includedData": ",".join(included_data)})

    def search_catalog_items(self, keywords: list[str], marketplace_id: str, region: str,
                             included_data: tuple[str, ...] = ("summaries", "salesRanks", "identifiers"),
                             page_size: int = 20) -> list[dict]:
        """GET /catalog/2022-04-01/items (searchCatalogItems) —— 按关键词搜目录，
        最多20个关键词(API限制)。返回 items 列表(可能为空)。"""
        params = {"marketplaceIds": marketplace_id, "keywords": ",".join(keywords[:20]),
                  "includedData": ",".join(included_data), "pageSize": page_size}
        j = self.call(region, "/catalog/2022-04-01/items", params=params)
        return j.get("items", [])

    # ------------------------------------------------------------- Reports API (品牌分析)

    _POLL_SECONDS, _POLL_TRIES = 8, 90  # ~12 分钟

    def pull_brand_analytics_search_terms(self, marketplace_id: str, region: str,
                                          week_start: str, week_end: str) -> list[dict]:
        """GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT，reportPeriod=WEEK (week_start必须是
        周日、week_end必须是周六，Amazon对齐要求)。需要 Brand Registry + 品牌分析报告权限，
        没开通会在 call() 里被包成 NotAuthorized。

        这个报告请求层面不支持按ASIN过滤——拉回来是整个市场的热搜词Top3点击/购买ASIN，
        "只看我方ASIN相关的行"要在下载后由调用方(weekly_report.py)过滤，不是这里做。

        实现时未能 100% 确认 reportOptions 是否还需要 distributorView/sellingProgram
        字段(卖家账户文档样例不完全一致)——先只传 reportPeriod，如果 Amazon 返回 400
        提示缺字段，按报错信息补上对应字段值即可，不要凭记忆先加。
        """
        body = {"reportType": "GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT",
                "marketplaceIds": [marketplace_id],
                "dataStartTime": f"{week_start}T00:00:00Z",
                "dataEndTime": f"{week_end}T23:59:59Z",
                "reportOptions": {"reportPeriod": "WEEK"}}
        pending_file = self.cache_dir / f"pending_brand_analytics_{marketplace_id}_{week_start}.json"
        rid = None
        if pending_file.exists():
            try:
                rid = json.loads(pending_file.read_text(encoding="utf-8"))["reportId"]
            except (ValueError, KeyError):
                pending_file.unlink(missing_ok=True)
        if rid:
            st = self.call(region, f"/reports/2021-06-30/reports/{rid}")
            if st.get("processingStatus") in ("CANCELLED", "FATAL"):
                pending_file.unlink(missing_ok=True)
                rid = None
        if not rid:
            rid = self.call(region, "/reports/2021-06-30/reports", method="POST", body=body)["reportId"]
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            pending_file.write_text(json.dumps({"reportId": rid}), encoding="utf-8")

        doc = None
        for _ in range(self._POLL_TRIES):
            st = self.call(region, f"/reports/2021-06-30/reports/{rid}")
            status = st.get("processingStatus")
            if status == "DONE":
                doc = st["reportDocumentId"]
                break
            if status in ("CANCELLED", "FATAL"):
                pending_file.unlink(missing_ok=True)
                raise RuntimeError(f"品牌分析搜索词报告 {status}，重跑会请求一份新报告。")
            time.sleep(self._POLL_SECONDS)
        if not doc:
            raise RuntimeError(
                f"品牌分析搜索词报告还在生成中 (已等 {self._POLL_TRIES * self._POLL_SECONDS // 60} 分钟)。"
                f"reportId已存，重跑本命令会接着查同一份，不会重新排队。")

        d = self.call(region, f"/reports/2021-06-30/documents/{doc}")
        raw = requests.get(d["url"], timeout=180).content
        if d.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        pending_file.unlink(missing_ok=True)
        payload = json.loads(raw.decode("utf-8", "replace"))
        return payload.get("dataByDepartmentAndSearchTerm") or payload.get("reportData") or []
