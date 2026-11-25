# ============================================================
# Kingphar 客户发现模块 v2.0
# 数据来源：ImportYeti（真实进口商，穿透货代）
# 使用方法：直接运行：python discover_customers.py
# ============================================================

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import anthropic
from dotenv import load_dotenv
from datetime import datetime
from ddgs import DDGS

dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

# ============================================================
# 每次使用时只修改这里
# ============================================================

TARGET_PRODUCT  = "医用纱布、绷带、伤口护理耗材"
TOP_N           = 5   # 最终筛出几家

# 搜索关键词（对应你的产品）
SEARCH_KEYWORDS = [
    "gauze medical importer",
    "bandage medical importer",
    "wound care disposable importer",
    "surgical gauze buyer",
]

# ============================================================
# 数据采集
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 排除名单（中国供应商、货代、已知竞争对手、大型制造商/竞品品牌）
EXCLUDE_KEYWORDS = [
    # 中国供应商 / 货代
    "china", "chinese", "shandong", "hubei", "guangdong",
    "freight", "logistics", "forwarder", "shipping",
    "winner medical", "zhende", "naton",
    # 伤口护理产品制造商（Kingphar竞品，不是买家）
    "convatec", "smith & nephew", "smith and nephew",
    "molnlycke", "mölnlycke", "hartmann", "coloplast",
    "bsn medical", "medline", "dynarex",
    # 大型医疗设备商（不采购纱布绷带）
    "stryker", "medtronic", "covidien", "draeger", "dräger",
    "vyaire", "ge healthcare", "philips healthcare",
    "siemens healthineers", "becton dickinson", "boston scientific",
    "edwards lifesciences", "zimmer biomet", "depuy", "baxter",
    "cardinal health", "hill-rom", "getinge", "fresenius",
    # 制药公司 / 大型零售商
    "pfizer", "johnson & johnson", "merck", "abbvie",
    "amazon", "walmart", "walgreens", "costco",
]


def search_importyeti_by_hs(hs_code, hs_name):
    """抓取ImportYeti HS Code页面获取进口商列表"""
    print(f"  📦 抓取HS Code {hs_code}（{hs_name}）...")

    url = f"https://www.importyeti.com/hs-codes/{hs_code}"
    companies = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # 提取公司链接（ImportYeti的公司链接格式是/company/xxx）
        for a in soup.select("a[href*='/company/']"):
            name = a.get_text(strip=True)
            href = a.get("href", "")
            if name and len(name) > 3 and len(name) < 80:
                # 从URL提取公司slug
                slug = href.split("/company/")[-1].rstrip("/")
                companies.append({
                    "name": name,
                    "slug": slug,
                    "source": f"ImportYeti HS{hs_code}",
                    "country": "USA",  # ImportYeti主要是美国进口数据
                })

    except Exception as e:
        print(f"  ⚠️ 抓取失败：{e}")

    return companies


def search_importyeti_by_ddg(keyword):
    """用DDG搜索ImportYeti上的相关公司"""
    print(f"  🔍 DDG搜索：{keyword}...")

    query = f"site:importyeti.com/company {keyword}"
    companies = []

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=8))

        for r in results:
            url = r.get("href", "")
            title = r.get("title", "")
            snippet = r.get("body", "")

            if "/company/" not in url:
                continue

            # 从URL提取公司名
            slug = url.split("/company/")[-1].rstrip("/")
            # 从标题提取公司名（通常格式是"公司名 - ImportYeti"）
            name = title.split(" - ")[0].strip() if " - " in title else title.strip()

            if name and len(name) > 3:
                # 从摘要提取采购信息
                shipments = ""
                match = re.search(r'(\d+)\s*shipment', snippet, re.IGNORECASE)
                if match:
                    shipments = match.group(1)

                supplier_info = ""
                if "top supplier" in snippet.lower():
                    supplier_info = snippet[:200]

                companies.append({
                    "name": name,
                    "slug": slug,
                    "url": url,
                    "shipments": shipments,
                    "supplier_info": supplier_info,
                    "source": f"ImportYeti DDG:{keyword}",
                    "country": "USA",
                })

        time.sleep(1.5)

    except Exception as e:
        print(f"  ⚠️ DDG搜索出错：{e}")

    return companies


