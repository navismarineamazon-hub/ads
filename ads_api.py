# -*- coding: utf-8 -*-
"""Amazon Ads API 客户端 (Sponsored Products v3)。

凭据: 从 <project>/.ads-credentials.env 读取 CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN。
凭据值不打印、不写日志。

已实测要点 (来自 amazon-ads-api 技能, 本账户验证过):
- v3 列表/报告接口的 Content-Type 与 Accept 必须是全小写厂商媒体类型, 两个都要带, 否则 415。
- 异步报告轮询等 status == COMPLETED (API 只有 PENDING/PROCESSING/COMPLETED/FAILED, 没有 SUCCESS)。
- 报告列名必须带归因窗口 (sales7d / purchases7d ...); spCampaigns 不支持 acosClicks7d (只有 14d)。
- /sp/keywords/list 等列表接口的 filter 参数在本账户不可用 (400), 全量翻页后本地过滤。
- 创建关键词永远返回 HTTP 207, 成功与失败混在 body 里, 按 index 对位。
"""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

import requests

API_BASE = "https://advertising-api.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# 各列表接口的媒体类型 (全小写, 实测)
MEDIA = {
    "campaigns": "application/vnd.spcampaign.v3+json",
    "adGroups": "application/vnd.spadgroup.v3+json",
    "keywords": "application/vnd.spkeyword.v3+json",
    "negativeKeywords": "application/vnd.spnegativekeyword.v3+json",
    "targets": "application/vnd.sptargetingclause.v3+json",
    "negativeTargets": "application/vnd.spnegativetargetingclause.v3+json",
    "productAds": "application/vnd.spproductad.v3+json",
    "report": "application/vnd.createasyncreportrequest.v3+json",
}

# Sponsored Display (SD) —— 2026-11-09 本账户实测确认: 跟 SP v3 完全不是一套风格，
# 不走 vendor media type, 是 plain application/json + 裸数组 body + 裸数组响应(无 index
# 字段, 按位置对应输入)。字段也跟 SP 不同 (adGroup 没有 bid, bid 在 target 层;
# productAd 用 sku 不用 asin)。schema 用空数组/故意留一个非法字段的方式测出来的，
# 没有真正创建过任何活动。
SD_MEDIA_TYPE = "application/json"
SD_PATHS = {"campaigns", "adGroups", "productAds", "targets"}

# 实体写端点 = MEDIA 里除 report 外每个 key 对应的 /sp/<key>，加上 SD_PATHS 对应的
# /sd/<key> ——这些是 create_*/update_* 用的路径。request() 靠这个集合判断 500 时能不能
# 自动重发 (写请求可能已生效，不能盲目重试)。
WRITE_ENDPOINTS = {f"/sp/{key}" for key in MEDIA if key != "report"} | {f"/sd/{key}" for key in SD_PATHS}