def get_company_detail(slug, company_name):
    """抓取ImportYeti公司详情页"""
    url = f"https://www.importyeti.com/company/{slug}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)

        detail = {
            "name": company_name,
            "url": url,
            "raw_text": text[:3000],
        }

        # 提取关键数字
        shipment_match = re.search(r'(\d[\d,]+)\s*shipment', text, re.IGNORECASE)
        if shipment_match:
            detail["shipments"] = shipment_match.group(1).replace(",", "")

        supplier_match = re.search(r'top supplier[:\s]+([^\n.]{5,60})', text, re.IGNORECASE)
        if supplier_match:
            detail["top_supplier"] = supplier_match.group(1).strip()

        return detail

    except Exception as e:
        return {}


def is_excluded(name):
    """检查是否是要排除的公司"""
    name_lower = name.lower()
    return any(kw in name_lower for kw in EXCLUDE_KEYWORDS)


# 国家名中英文映射

def _get_api_configs_dc():
    """读取主备API配置，供 discover 阶段使用"""
    configs = []
    pk = os.getenv("ANTHROPIC_API_KEY")
    pu = os.getenv("ANTHROPIC_BASE_URL", "")
    pm = os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6")
    if pk and pu:
        pu_clean = pu.rstrip("/")
        if pu_clean.endswith("/v1"): pu_clean = pu_clean[:-3]
        configs.append({"name": "主服务", "api_key": pk, "base_url": pu_clean, "model": pm})
    bk = os.getenv("BACKUP_API_KEY")
    bu = os.getenv("BACKUP_BASE_URL", "")
    bm = os.getenv("BACKUP_MODEL", "claude-sonnet-4-6")
    if bk and bu:
        bu_clean = bu.rstrip("/")
        if bu_clean.endswith("/v1"): bu_clean = bu_clean[:-3]
        configs.append({"name": "备用服务", "api_key": bk, "base_url": bu_clean, "model": bm})
    return configs