def load_credentials(path: str | Path) -> dict:
    """解析 KEY=VALUE 格式的凭据文件。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"凭据文件不存在: {path}")
    creds = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip()] = v.strip().strip('"').strip("'")
    for required in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
        if not creds.get(required):
            raise ValueError(f"凭据文件缺少 {required}")
    return creds


class TokenManager:
    """LWA access token, 带磁盘缓存 (有效期 ~1h, 提前 5 分钟刷新)。"""

    def __init__(self, creds: dict, cache_file: Path):
        self.creds = creds
        self.cache_file = cache_file
        self._token: str | None = None
        self._expiry = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            self._token = data.get("access_token")
            self._expiry = float(data.get("expiry", 0))
        except Exception:
            pass

    def _save_cache(self) -> None:
        try:
            self.cache_file.write_text(
                json.dumps({"access_token": self._token, "expiry": self._expiry}),
                encoding="utf-8",
            )
        except Exception:
            pass

    def get(self) -> str:
        if self._token and time.time() < self._expiry - 300:
            return self._token
        resp = requests.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.creds["REFRESH_TOKEN"],
                "client_id": self.creds["CLIENT_ID"],
                "client_secret": self.creds["CLIENT_SECRET"],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = f"{err.get('error')}: {err.get('error_description')}"
            except Exception:
                msg = resp.text[:300]
            raise RuntimeError(f"LWA 换 token 失败 HTTP {resp.status_code}: {msg}")
        data = resp.json()
        self._token = data["access_token"]
        self._expiry = time.time() + int(data.get("expires_in", 3600))
        self._save_cache()
        return self._token


class AdsClient:
    def __init__(self, profile_id: str, credentials_file: str | Path, cache_dir: Path,
                 api_base: str | None = None):
        creds = load_credentials(credentials_file)
        self.profile_id = str(profile_id)
        self.tokens = TokenManager(creds, Path(cache_dir) / ".token_cache.json")
        self.session = requests.Session()
        # 区域端点: 美区默认 advertising-api.amazon.com, EU 站点传
        # https://advertising-api-eu.amazon.com, 远东传 ...-fe... (config.yaml api_base)
        self.api_base = (api_base or API_BASE).rstrip("/")

    # ---------- 底层请求 ----------
    def _headers(self, media_type: str | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {self.tokens.get()}",
            "Amazon-Advertising-API-ClientId": self.creds_client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Accept": media_type or "application/json",
        }
        if media_type:
            h["Content-Type"] = media_type
        return h

    @property
    def creds_client_id(self) -> str:
        return self.tokens.creds["CLIENT_ID"]

    def request(self, method: str, path: str, body: dict | list | None = None,
                media_type: str | None = None, max_retries: int = 5) -> requests.Response:
        url = path if path.startswith("http") else self.api_base + path
        for attempt in range(max_retries):
            resp = self.session.request(
                method, url, headers=self._headers(media_type),
                json=body if body is not None else None, timeout=120,
            )
            if resp.status_code == 429:  # 限速
                wait = int(resp.headers.get("Retry-After", "0") or 0) or 2 ** attempt
                print(f"  429 限速, 等待 {wait}s 重试...")
                time.sleep(min(wait, 60))
                continue
            if resp.status_code >= 500:
                # 实体写请求可能已被应用，不能自动重发。交给执行账本核实。
                if method in ("PUT", "POST") and path in WRITE_ENDPOINTS:
                    return resp
                time.sleep(2 ** attempt)
                continue
            return resp
        return resp

    # ---------- 账户 / 列表 ----------
    def profiles(self) -> list:
        return self.request("GET", "/v2/profiles").json()

    def _list_all(self, path: str, media_key: str, item_key: str, max_results: int = 1000,
                  page_cap: int = 60, filters: dict | None = None) -> list:
        """翻页拉全量 (filter 在本账户不可用, 一律本地过滤)。"""
        items, token, pages = [], None, 0
        while pages < page_cap:
            body: dict = {"maxResults": max_results, **(filters or {})}
            if token:
                body["nextToken"] = token
            resp = self.request("POST", path, body, MEDIA[media_key])
            if resp.status_code != 200:
                raise RuntimeError(f"{path} HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            if item_key not in data:
                raise RuntimeError(f"{path}: 缺少列表字段 {item_key}，不使用空快照")
            items.extend(data.get(item_key) or [])
            token = data.get("nextToken")
            pages += 1
            if not token:
                break
            time.sleep(0.3)
        if token:
            raise RuntimeError(f"{path}: 翻页超过上限，快照不完整")
        return items

    def list_campaigns(self, filters=None) -> list:
        return self._list_all("/sp/campaigns/list", "campaigns", "campaigns", filters=filters)

    def list_ad_groups(self, filters=None) -> list:
        return self._list_all("/sp/adGroups/list", "adGroups", "adGroups", filters=filters)

    def list_keywords(self, filters=None) -> list:
        return self._list_all("/sp/keywords/list", "keywords", "keywords", filters=filters)

    def list_negative_keywords(self, filters=None) -> list:
        return self._list_all("/sp/negativeKeywords/list", "negativeKeywords", "negativeKeywords", filters=filters)

    def list_targets(self, filters=None) -> list:
        return self._list_all("/sp/targets/list", "targets", "targetingClauses", filters=filters)

    def list_negative_targets(self, filters=None) -> list:
        return self._list_all("/sp/negativeTargets/list", "negativeTargets",
                              "negativeTargetingClauses", filters=filters)

    def recommend_bids(self, campaign_id, ad_group_id, expressions):
        resp = self.request("POST", "/sp/targets/bid/recommendations", {
            "recommendationType": "BIDS_FOR_EXISTING_AD_GROUP",
            "campaignId": str(campaign_id), "adGroupId": str(ad_group_id),
            "targetingExpressions": expressions},
            "application/vnd.spthemebasedbidrecommendation.v3+json")
        if resp.status_code != 200:
            raise RuntimeError(f"推荐竞价 HTTP {resp.status_code}")
        data = resp.json()
        result = {}
        for theme in data.get("bidRecommendations", []):
            if theme.get("theme") != "CONVERSION_OPPORTUNITIES":
                continue
            for item in theme.get("bidRecommendationsForTargetingExpressions", []):
                exp = item["targetingExpression"]
                values = sorted(float(v["suggestedBid"]) for v in item.get("bidValues", [])
                                if v.get("suggestedBid") is not None)
                if values:
                    result[(exp["type"], (exp.get("value") or "").lower())] = values
        return result

    # ---------- 异步报告 ----------
    def create_report(self, name: str, start: str, end: str, report_type_id: str,
                      group_by: list, columns: list, time_unit: str = "SUMMARY",
                      filters: list | None = None) -> str:
        body = {
            "name": name,
            "startDate": start,
            "endDate": end,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS",
                "groupBy": group_by,
                "columns": columns,
                "reportTypeId": report_type_id,
                "timeUnit": time_unit,
                "format": "GZIP_JSON",
            },
        }
        if filters:
            body["configuration"]["filters"] = filters
        resp = self.request("POST", "/reporting/reports", body, MEDIA["report"])
        data = resp.json()
        rid = data.get("reportId")
        if not rid:
            raise RuntimeError(f"报告创建失败 HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:800]}")
        return rid

    def wait_report(self, report_id: str, timeout_s: int = 2400, poll_s: int = 30, poll_max_s: int = 120) -> str:
        """轮询单个报告到 COMPLETED, 返回下载 url。多报告场景请用 wait_reports (并发等待)。"""
        return self.wait_reports({report_id}, timeout_s, poll_s, poll_max_s)[report_id]

    def wait_reports(self, report_ids, timeout_s: int = 2400, poll_s: int = 30,
                     poll_max_s: int = 120) -> dict[str, str]:
        """并发等待多份报告 (Amazon 侧本就并行生成报告, 这里不该顺序阻塞等它们)。
        每轮查一遍所有还没完成的, 立即检查一次再 sleep, 总耗时是 max(各报告耗时)
        而不是 sum(各报告耗时)。返回 {report_id: url}。"""
        pending = set(report_ids)
        results: dict[str, str] = {}
        deadline = time.time() + timeout_s
        started = time.time()
        n = 0
        while pending:
            for rid in list(pending):
                resp = self.request("GET", f"/reporting/reports/{rid}")
                data = resp.json()
                status = data.get("status", "?")
                if status in ("COMPLETED", "SUCCESS"):
                    url = data.get("url")
                    if not url:
                        raise RuntimeError(f"报告 {rid} COMPLETED 但无 url")
                    results[rid] = url
                    pending.discard(rid)
                elif status in ("FAILED", "FAILURE"):
                    raise RuntimeError(f"报告 {rid} 失败: {json.dumps(data, ensure_ascii=False)[:800]}")
            if not pending:
                break
            if time.time() >= deadline:
                raise TimeoutError(f"报告 {pending} 超时未完成 (>{timeout_s}s)")
            wait = min(poll_s * (2 ** min(n, 8)), poll_max_s, max(0, deadline - time.time()))
            print(f"  轮询: {len(pending)} 份报告处理中, 等待 {int(wait)}s... (已耗时 {int(time.time()-started)}s)",
                  flush=True)
            time.sleep(wait)
            n += 1
        return results

    def download_report(self, url: str) -> list:
        """下载 gzip 报告并解析为行列表。"""
        for attempt in range(3):
            resp = self.session.get(url, timeout=300)
            if resp.status_code == 200:
                break
            time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"报告下载失败 HTTP {resp.status_code}")
        raw = gzip.decompress(resp.content).decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        # 兼容 {records: [...]} 包裹
        for key in ("records", "data", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]

    # ---------- 写操作 (第二阶段用; 207 永远返回, 按 index 对位错误) ----------
    @staticmethod
    def _parse_207(resp: requests.Response, item_key: str) -> tuple[list, list]:
        """207 响应两种实测结构:
        - 空 body: {}
        - 非空: {item_key: {"success": [...], "error": [...]}}  (嵌套 dict)
        """
        data = resp.json()
        inner = data.get(item_key)
        if isinstance(inner, dict):
            return inner.get("success") or [], inner.get("error") or []
        ok = inner or []
        errors = data.get("errors") or data.get("error") or []
        return ok, errors

    def create_broad_keywords(self, items: list) -> tuple[list, list]:
        """items: [{adGroupId, campaignId, keywordText, matchType='BROAD', bid?, state='ENABLED'}]"""
        resp = self.request("POST", "/sp/keywords", {"keywords": items}, MEDIA["keywords"])
        return self._parse_batch_response(resp, "keywords", len(items))

    def create_negative_exact(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, adGroupId?, keywordText, matchType='NEGATIVE_EXACT', state='ENABLED'}]
        端点未在本账户实测过, 首次使用请先用空数组 probe() 试探。"""
        resp = self.request("POST", "/sp/negativeKeywords", {"negativeKeywords": items},
                            MEDIA["negativeKeywords"])
        return self._parse_batch_response(resp, "negativeKeywords", len(items))

    def create_campaigns(self, items: list) -> tuple[list, list]:
        """新建活动 (建定位/PT 活动用)。items: [{name, budget:{budgetType,budget}, startDate
        ('YYYY-MM-DD'), state, targetingType='MANUAL', dynamicBidding:{strategy}}]
        端点未在本账户实测过, 首次使用请先用空数组 probe() 试探。"""
        resp = self.request("POST", "/sp/campaigns", {"campaigns": items}, MEDIA["campaigns"])
        return self._parse_batch_response(resp, "campaigns", len(items))

    def create_ad_groups(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, name, state, defaultBid}]
        端点未在本账户实测过, 首次使用请先用空数组 probe() 试探。"""
        resp = self.request("POST", "/sp/adGroups", {"adGroups": items}, MEDIA["adGroups"])
        return self._parse_batch_response(resp, "adGroups", len(items))

    def create_product_ads(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, adGroupId, asin 或 sku, state='ENABLED'}]
        端点未在本账户实测过, 首次使用请先用空数组 probe() 试探。"""
        resp = self.request("POST", "/sp/productAds", {"productAds": items}, MEDIA["productAds"])
        return self._parse_batch_response(resp, "productAds", len(items))

    def create_targets(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, adGroupId, expression:[{type,value}], expressionType='MANUAL', bid, state='ENABLED'}]
        端点未在本账户实测过, 首次使用请先用空数组 probe() 试探。"""
        resp = self.request("POST", "/sp/targets", {"targetingClauses": items}, MEDIA["targets"])
        return self._parse_batch_response(resp, "targetingClauses", len(items))

    def create_negative_targets(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, adGroupId, expression:[{type='ASIN_SAME_AS', value}], state='ENABLED'}]
        负向定位只支持 ASIN_SAME_AS / ASIN_BRAND_SAME_AS, 不支持类目。
        端点未在本账户实测过, 首次使用请先用空数组 probe() 试探。"""
        resp = self.request("POST", "/sp/negativeTargets", {"negativeTargetingClauses": items},
                            MEDIA["negativeTargets"])
        return self._parse_batch_response(resp, "negativeTargetingClauses", len(items))

    def probe(self, path: str, media_key: str, item_key: str) -> bool:
        """空数组试探: 返回 True 说明端点/媒体类型可用 (不会改任何数据)。"""
        resp = self.request("POST", path, {item_key: []}, MEDIA[media_key])
        print(f"  probe {path}: HTTP {resp.status_code} {resp.text[:120]}")
        return resp.status_code == 207

    # ---------- Sponsored Display (SD) ----------
    @staticmethod
    def _parse_sd_response(resp: requests.Response, count: int) -> tuple[list, list]:
        """SD 创建响应是裸数组 (无 index 字段), 按位置对应输入；每项要么是成功对象
        (带 campaignId/adGroupId/adId/targetId 等), 要么是 {"code","description"} 错误。
        成功判定: 不含 "code" 字段。数组长度跟 count 不一致时整批记为未确认。"""
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return [], [{"code": "INVALID_JSON", "detail": resp.text[:300]}]
        if not isinstance(data, list):
            return [], [{"code": "UNKNOWN_RESPONSE", "detail": json.dumps(data, ensure_ascii=False)[:500]}]
        if len(data) != count:
            return [], [{"code": "LENGTH_MISMATCH", "detail": json.dumps(data, ensure_ascii=False)[:500]}]
        success, errors = [], []
        for i, item in enumerate(data):
            if isinstance(item, dict) and item.get("code") not in (None, "SUCCESS"):
                errors.append({"index": i, **item})
            else:
                success.append({"index": i, **(item if isinstance(item, dict) else {"value": item})})
        return success, errors

    def probe_sd(self, path: str) -> bool:
        """SD 版 probe: 空数组试探, 不会改任何数据。成功响应是 400 "No entities specified"
        (说明端点/content-type 对了), 不是 207 —— 跟 SP 的 probe() 判定标准不同。"""
        resp = self.request("POST", path, [], SD_MEDIA_TYPE)
        print(f"  probe {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.status_code == 400 and "No entities specified" in resp.text

    def create_sd_campaigns(self, items: list) -> tuple[list, list]:
        """items: [{name, tactic='T00020'(contextual商品定位)/'T00030'(受众), state='enabled',
        budget, budgetType='daily', startDate('YYYYMMDD'), costType='cpc'}]。
        2026-11-09 已用空数组+故意非法字段探测确认端点/字段名, 未真正创建过。"""
        resp = self.request("POST", "/sd/campaigns", items, SD_MEDIA_TYPE)
        return self._parse_sd_response(resp, len(items))

    def create_sd_ad_groups(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, name, state='enabled'}]。没有 bid 字段 (bid 在 target 层,
        传 bid 会 422 Unrecognized field)。2026-11-09 已探测确认。"""
        resp = self.request("POST", "/sd/adGroups", items, SD_MEDIA_TYPE)
        return self._parse_sd_response(resp, len(items))

    def create_sd_product_ads(self, items: list) -> tuple[list, list]:
        """items: [{campaignId, adGroupId, sku, state='enabled'}]。只认 sku, 传 asin 会报错
        "Product ad specified asin for create operation"。2026-11-09 已探测确认。"""
        resp = self.request("POST", "/sd/productAds", items, SD_MEDIA_TYPE)
        return self._parse_sd_response(resp, len(items))

    def create_sd_targets(self, items: list) -> tuple[list, list]:
        """商品定位(contextual)。items: [{campaignId, adGroupId, expressionType='manual',
        state='enabled', bid, expression:[{type='asinSameAs', value=ASIN}]}]。
        expressionType 必须小写 'manual' ('MANUAL'/'contextual'/'views' 等全部被拒绝)。
        2026-11-09 已探测确认。"""
        resp = self.request("POST", "/sd/targets", items, SD_MEDIA_TYPE)
        return self._parse_sd_response(resp, len(items))

    def update_campaigns(self, items: list) -> tuple[list, list]:
        """v3 批量更新活动 (预算等)。items: [{campaignId, name, budget:{budgetType,amount}}]
        单条 PUT /sp/campaigns/{id} 在本账户 403 不可用, 只能走这个批量端点。
        响应结构特殊: {"campaigns":{"success":[...],"error":[...]}}"""
        resp = self.request("PUT", "/sp/campaigns", {"campaigns": items}, MEDIA["campaigns"])
        return self._parse_batch_response(resp, "campaigns", len(items))

    def update_targets(self, items: list) -> tuple[list, list]:
        """v3 批量更新 target bid。items 至少含 targetId、bid。"""
        resp = self.request("PUT", "/sp/targets", {"targetingClauses": items}, MEDIA["targets"])
        return self._parse_batch_response(resp, "targetingClauses", len(items))

    def update_keywords(self, items: list) -> tuple[list, list]:
        """v3 批量更新 keyword bid (BROAD/PHRASE/EXACT 关键词)。items 至少含 keywordId、bid。"""
        resp = self.request("PUT", "/sp/keywords", {"keywords": items}, MEDIA["keywords"])
        return self._parse_batch_response(resp, "keywords", len(items))

    @staticmethod
    def _parse_batch_response(resp: requests.Response, item_key: str, count: int) -> tuple[list, list]:
        """解析批量响应；缺失 index 必须进入失败/待核实，不得默认成功。"""
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return [], [{"code": "INVALID_JSON", "detail": resp.text[:300]}]
        inner = data.get(item_key)
        if not isinstance(inner, dict):
            return [], [{"code": "UNKNOWN_RESPONSE", "detail": json.dumps(data, ensure_ascii=False)[:500]}]
        success = inner.get("success") if isinstance(inner.get("success"), list) else []
        errors = inner.get("error") or inner.get("errors")
        errors = errors if isinstance(errors, list) else []
        seen = {x.get("index") for x in success + errors if isinstance(x, dict)}
        errors = list(errors) + [
            {"index": i, "code": "UNCONFIRMED", "detail": "响应未返回该条目的成功或失败结果"}
            for i in range(count) if i not in seen
        ]
        return success, errors