def _call_api_dc(config, prompt, max_tokens=2000):
    """用httpx直接调用API，支持主备切换"""
    import httpx as _hx
    api_path = os.getenv("PRIMARY_API_PATH", "/v1/messages")
    url = config["base_url"] + api_path
    headers = {
        "x-api-key": config["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": config["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    with _hx.Client(timeout=_hx.Timeout(connect=15, read=None, write=15, pool=5)) as hc:
        resp = hc.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if not resp.text.strip():
        raise RuntimeError("API返回空响应体")
    data = resp.json()
    text = (data.get("content") or [{}])[0].get("text", "")
    if not text:
        raise RuntimeError(f"响应text为空: {str(data)[:200]}")
    return text

COUNTRY_EN = {
    # 北美
    "美国":"USA","加拿大":"Canada","墨西哥":"Mexico",
    # 欧洲西部
    "德国":"Germany","英国":"United Kingdom","法国":"France",
    "意大利":"Italy","西班牙":"Spain","荷兰":"Netherlands",
    "比利时":"Belgium","瑞士":"Switzerland","奥地利":"Austria",
    "葡萄牙":"Portugal","瑞典":"Sweden","挪威":"Norway",
    "丹麦":"Denmark","芬兰":"Finland",
    # 欧洲东部
    "波兰":"Poland","捷克":"Czech Republic","罗马尼亚":"Romania",
    "匈牙利":"Hungary","希腊":"Greece",
    # 中东
    "阿联酋":"UAE","沙特阿拉伯":"Saudi Arabia","土耳其":"Turkey",
    "以色列":"Israel","卡塔尔":"Qatar","科威特":"Kuwait","埃及":"Egypt",
    # 亚洲
    "日本":"Japan","韩国":"South Korea","印度":"India",
    "新加坡":"Singapore","泰国":"Thailand","越南":"Vietnam",
    "马来西亚":"Malaysia","印度尼西亚":"Indonesia",
    "菲律宾":"Philippines","孟加拉国":"Bangladesh","巴基斯坦":"Pakistan",
    # 大洋洲
    "澳大利亚":"Australia","新西兰":"New Zealand",
    # 南美
    "巴西":"Brazil","阿根廷":"Argentina","智利":"Chile",
    "哥伦比亚":"Colombia","秘鲁":"Peru",
    # 非洲
    "南非":"South Africa","尼日利亚":"Nigeria","肯尼亚":"Kenya",
}

def search_companies_by_country(country_cn, product_keywords=None, max_results=12, query_override=""):
    """
    用 DDG 搜索指定国家的真实医疗耗材公司（直接搜公司官网，过滤目录页）。
    """

    # ── AI 查询覆盖 ──
    if query_override:
        try:
            from collect_info import ddg_search
            results = ddg_search(query_override, num=8, timeout=10)
            companies = []
            for r in results:
                title = r.get('title', '')
                # 从搜索结果提取公司名
                name = title.split(' - ')[0].split(' | ')[0].split(' — ')[0].strip()
                if len(name) > 3 and len(name) < 80:
                    companies.append({
                        'name': name,
                        'country': country if isinstance(country, str) else '',
                        'website': r.get('href', r.get('url', '')),
                        'reason': r.get('body', r.get('snippet', ''))[:200],
                    })
            if companies:
                return companies[:max_results if 'max_results' in dir() else 10]
        except Exception as _e:
            pass  # 降级到原始逻辑

    country_en = COUNTRY_EN.get(country_cn, country_cn)
    # 国家对应的顶级域名（用于识别本地公司官网）
    country_tld = {
        "德国":".de","法国":".fr","英国":".co.uk","意大利":".it","西班牙":".es",
        "荷兰":".nl","比利时":".be","瑞士":".ch","奥地利":".at","葡萄牙":".pt",
        "瑞典":".se","挪威":".no","丹麦":".dk","芬兰":".fi",
        "波兰":".pl","捷克":".cz","罗马尼亚":".ro","匈牙利":".hu","希腊":".gr",
        "阿联酋":".ae","土耳其":".com.tr","以色列":".co.il","卡塔尔":".qa",
        "日本":".co.jp","韩国":".co.kr","印度":".in","新加坡":".com.sg",
        "泰国":".co.th","越南":".vn","马来西亚":".com.my","印度尼西亚":".co.id",
        "菲律宾":".com.ph","澳大利亚":".com.au","新西兰":".co.nz",
        "巴西":".com.br","阿根廷":".com.ar","智利":".cl","哥伦比亚":".com.co",
        "加拿大":".ca","南非":".co.za",
    }.get(country_cn, "")

    print(f"  🌍 全球搜索：{country_cn}({country_en}) 医疗耗材真实公司")

    # 专门搜公司官网的查询策略
    queries = [
        f'"{country_en}" medical disposables distributor wholesale site:{country_tld.lstrip(".")}' if country_tld else f'medical disposables distributor {country_en} GmbH OR Ltd OR SAS OR BV',
        f'wound care bandage supplier {country_en} GmbH OR Ltd OR Corp',
        f'surgical consumables importer {country_en} company',
        f'medical supplies wholesaler {country_en} -"list of" -"top 10" -directory',
    ]

    # 目录/文章类标题前缀（有这些开头的不是真实公司）
    TITLE_BLACKLIST = [
        "list of","top ","best ","leading ","major ","key ","find ","directory",
        "suppliers in ","distributors in ","companies in ","wholesalers in ",
        "medical distributors","medical suppliers","how to","what is",
        "guide to","introduction","overview","article","news","2024","2025",
    ]
    # URL路径中出现这些词说明是目录/文章页
    URL_PATH_BLACKLIST = [
        "list","directory","article","blog","news","guide","top-","best-",
        "suppliers","distributors","wholesalers","companies-in","wiki",
        "search","category","tag","page","post",
    ]
    SKIP_DOMAINS = [
        "wikipedia","linkedin","alibaba","made-in-china","globalspec",
        "europages","kompass","dnb.com","bloomberg","reuters","thomasnet",
        "craftsvilla","indiamart","tradekey","ecplaza","busytrade",
        "hellotrade","tradeindia","exportersindia","yellowpages","yelp",
        "bizbuysell","manta.com","chamberofcommerce","bizapedia",
        "opencorporates","zoominfo","crunchbase","glassdoor","indeed",
        "trustpilot","sitejabber","bbb.org","ripoffreport",
    ]

    companies = []
    seen_domains = set()

    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    from urllib.parse import urlparse

    for query in queries[:4]:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=8, region="wt-wt"))

            for r in results:
                url   = r.get("href", "").lower()
                title = r.get("title", "")
                body  = r.get("body", "")
                title_lower = title.lower()

                # 1. 跳过黑名单域名
                if any(d in url for d in SKIP_DOMAINS):
                    continue

                # 2. 跳过目录/文章类标题
                if any(title_lower.startswith(bl) or bl in title_lower[:40]
                       for bl in TITLE_BLACKLIST):
                    continue

                # 3. 跳过URL路径含目录/文章特征的
                try:
                    parsed = urlparse(url)
                    path = parsed.path.lower()
                    if any(bl in path for bl in URL_PATH_BLACKLIST):
                        continue
                    domain = parsed.netloc.replace("www.", "")
                except:
                    continue

                # 4. 域名去重
                if not domain or domain in seen_domains:
                    continue
                seen_domains.add(domain)

                # 5. 提取公司名
                name = title.split(" - ")[0].split(" | ")[0].split(" – ")[0].strip()
                name = name.strip(".")
                if not name or len(name) < 4 or len(name) > 80:
                    continue

                # 6. 要求有医疗相关词
                combined = (title + " " + body).lower()
                medical_kw = ["medical","surgical","healthcare","clinical","hospital",
                               "wound","bandage","gauze","disposable","consumable",
                               "pharma","meditec","sanitär","gesundheit","santé"]
                if not any(k in combined for k in medical_kw):
                    continue

                # 7. 拼回原始URL（不用lower版本）
                real_url = r.get("href", "")

                companies.append({
                    "name":      name,
                    "slug":      "",
                    "country":   country_cn,
                    "website":   real_url,
                    "shipments": "—",
                    "source":    f"DDG·{country_cn}",
                    "describe":  body[:150],
                })
                print(f"    ✓ {name} ({domain})")

            time.sleep(1.5)
            if len(companies) >= max_results:
                break

        except Exception as e:
            print(f"  ⚠️ 搜索出错({query[:40]}): {e}")
            time.sleep(1)

    print(f"  ✅ {country_cn} 找到 {len(companies)} 家真实候选公司")
    return companies


def ai_generate_queries(product, countries, log_fn=None):
    """
    用 AI 生成针对目标产品+国家的精准搜索词列表。
    输出：[{"country": "德国", "query": "Verbandsmittel Großhändler Deutschland", "intent": "本地经销商"}, ...]
    """
    import anthropic as _anth, json as _json, re as _re, threading as _th

    def _log(msg):
        if log_fn: log_fn(msg)
        else: print(msg)

    # 确保全是 dict
    companies = [c for c in companies if isinstance(c, dict) and c.get("name")]
    if not companies:
        return []
    
    # 读取主备API配置（和 kingphar_intel.py 保持一致）
    api_configs = _get_api_configs_dc()
    if not api_configs:
        _log("  ⚠️ AI检索：未找到任何API配置，跳过")
        return []

    country_list = "、".join(countries) if countries else "全球"

    prompt = f"""你是医用耗材出口开发专家。为Kingphar（中国医用纱布/绷带/伤口护理耗材出口商）
生成在以下目标市场找到真实采购商/经销商的搜索词。

产品：{product}
目标国家：{country_list}

要求：
- 每个国家生成3-4条搜索词
- 使用当地语言或行业术语（德国用德语、法国用法语等）
- 覆盖：本地经销商、医院采购商、医疗耗材批发商、GPO采购组织
- 避免：制造商、品牌商、目录网站
- 格式：可以直接输入搜索引擎的短语

只返回JSON数组，不要任何其他文字：
[
  {{"country": "国家名", "query": "搜索词", "intent": "目标类型（经销商/批发商/医院采购/GPO）"}},
  ...
]"""

    # 国家太多时拆批，每批最多5个国家，避免prompt太长返回空
    MAX_PER_BATCH = 5
    all_queries = []

    for batch_start in range(0, len(countries), MAX_PER_BATCH):
        batch = countries[batch_start: batch_start + MAX_PER_BATCH]
        batch_str = "、".join(batch)
        n_expected = len(batch) * 3  # 每国3条，估算max_tokens

        batch_prompt = f"""你是医用耗材出口开发专家。为Kingphar生成精准搜索词，找到真实的医疗耗材采购商。

产品：{product}
目标国家：{batch_str}

每个国家生成3条搜索词，使用当地语言/行业术语，覆盖：经销商、批发商、医院采购、GPO。

直接返回JSON数组（不要任何解释或代码块）：
[{{"country":"国家名","query":"搜索词","intent":"目标类型"}}]"""

        # 主备切换：逐个尝试API配置
        batch_result = None
        for cfg in api_configs:
            try:
                _log(f"  🔌 [{cfg['name']}] 生成搜索词...")
                raw = _call_api_dc(cfg, batch_prompt, max_tokens=max(800, n_expected * 40))
                raw = _re.sub(r"```[a-z]*\s*", "", raw)
                raw = _re.sub(r"```\s*", "", raw).strip()
                m = _re.search(r"\[.*\]", raw, _re.DOTALL)
                raw = m.group() if m else raw
                parsed = _json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    batch_result = parsed
                    break
            except Exception as e:
                _log(f"  ⚠️ [{cfg['name']}] 失败: {str(e)[:100]}，切换下一个...")

        if batch_result:
            all_queries.extend(batch_result)
            _log(f"  🤖 [{batch_str}] 生成 {len(batch_result)} 条搜索词")
        else:
            _log(f"  ⚠️ [{batch_str}] 所有API均失败，跳过")

    _log(f"  ✅ AI共生成 {len(all_queries)} 条精准搜索词")
    return all_queries


def search_by_ai_queries(ai_queries, log_fn=None, max_per_query=6):
    """
    用多线程并行执行AI生成的搜索词，每条独立线程，总超时60秒。
    用线程列表而不是 ThreadPoolExecutor dict，避免 Future hashability 问题。
    """
    from urllib.parse import urlparse as _up
    import threading as _th2
    import time as _t2

    def _log(msg):
        if log_fn: log_fn(msg)
        else: print(msg)

    if not ai_queries:
        return []

    SKIP_DOMAINS = [
        "wikipedia","linkedin","alibaba","made-in-china","globalspec",
        "europages","kompass","dnb.com","bloomberg","reuters","thomasnet",
        "indiamart","tradekey","yellowpages","yelp","manta.com",
        "zoominfo","crunchbase","glassdoor","trustpilot",
    ]
    TITLE_BLACKLIST = [
        "list of","top ","best ","leading ","directory","suppliers in",
        "distributors in","companies in","how to","2024","2025",
    ]

    all_companies = []
    seen_domains = set()
    lock = _th2.Lock()

    def _search_one(item, result_box):
        query   = item.get("query", "")
        country = item.get("country", "")
        intent  = item.get("intent", "")
        found   = []

        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        import concurrent.futures as _cf2
        def _do():
            with DDGS() as d:
                return list(d.text(query, max_results=max_per_query))

        ex = _cf2.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_do)
        ex.shutdown(wait=False)
        try:
            results = fut.result(timeout=9)
        except Exception:
            result_box.append([])
            return

        for r in results:
            url     = r.get("href", "")
            title   = r.get("title", "")
            body    = r.get("body", "")
            url_l   = url.lower()
            title_l = title.lower()

            if any(d in url_l for d in SKIP_DOMAINS): continue
            if any(title_l.startswith(bl) or bl in title_l[:50] for bl in TITLE_BLACKLIST): continue

            try:
                parsed = _up(url)
                domain = parsed.netloc.replace("www.", "")
                path   = parsed.path.lower()
                if any(b in path for b in ["list","directory","blog","article","wiki","news"]): continue
            except:
                continue

            with lock:
                if domain in seen_domains: continue
                seen_domains.add(domain)

            name = title.split(" - ")[0].split(" | ")[0].split(" – ")[0].strip().strip(".")
            if not name or len(name) < 4 or len(name) > 80: continue

            combined = (title + " " + body).lower()
            med_kw = ["medical","surgical","healthcare","clinical","hospital","wound",
                      "bandage","gauze","disposable","consumable","pharma",
                      "santé","gesundheit","sanitär","salud"]
            if not any(k in combined for k in med_kw): continue

            found.append({
                "name":       name,
                "slug":       "",
                "country":    country,
                "website":    url,
                "shipments":  "—",
                "source":     f"AI推荐·{country}",
                "describe":   body[:150],
                "_ai_intent": intent,
                "_ai_query":  query,
            })

        result_box.append(found)

    _log(f"  🔍 并行执行 {len(ai_queries)} 条AI推荐搜索词...")

    # 每条搜索词一个线程，result_box收集结果
    threads = []
    result_boxes = []
    for item in ai_queries:
        box = []
        result_boxes.append(box)
        t = _th2.Thread(target=_search_one, args=(item, box), daemon=True)
        threads.append((t, item))
        t.start()

    # 等待所有线程，总超时60s
    deadline = _t2.time() + 60
    for t, item in threads:
        remaining = deadline - _t2.time()
        if remaining <= 0:
            break
        t.join(timeout=max(0.5, remaining))

    # 汇总结果
    for box, (_, item) in zip(result_boxes, threads):
        if box and box[0]:
            found = box[0]
            all_companies.extend(found)
            _log(f"    ✓ [{item.get('country','')}] {item.get('query','')[:40]} → {len(found)}家")

    _log(f"  ✅ AI推荐检索共找到 {len(all_companies)} 家候选")
    return all_companies


def deduplicate(companies):
    """去重"""
    seen = set()
    result = []
    for c in companies:
        key = c["name"].lower().strip().replace(" ", "")
        if key not in seen and not is_excluded(c["name"]):
            seen.add(key)
            result.append(c)
    return result


def ai_score_and_select(companies, product, top_n):
    """AI评分筛选最有价值的目标客户（主服务失败立即切备用）"""

    api_configs = _get_api_configs_dc()

    company_list = "\n".join([
        f"{i+1}. {c['name']} | 国家:{c.get('country','未知')} | 采购次数:{c.get('shipments','未知')} | 来源:{c.get('source','未知')} | 描述:{c.get('describe','')[:60]}"
        for i, c in enumerate(companies[:40])
    ])

    country_list = list(set(c.get("country","未知") for c in companies[:40] if c.get("country")))
    markets_str = "、".join(country_list) if country_list else "全球"

    # 根据产品线动态生成筛选重点
    p_lower = product.lower()
    if any(k in p_lower for k in ["纱布","gauze","绷带","bandage","敷料","dressing","伤口","wound"]):
        product_focus = "重点找伤口护理耗材分销商、急救耗材批发商、药房/药店连锁采购商"
        exclude_extra = "排除：伤口护理品牌商（Convatec、Mölnlycke、Smith&Nephew等直接竞品）"
    elif any(k in p_lower for k in ["手套","gloves","口罩","mask","防护","protective","无菌","sterile"]):
        product_focus = "重点找手术室耗材供应商、医院采购部门、防护用品经销商"
        exclude_extra = "排除：医疗设备制造商、大型药企"
    else:
        product_focus = "重点找医疗耗材综合分销商、进口商、GPO采购组织"
        exclude_extra = "排除：医疗设备制造商（Stryker、Medtronic等）、大型制药公司"

    prompt = f"""你是医用耗材外贸专家，为Kingphar筛选最值得开发的潜在买家。

【我方产品】{product}
【我方优势】FDA(1998)+CE MDR+ISO13485，疫情零断供，出口前十，支持OEM
【目标市场】{markets_str}
【产品匹配重点】{product_focus}

候选公司名单：
{company_list}

【筛选标准】
✅ 优先：医疗耗材分销商/进口商/批发商/GPO，有中国或亚洲采购记录
✅ 优先：中小型公司（决策快、灵活性强）
✅ 接受：医院采购部门、药店连锁采购、政府卫生采购机构
✅ 非美国公司：优先看行业匹配度，无采购记录也可入选
❌ {exclude_extra}
❌ 货代/物流公司、制药公司、大型零售商

选出最值得开发的 {top_n} 家，只返回JSON数组：
[{{"name":"公司名","country":"国家","shipments":"采购次数或—","reason":"推荐理由（具体说明业务匹配点和切入机会）","priority":"高/中","approach":"首封邮件切入角度"}}]"""

    print(f"\n🤖 AI正在从 {len(companies)} 家候选公司中筛选 Top {top_n}...\n")

    def parse_json(raw):
        raw = raw.strip()
        for attempt_text in [
            raw,
            re.sub(r'```[a-z]*\s*', '', re.sub(r'```\s*', '', raw)).strip(),
        ]:
            try:
                r = json.loads(attempt_text)
                if isinstance(r, list) and len(r) > 0:
                    return r
            except:
                pass
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            try:
                r = json.loads(m.group())
                if isinstance(r, list) and len(r) > 0:
                    return r
            except:
                pass
        return None

    for config in api_configs:
        print(f"  🔌 {config['name']}（{config['model']}）")
        try:
            raw = _call_api_dc(config, prompt, max_tokens=3000)
            result = parse_json(raw)
            if result:
                print(f"  ✅ 成功（{config['name']}，解析出{len(result)}家）")
                return result
            else:
                print(f"  ⚠️ JSON解析失败，原始内容：{raw[:200]}")
                raise ValueError("JSON解析失败")
        except Exception as e:
            print(f"  ❌ {config['name']} 失败：{str(e)[:100]}")
            if config != api_configs[-1]:
                print("  🔄 切换备用服务...")

    return []



def main():
    print("\n" + "="*60)
    print("     Kingphar 客户发现模块 v3.0")
    print("     数据来源：ImportYeti（美国） + 跨境搜（全球）")
    print("="*60)
    print(f"  主推产品：{TARGET_PRODUCT}")
    print(f"  筛选数量：Top {TOP_N}")
    print("="*60)

    all_companies = []

    # ── 数据源1：ImportYeti（美国进口商，真实采购数据）──────────
    print("\n📦 数据源1：ImportYeti（美国市场）")

    hs_codes = [
        ("5803", "gauze-other-than-narrow-fabrics"),
        ("3005", "bandages-wound-care"),
    ]
    for code, name in hs_codes:
        results = search_importyeti_by_hs(code, name)
        print(f"     HS{code} 找到 {len(results)} 家")
        all_companies.extend(results)
        time.sleep(2)

    for keyword in SEARCH_KEYWORDS:
        results = search_importyeti_by_ddg(keyword)
        print(f"     DDG [{keyword}] 找到 {len(results)} 家")
        all_companies.extend(results)
        time.sleep(1.5)

    # ── 数据源2：跨境搜（全球市场补充）────────────────────────
    try:
        from kuajings_client import discover_from_kuajings, _is_configured
        if _is_configured():
            print("\n🌍 数据源2：跨境搜（全球市场补充）")
            kj_companies = discover_from_kuajings(product=TARGET_PRODUCT, top_n=TOP_N * 2)
            # 转换为 all_companies 的统一格式
            for c in kj_companies:
                all_companies.append({
                    "name":     c["name"],
                    "slug":     "",
                    "country":  c.get("country", ""),
                    "shipments":c.get("shipments", ""),
                    "source":   "跨境搜",
                    "_kj_raw":  c.get("_raw", {}),
                })
            print(f"     跨境搜补充 {len(kj_companies)} 家")
        else:
            print("\n  ℹ️  跨境搜未配置（KUAJINGS_KEY/SECRET），跳过全球市场扫描")
    except ImportError:
        print("\n  ℹ️  kuajings_client.py 未安装，跳过跨境搜")
    except Exception as e:
        print(f"\n  ⚠️  跨境搜数据源异常：{e}，继续使用 ImportYeti 数据")

    # 去重过滤
    unique = deduplicate(all_companies)
    print(f"\n✅ 双数据源合并去重后共 {len(unique)} 家候选公司")
    us_n  = sum(1 for c in unique if c.get("source") != "跨境搜")
    kj_n  = sum(1 for c in unique if c.get("source") == "跨境搜")
    if kj_n or us_n:
        print(f"   其中：ImportYeti(美国) {us_n} 家 + 跨境搜(全球) {kj_n} 家")

    if len(unique) < 3:
        print("⚠️ 候选公司太少，尝试补充搜索...")
        extra = search_importyeti_by_ddg("medical disposable importer USA wound care")
        unique.extend(extra)
        unique = deduplicate(unique)
        print(f"   补充后共 {len(unique)} 家")

    if not unique:
        print("❌ 未找到候选公司，请检查网络连接")
        return []

    # 第三步：对部分公司抓取详情（采购次数、供应商信息）
    print(f"\n📊 抓取前 {min(15, len(unique))} 家公司的详细采购数据...")
    enriched = []
    for c in unique[:15]:
        if c.get("slug"):
            detail = get_company_detail(c["slug"], c["name"])
            if detail:
                c.update(detail)
        enriched.append(c)
        time.sleep(1)

    # 第四步：AI筛选评分
    top = ai_score_and_select(enriched if enriched else unique, TARGET_PRODUCT, TOP_N)

    # 输出结果
    print("\n" + "="*60)
    print(f"🏆 AI推荐 Top {TOP_N} 高价值目标客户")
    print("="*60)

    if top:
        for i, c in enumerate(top, 1):
            icon = "🔴" if c.get("priority") == "高" else "🟡"
            print(f"\n{icon} 第{i}名：{c.get('name','')}")
            print(f"   国家：{c.get('country','USA')}")
            print(f"   采购次数：{c.get('shipments','未知')}")
            print(f"   推荐理由：{c.get('reason','')}")
            print(f"   切入角度：{c.get('approach','')}")

        # 保存结果
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"目标客户名单_{timestamp}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(top, f, ensure_ascii=False, indent=2)

        print(f"\n✅ 名单已保存到：D:\\kingphar_crm\\{filename}")
        print("\n💡 下一步：运行 kingphar_intel.py 对这些公司逐一深度分析")

    return top


if __name__ == "__main__":
    main()
