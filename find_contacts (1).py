# ============================================================
# Ox · find_contacts.py  v5.2
# v5.1 + 增强数据源 + 深度爬取 + 多重验证 + 决策人评分
# 新增：展会名录/B2B平台/政府采购/PDF挖掘/Sitemap/Wayback/
#       Facebook WhatsApp/Google Dork/多验证器投票/域名健康度/
#       邮箱三角校验/社交存在验证/决策人识别/邮箱模式学习
# ============================================================
import os, re, time, json, socket, smtplib, requests
from bs4 import BeautifulSoup
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
import concurrent.futures as _cf
from dotenv import load_dotenv

# v5.2 新增模块
try:
    from enhanced_sources import run_all_enhanced_sources
    ENHANCED_SOURCES_AVAILABLE = True
except ImportError:
    ENHANCED_SOURCES_AVAILABLE = False

try:
    from deep_scraper import run_deep_scraping, batch_social_check
    DEEP_SCRAPER_AVAILABLE = True
except ImportError:
    DEEP_SCRAPER_AVAILABLE = False

try:
    from email_verifier_v2 import (
        enhanced_verify_contacts, enhanced_priority_score,
        triangulate_email, learn_email_pattern, score_domain_health,
    )
    ENHANCED_VERIFIER_AVAILABLE = True
except ImportError:
    ENHANCED_VERIFIER_AVAILABLE = False

try:
    import whois as whois_lib
    WHOIS_AVAILABLE = True
except ImportError:
    WHOIS_AVAILABLE = False

dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
BUYER_TITLES = ["purchasing","procurement","supply chain","sourcing","buyer","category manager","vendor","supplier","operations manager","director of operations"]
EXEC_TITLES  = ["owner","president","ceo","founder","co-founder","general manager","managing director","vice president","vp ","chief operating","head of"]
HUNTER_API_KEY  = os.getenv("HUNTER_API_KEY","")
HUNTER_API_KEY2 = os.getenv("HUNTER_API_KEY2","")
HUNTER_KEYS = [k for k in [HUNTER_API_KEY, HUNTER_API_KEY2] if k]
EMAIL_PATTERNS = ["{first}.{last}@{domain}","{first}{last}@{domain}","{f}{last}@{domain}","{first}@{domain}","{first}_{last}@{domain}","{last}.{first}@{domain}","{f}.{last}@{domain}"]
DEPT_PREFIXES  = ["purchasing","procurement","sourcing","supply","sales","info","contact","orders","import"]
DOMAIN_SKIP    = ["linkedin","facebook","twitter","importyeti","wikipedia","amazon","bloomberg","crunchbase","zoominfo","dnb","glassdoor","indeed","yelp","yellowpages","hoovers","bbb.org","manta.com","bizapedia","opencorporates"]
WRONG_DOMAIN_SIGNALS = ["news","media","press","journal","blog","tech","software","finance","insurance","realty","auto","car","pet","vet","cpap","sleep","dental","legal","law","attorney","devdiscourse","techcrunch","reuters","bloomberg"]


# ============================================================
# 0. 域名级预筛（免费，不需要25端口）
# ============================================================
_PORT25_BLOCKED = True  # 阿里云马来西亚节点确认封禁

DISPOSABLE_DOMAINS = {
    "mailinator.com","tempmail.com","guerrillamail.com","throwaway.email",
    "yopmail.com","sharklasers.com","guerrillamailblock.com","grr.la",
    "dispostable.com","mailnesia.com","trashmail.com","10minutemail.com",
    "temp-mail.org","fakeinbox.com","mailcatch.com","tempail.com",
}

# MX 查询结果缓存（同一域名不重复查 DNS）
_mx_cache = {}

def domain_has_mx(domain):
    """无MX记录 = 不可能收邮件，直接淘汰（带缓存）"""
    domain = domain.lower().strip()
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, 'MX')
        result = len(answers) > 0
    except Exception:
        result = False
    _mx_cache[domain] = result
    return result

def is_disposable_domain(domain):
    """一次性邮箱域名过滤"""
    return domain.lower() in DISPOSABLE_DOMAINS

def check_domain_quality(domain):
    """
    综合域名检查，返回 (valid, label)
    valid=True 可用, valid=False 淘汰
    label: 标记文本附加到 email_status
    """
    if not domain:
        return False, "❌域名为空"
    domain = domain.lower().strip()
    if is_disposable_domain(domain):
        return False, "❌一次性邮箱域名"
    if not domain_has_mx(domain):
        return False, "❌域名无MX记录（无法收邮件）"
    return True, ""

def try_free_email_verify(email):
    """
    用免费API验证邮箱（不需要25端口）
    主力: disify.com | 备选: mailcheck.ai
    返回: 'valid' / 'invalid' / 'risky' / 'unknown'
    """
    # ---- 主力：disify.com ----
    try:
        resp = requests.get(
            f"https://disify.com/api/email/{email}",
            timeout=6
        )
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                pass  # fall through to backup
            elif data.get("disposable"):
                return "invalid"
            elif not data.get("dns"):
                return "invalid"  # 域名DNS不通
            elif not data.get("format"):
                return "invalid"  # 邮箱格式错误
            elif data.get("role"):
                return "risky"  # info@ sales@ 等角色邮箱
            else:
                return "valid"
    except (requests.RequestException, ValueError, KeyError):
        pass

    # ---- 备选：mailcheck.ai ----
    try:
        resp = requests.get(
            f"https://api.mailcheck.ai/email/{email}",
            timeout=6
        )
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                return "unknown"
            if data.get("disposable"):
                return "invalid"
            if data.get("spam"):
                return "invalid"
            if not data.get("mx"):
                return "invalid"
            if data.get("role_account"):
                return "risky"
            return "valid"
    except (requests.RequestException, ValueError, KeyError):
        pass

    return "unknown"


# ============================================================
# DDG 搜索封装（复用线程池）
# ============================================================
_ddg_pool = _cf.ThreadPoolExecutor(max_workers=2)

def _safe_ddg(query, max_results=6, timeout=8):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    def _do():
        with DDGS() as d:
            return list(d.text(query, max_results=max_results))
    fut = _ddg_pool.submit(_do)
    try:
        return fut.result(timeout=timeout)
    except Exception:
        return []


# ============================================================
# NEW: WhatsApp 号码抓取
# ============================================================
WA_PATTERNS = [
    r'wa\.me/(\+?[\d\s\-\(\)]{7,20})',
    r'api\.whatsapp\.com/send[?]phone=([\d\+]+)',
    r'[Ww]hats[Aa]pp[\s::\-]*(\+?[\d\s\-\(\)]{8,20})',
    r'WA[\s::]*(\+?[\d\s\-\(\)]{8,20})',
]

def _clean_phone(raw):
    if not raw:
        return ""
    cleaned = re.sub(r'[^\d+]', '', raw)
    if len(cleaned) < 7:
        return ""
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned

def scrape_whatsapp(website, timeout=8):
    if not website:
        return []
    url = website.strip()
    if not url.startswith("http"):
        url = "https://" + url
    from urllib.parse import urlparse
    pages_to_check = [url]
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for slug in ["/contact", "/contact-us", "/about", "/about-us"]:
            pages_to_check.append(base + slug)
    except Exception:
        pass
    wa_numbers = []
    for page_url in pages_to_check[:3]:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code != 200:
                continue
            for pattern in WA_PATTERNS:
                for m in re.finditer(pattern, resp.text):
                    phone = _clean_phone(m.group(1))
                    if phone and phone not in wa_numbers:
                        wa_numbers.append(phone)
        except Exception:
            continue
    if wa_numbers:
        print(f"    WhatsApp: 找到 {len(wa_numbers)} 个号码: {wa_numbers}")
    return wa_numbers

# ============================================================
# NEW: Apollo.io 查询
# ============================================================
def query_apollo(company_name, country="", domain="", max_results=5):
    try:
        from apollo_client import search_contacts, is_configured
        if not is_configured():
            return []
        contacts = search_contacts(company_name=company_name, country=country, domain=domain, max_results=max_results)
        result = []
        for c in contacts:
            phone = c.get("phone","")
            result.append({
                "name":     c.get("name",""),
                "position": c.get("title","") or c.get("position",""),
                "email":    c.get("email",""),
                "phone":    phone,
                "whatsapp": phone,
                "linkedin": c.get("linkedin",""),
                "email_status":  c.get("email_status",""),
                "email_guesses": [],
                "source":        "Apollo.io",
                "_apollo_score": c.get("_title_score",0),
            })
        return result
    except Exception as e:
        print(f"    Apollo.io 跳过: {e}")
        return []

# ============================================================
# NEW: Claude AI + Web Search 联系人搜索
# 让 Claude 像人一样联网搜索 WhatsApp、邮箱、联系人
# ============================================================

import anthropic

_anthropic_client = None

def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def claude_search_contacts(company_name, country="", domain="", company_url=""):
    """
    用 Claude API + web_search 工具联网搜索公司联系人。
    Claude 会自主决定搜索策略，找 WhatsApp、邮箱、关键人。
    返回格式与其他函数一致的 contacts 列表。
    """
    client = _get_anthropic_client()
    if not client:
        print("    Claude搜索：无API Key，跳过")
        return []

    # 构造 prompt，让 Claude 专注搜索联系信息
    site_info = ""
    if domain:
        site_info += f"公司官网域名: {domain}\n"
    if company_url:
        site_info += f"公司网址: {company_url}\n"

    prompt = f"""你是一个B2B外贸客户开发专家。请帮我搜索以下公司的真实联系方式。

公司名称: {company_name}
国家: {country or '未知'}
{site_info}

请执行以下搜索任务（按优先级）：

1. **WhatsApp号码**（最重要）：
   - 搜索 "{company_name} WhatsApp" 或 "{company_name} WhatsApp number"
   - 搜索 "site:wa.me {company_name}" 或 "{domain} WhatsApp"
   - 查看公司官网的 contact/about 页面里是否有 WhatsApp

2. **采购/进口负责人**：
   - 搜索 "{company_name} purchasing manager" 或 "procurement director"
   - 搜索 "{company_name} buyer email"
   - 搜索 LinkedIn 上该公司的采购相关人员

3. **公司邮箱和电话**：
   - 搜索 "{company_name} email contact"
   - 搜索 "@{domain}" 相关邮箱

请尽可能多搜索，找到真实可用的联系方式。

最终请严格按以下JSON格式返回结果（不要任何其他文字，只返回JSON数组）：
```json
[
  {{
    "name": "联系人姓名（没有则空字符串）",
    "position": "职位（没有则空字符串）",
    "email": "邮箱（没有则空字符串）",
    "phone": "电话（没有则空字符串）",
    "whatsapp": "WhatsApp号码（没有则空字符串）",
    "linkedin": "LinkedIn链接（没有则空字符串）",
    "source_detail": "信息来源说明"
  }}
]
```

如果什么都没找到，返回空数组 []。"""

    try:
        print(f"    🧠 Claude联网搜索（web_search）...")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        # 从响应中提取文本（Claude可能多轮搜索后才返回）
        # 处理可能需要多轮的情况
        result_text = _extract_final_text(client, response, prompt)

        # 解析 JSON
        contacts = _parse_claude_contacts(result_text, domain)
        print(f"    🧠 Claude搜索：找到 {len(contacts)} 个联系人")
        return contacts

    except Exception as e:
        print(f"    Claude搜索失败: {e}")
        return []


def _extract_final_text(client, response, original_prompt):
    """
    处理 Claude 的多轮 tool_use 响应。
    Claude 搜索时会先调用 web_search（tool_use），然后收到结果（tool_result），
    可能需要多轮才返回最终文本。API 会自动处理 web_search 的调用和结果，
    我们只需要从最终响应中提取 text 块。
    """
    # web_search 工具由 Anthropic 服务端自动执行，
    # 最终 response 的 stop_reason 应该是 "end_turn"
    # 直接从 content 里提取所有 text
    texts = []
    for block in response.content:
        if hasattr(block, 'text'):
            texts.append(block.text)
    return "\n".join(texts)


def _parse_claude_contacts(text, domain=""):
    """从 Claude 返回的文本中解析联系人 JSON"""
    import json as _json

    # 尝试提取 JSON 数组
    # 先试 ```json ... ``` 包裹的
    m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        # 直接找 [ ... ]
        m = re.search(r'\[[\s\S]*\]', text)
        if m:
            raw = m.group(0)
        else:
            return []

    try:
        items = _json.loads(raw)
    except _json.JSONDecodeError:
        # 尝试修复常见问题
        try:
            raw_fixed = re.sub(r',\s*}', '}', raw)
            raw_fixed = re.sub(r',\s*\]', ']', raw_fixed)
            items = _json.loads(raw_fixed)
        except Exception:
            print(f"    Claude返回JSON解析失败")
            return []

    if not isinstance(items, list):
        return []

    contacts = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue

        email = (item.get("email") or "").strip().lower()
        whatsapp = _clean_phone(item.get("whatsapp") or "")
        phone = _clean_phone(item.get("phone") or "")

        # 至少要有一个联系方式才值得保留
        if not email and not whatsapp and not phone and not item.get("linkedin"):
            continue

        # 判断邮箱状态
        email_status = ""
        if email:
            local = email.split("@")[0] if "@" in email else ""
            DEPT = ["info", "sales", "contact", "orders", "admin", "support",
                    "hello", "team", "office", "general", "enquiries"]
            if local in DEPT or not ("." in local or "_" in local):
                email_status = "Claude搜索（部门邮箱）"
            else:
                email_status = "Claude搜索（实名邮箱）"

        source_detail = item.get("source_detail", "Claude Web Search")

        contacts.append({
            "name":          (item.get("name") or "").strip(),
            "position":      (item.get("position") or "").strip(),
            "email":         email,
            "phone":         phone or whatsapp,
            "whatsapp":      whatsapp or phone,
            "linkedin":      (item.get("linkedin") or "").strip(),
            "email_status":  email_status,
            "email_guesses": [],
            "source":        f"Claude AI搜索·{source_detail}",
        })

    return contacts


# ============================================================
# NEW: 统一优先级评分
# ============================================================
def _priority_score(c):
    """优先级评分 - 有增强模块时使用增强版"""
    if ENHANCED_VERIFIER_AVAILABLE:
        return enhanced_priority_score(c)
    # 回退到原始逻辑
    score  = 0
    status = c.get("email_status","")
    email  = c.get("email","")
    if "❌" in status:
        return -100
    if c.get("whatsapp") or c.get("phone"):
        score += 200
    if email:
        if any(x in status for x in ["✅","verified","FDA","Hunter.io","WHOIS直接","官网直接"]):
            score += 100
        elif "推算" in status and "✅" in status:
            score += 70
        elif "⚠️" in status:
            score += 25
        elif "推算" in status:
            score += 40
        elif "部门" in status:
            score += 30
        else:
            score += 20
        if "Claude搜索（实名邮箱）" in status:
            score += 80
    if c.get("linkedin"):
        score += 15
    title = (c.get("position") or c.get("title") or "").lower()
    if any(kw in title for kw in ["purchas","procure","supply","sourc","buyer","import","category","logistics","head of"]):
        score += 30
    if any(kw in title for kw in ["director","manager","ceo","owner","president","founder"]):
        score += 15
    score += c.get("_apollo_score",0) * 2
    return score


# ============================================================
# 1. LinkedIn搜索
# ============================================================
def _parse_linkedin_title(title):
    clean = re.sub(r'\s*[\|]\s*LinkedIn.*', '', title).strip()
    clean = re.sub(r'\s*-\s*LinkedIn.*', '', clean).strip()
    name, position = "", ""
    for sep in [" - ", " \u2013 ", " \u2014 "]:
        if sep in clean:
            parts    = clean.split(sep, 1)
            name     = parts[0].strip()
            position = re.sub(r'\s+at\s+.+$', '', parts[1], flags=re.IGNORECASE).strip()
            break
    if not name:
        name = clean
    return name, position

def search_linkedin_contacts(company_name):
    contacts_t1, contacts_t2 = [], []
    queries_t1 = [
        f'site:linkedin.com/in "{company_name}" purchasing OR procurement OR buyer',
        f'site:linkedin.com/in "{company_name}" supply chain OR sourcing',
    ]
    queries_t2 = [
        f'site:linkedin.com/in "{company_name}" owner OR president OR CEO OR "general manager"',
        f'site:linkedin.com/in "{company_name}" director OR manager',
    ]
    def _run(query, bucket, keywords):
        try:
            results = _safe_ddg(query, max_results=6, timeout=8)
            for r in results:
                url   = r.get("href","")
                title = r.get("title","")
                body  = r.get("body","")
                if "linkedin.com/in" not in url:
                    continue
                name, position = _parse_linkedin_title(title)
                if not name or len(name) < 3 or len(name) > 50:
                    continue
                combined = (position + " " + body).lower()
                if not any(kw in combined for kw in keywords):
                    continue
                all_names = [c["name"].lower() for c in contacts_t1 + contacts_t2]
                if name.lower() in all_names:
                    continue
                bucket.append({"name":name,"position":position,"linkedin":url,"email":"","phone":"","whatsapp":"","email_status":"","email_guesses":[],"source":"LinkedIn/DDG"})
            time.sleep(1.5)
        except Exception as e:
            print(f"    LinkedIn出错：{e}")
    for q in queries_t1:
        _run(q, contacts_t1, BUYER_TITLES)
        if len(contacts_t1) >= 3: break
    if len(contacts_t1) < 3:
        for q in queries_t2:
            _run(q, contacts_t2, EXEC_TITLES)
            if len(contacts_t2) >= 2: break
    print(f"    LinkedIn: 采购{len(contacts_t1)}人 + 高管{len(contacts_t2)}人")
    return (contacts_t1 + contacts_t2)[:5]


# ============================================================
# 2. 域名查找
# ============================================================
def _validate_domain(domain, company_name):
    if any(sig in domain.lower() for sig in WRONG_DOMAIN_SIGNALS):
        return False
    try:
        for scheme in ["https","http"]:
            try:
                resp = requests.get(f"{scheme}://{domain}", headers=HEADERS, timeout=6)
                if resp.status_code not in (200,301,302,304):
                    continue
                page = resp.text.lower()
                keywords = re.sub(r'\b(inc|llc|ltd|corp|co|medical|surgical|depot|international|service|services|trade|health)\b','',company_name.lower()).split()
                keywords = [k for k in keywords if len(k) > 2]
                if not keywords: return True
                return sum(1 for kw in keywords if kw in page) > 0
            except Exception:
                continue
    except Exception:
        pass
    return True

def find_company_domain(company_name):
    try:
        results = _safe_ddg(f'"{company_name}" official website medical', max_results=8, timeout=8)
        for r in results:
            url = r.get("href","")
            m   = re.search(r'https?://(?:www\.)?([^/]+)', url)
            if not m: continue
            domain = m.group(1).lower()
            if any(s in domain for s in DOMAIN_SKIP): continue
            if _validate_domain(domain, company_name): return domain
        time.sleep(1)
    except Exception as e:
        print(f"    域名搜索失败：{e}")
    return ""


# ============================================================
# 3. WHOIS
# ============================================================
def query_whois(domain):
    if not WHOIS_AVAILABLE or not domain:
        return []
    contacts = []
    try:
        w = whois_lib.whois(domain)
        emails = w.get("emails",[])
        if isinstance(emails, str): emails = [emails]
        PRIVACY_DOMAINS = ["whoisguard","privacy","protect","proxy","redacted","cscglobal","domainsbyproxy","perfectprivacy","contactprivacy","withheldforprivacy","registrar-servers","godaddy","networksolutions","namecheap","domainabuse","abuse","noreply","hostmaster","postmaster","webmaster","tucows","registrar","enom","uniregistry","101domain","markmonitor","safenames","register.com","1and1"]
        emails = [e.lower() for e in (emails or []) if e and not any(kw in e.lower() for kw in PRIVACY_DOMAINS)]
        name = w.get("name","") or w.get("registrant_name","") or ""
        org  = w.get("org","")  or w.get("registrant_org","")  or ""
        COMPANY_SUFFIXES = ["llc","inc","ltd","corp","co.","company","appliances","medical","surgical","international","depot","supplies","group","holdings"]
        PRIVACY_PLACEHOLDERS = ["registration private","whois privacy","data protected","privacy protect","contact privacy","redacted for privacy","withheld for privacy","not disclosed","private registration","domain privacy","identity protection"]
        is_privacy   = any(pp in name.lower() for pp in PRIVACY_PLACEHOLDERS)
        is_company   = (not name or is_privacy or any(s in name.lower() for s in COMPANY_SUFFIXES) or name.isupper())
        person_name  = "" if is_company else name.strip()
        domain_emails = [e for e in emails if domain in e]
        other_emails  = [e for e in emails if domain not in e]
        best_emails   = (domain_emails + other_emails)[:2]
        if best_emails:
            for email in best_emails:
                email_domain = email.split("@")[-1] if "@" in email else ""
                is_co_email  = (domain in email) or (email_domain == domain)
                dname = person_name or (org if org and not any(s in org.lower() for s in ["llc","inc","ltd","corp","appliances","medical"]) else "") or "域名注册人"
                contacts.append({"name":dname,"position":"域名注册人 (Owner/Admin)","email":email,"phone":"","whatsapp":"","linkedin":"","email_status":"WHOIS直接获取（公司邮箱）" if is_co_email else "WHOIS直接获取（待验证）","email_guesses":[],"source":"WHOIS"})
        elif person_name and not is_privacy:
            contacts.append({"name":person_name,"position":"域名注册人 (Owner/Admin)","email":"","phone":"","whatsapp":"","linkedin":"","email_status":"","email_guesses":[],"source":"WHOIS"})
        print(f"    WHOIS: 注册人={person_name or '隐藏'}, 邮箱={emails or '隐藏'}")
    except Exception as e:
        print(f"    WHOIS失败：{e}")
    return contacts


# ============================================================
# 4. FDA数据库
# ============================================================
def query_fda(company_name):
    contacts = []
    name_clean = company_name.replace('"','')
    keywords   = [w for w in name_clean.split() if len(w) > 2][:3]
    search_queries = [q for q in [
        f'registration.owner_operator.firm_name:"{name_clean}"',
        f'registration.owner_operator.firm_name:{"+".join(keywords)}' if keywords else None,
        f'registration.owner_operator.firm_name:{keywords[0]}' if keywords else None,
    ] if q]
    for search_q in search_queries:
        try:
            resp = requests.get("https://api.fda.gov/device/registrationlisting.json", params={"search":search_q,"limit":5}, timeout=12)
            if resp.status_code != 200: continue
            data = resp.json()
            if "error" in data: continue
            results = data.get("results",[])
            if not results: continue
            print(f"    FDA: 搜到{len(results)}条，筛选中...")
            for item in results:
                reg   = item.get("registration",{})
                owner = reg.get("owner_operator",{})
                agent = reg.get("us_agent",{})
                firm_name = owner.get("firm_name","")
                firm_lower = firm_name.lower()
                kw_hits  = sum(1 for kw in keywords if kw.lower() in firm_lower)
                required = len(keywords) if len(keywords) <= 2 else len(keywords) - 1
                if kw_hits < required: continue
                corr      = owner.get("official_correspondent",{})
                corr_name = f"{corr.get('first_name','').strip()} {corr.get('last_name','').strip()}".strip()
                agent_name  = agent.get("name","").strip()
                agent_email = agent.get("email_address","").strip().lower()
                agent_biz   = agent.get("business_name","").strip()
                city    = reg.get("city","")
                state   = reg.get("state_code","")
                country = reg.get("iso_country_code","")
                addr    = ", ".join(filter(None,[city,state,country]))
                src     = f"FDA官方注册 ({firm_name}, {addr})" if addr else f"FDA官方注册 ({firm_name})"
                if corr_name and len(corr_name) > 3:
                    contacts.append({"name":corr_name,"position":"FDA注册官方联系人","email":"","phone":"","whatsapp":"","linkedin":"","email_status":"","email_guesses":[],"source":src})
                if agent_email and agent_name:
                    GENERIC = ["fdabasics","registrar","compliance","usfda","fdaconsult","regulatory","qmscertifications"]
                    if not any(kw in agent_email for kw in GENERIC):
                        contacts.append({"name":agent_name,"position":f"FDA美国代理 ({agent_biz})" if agent_biz else "FDA美国代理","email":agent_email,"phone":"","whatsapp":"","linkedin":"","email_status":"FDA数据库直接获取","email_guesses":[],"source":src})
                if len(contacts) >= 3: break
            if contacts: break
        except Exception as e:
            print(f"    FDA搜索失败: {e}")
    print(f"    FDA: 找到 {len(contacts)} 个联系人")
    return contacts


# ============================================================
# 5. SMTP邮箱验证
# ============================================================
def _get_mx_record(domain):
    try:
        import dns.resolver
        mx_records = dns.resolver.resolve(domain,'MX')
        return str(sorted(mx_records, key=lambda r: r.preference)[0].exchange).rstrip('.')
    except Exception:
        return domain

def verify_email_smtp(email, sender="verify@kingphar.com", timeout=8):
    """25端口封禁时自动跳过SMTP验证"""
    if _PORT25_BLOCKED:
        return None  # 直接跳过，不浪费时间
    try:
        domain  = email.split("@")[1]
        mx_host = _get_mx_record(domain)
        with smtplib.SMTP(timeout=timeout) as smtp:
            smtp.connect(mx_host, 25)
            smtp.helo("kingphar.com")
            smtp.mail(sender)
            code, _ = smtp.rcpt(email)
            if code == 250: return True
            elif code in (550,551,552,553): return False
            elif code in (421,450,451,452): return "busy"
            else: return None
    except Exception:
        return None


def verify_contact_emails(contacts, max_verify=15):
    """
    混合验证：免费API为主，SMTP作后备
    只验证推算邮箱和不确定来源的邮箱
    """
    verified_count = 0
    for c in contacts:
        if verified_count >= max_verify:
            break
        email  = c.get("email","")
        status = c.get("email_status","")
        if not email:
            continue

        # 跳过已标记的（验证过/失败/catch-all/域名无效）
        if "✅" in status or "❌" in status or "⚠️" in status:
            continue

        domain = email.split("@")[-1]

        # Step 1: 域名级预筛（免费，即时，不消耗 API 配额）
        valid, label = check_domain_quality(domain)
        if not valid:
            c["email_status"] = (status + f" {label}").strip()
            continue

        # Step 2: 只对"推算"邮箱做 API 验证（节省配额）
        # 非推算邮箱（官网直接/FDA/WHOIS/Claude搜索等）默认可靠
        if "推算" not in status:
            continue

        # Step 3: 免费API验证推算邮箱
        result = try_free_email_verify(email)
        verified_count += 1

        if result == "valid":
            c["email_status"] = (status + " ✅API验证通过").strip()
        elif result == "invalid":
            # 尝试备选邮箱
            guesses = c.get("email_guesses", [])
            found_alt = False
            for alt in guesses:
                if verified_count >= max_verify:
                    break
                alt_result = try_free_email_verify(alt)
                verified_count += 1
                if alt_result == "valid":
                    c["email"] = alt
                    c["email_status"] = "推算备选 ✅API验证通过"
                    c["email_guesses"] = [g for g in guesses if g != alt]
                    found_alt = True
                    break
            if not found_alt:
                c["email_status"] = (status + " ❌API验证失败").strip()
        elif result == "risky":
            c["email_status"] = (status + " ⚠️Catch-all域名（不可靠）").strip()
        # unknown → 不改状态，保持原样

        time.sleep(0.3)  # eva API 频率限制（实际不严格）

    return contacts


# ============================================================
# 6. 官网爬取
# ============================================================
def _parse_team_page(soup, domain):
    members = []
    for heading in soup.find_all(["h2","h3","h4"]):
        name = heading.get_text(strip=True)
        if not (3 < len(name) < 50): continue
        if any(ch.isdigit() for ch in name): continue
        position = ""
        next_el  = heading.find_next_sibling()
        if next_el and next_el.name in ["p","span","div"]:
            pos_text = next_el.get_text(strip=True)
            if len(pos_text) < 80: position = pos_text
        combined = (name + " " + position).lower()
        if any(kw in combined for kw in BUYER_TITLES + EXEC_TITLES):
            members.append({"name":name,"position":position,"linkedin":"","email":"","phone":"","whatsapp":"","email_status":"","email_guesses":[],"source":f"官网团队页({domain})"})
    return members[:5]

def scrape_website_contacts(company_name, domain=""):
    contacts, emails = [], set()
    if not domain: return contacts, emails
    pages = ["/contact","/contact-us","/about","/about-us","/team","/our-team",
             "/staff","/people","/leadership","/management","/company",
             "/imprint","/impressum","/legal-notice","/kontakt"]  # 欧洲公司法律要求公示
    for path in [""] + pages:
        # 优先 https，失败才试 http
        for scheme in ["https","http"]:
            try:
                url  = f"{scheme}://{domain}{path}"
                resp = requests.get(url, headers=HEADERS, timeout=8)
                if resp.status_code != 200: continue
                soup = BeautifulSoup(resp.text, "html.parser")

                # 提取纯文本中的邮箱
                text = soup.get_text(separator=" ", strip=True)
                found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
                for e in found:
                    e = e.lower()
                    if any(s in e for s in ["example","noreply","privacy","@w3","@schema"]): continue
                    if domain in e or "." in e.split("@")[-1]: emails.add(e)

                # 提取 mailto: 链接（有些邮箱不在可见文本里）
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if href.startswith("mailto:"):
                        mailto_email = href.replace("mailto:","").split("?")[0].strip().lower()
                        if re.match(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', mailto_email):
                            if not any(s in mailto_email for s in ["example","noreply","privacy"]):
                                emails.add(mailto_email)

                if any(kw in path for kw in ["/team","/staff","/people","/leadership","/management"]):
                    for m in _parse_team_page(soup, domain):
                        if not any(c["name"].lower() == m["name"].lower() for c in contacts):
                            contacts.append(m)
                time.sleep(0.5)
                break  # https 成功就不试 http
            except Exception:
                continue
    return contacts, emails


# ============================================================
# 7. 邮箱推算
# ============================================================
def generate_email_guesses(name, domain):
    if not name or not domain: return []
    parts = name.lower().strip().split()
    if len(parts) < 2: return []
    first = re.sub(r'[^a-z]','',parts[0])
    last  = re.sub(r'[^a-z]','',parts[-1])
    if not first or not last: return []
    f = first[0]
    return [p.format(first=first,last=last,f=f,domain=domain) for p in EMAIL_PATTERNS]

def detect_email_pattern(known_emails, domain):
    for email in known_emails:
        if "@" + domain not in email: continue
        local = email.split("@")[0]
        if "." in local and len(local.split(".")) == 2:
            p = local.split(".")
            if len(p[0]) > 1 and len(p[1]) > 1: return "{first}.{last}@{domain}"
        if len(local) <= 2: return "{f}{last}@{domain}"
        if "_" in local: return "{first}_{last}@{domain}"
    return None

def enrich_with_email_guesses(contacts, domain, known_emails, hunter_format=None):
    """推算邮箱：有格式时只生成最佳1个，无格式时只保留top 2"""
    if not domain: return contacts

    # 域名预筛：无MX → 标记所有无邮箱的联系人并跳过推算
    if not domain_has_mx(domain):
        print(f"    ⚠️ {domain} 无MX记录，跳过邮箱推算")
        for c in contacts:
            if not c.get("email"):
                c["email_status"] = (c.get("email_status","") + " ⚠️域名无MX记录").strip()
        return contacts

    detected = hunter_format or detect_email_pattern(known_emails, domain)
    if detected: print(f"    邮箱格式锁定：{detected}")

    for c in contacts:
        if c.get("email"): continue
        name = c.get("name","")
        guesses = generate_email_guesses(name, domain)
        if not guesses: continue

        if detected:
            parts = name.lower().strip().split()
            if len(parts) >= 2:
                first = re.sub(r'[^a-z]','',parts[0])
                last  = re.sub(r'[^a-z]','',parts[-1])
                f     = first[0] if first else ""
                if first and last:
                    best   = detected.format(first=first,last=last,f=f,domain=domain)
                    source = "Hunter格式API" if hunter_format else "域名格式推断"
                    c["email"]         = best
                    c["email_status"]  = f"推算（{source}，准确率较高）"
                    c["email_guesses"] = []  # 已锁定格式，不给备选
                    continue

        # 无格式时：只保留最常见的2个模式
        c["email"]         = guesses[0]  # first.last@
        c["email_status"]  = "推算（常见格式，需验证）"
        c["email_guesses"] = guesses[1:3]  # firstlast@ 和 flast@

    return contacts


# ============================================================
# 8. Hunter.io 格式检测
# ============================================================
def query_hunter_email_format(domain):
    if not HUNTER_KEYS or not domain: return None
    HUNTER_FORMAT_MAP = {"first.last":"{first}.{last}@{domain}","first":"{first}@{domain}","last.first":"{last}.{first}@{domain}","flast":"{f}{last}@{domain}","firstlast":"{first}{last}@{domain}","first_last":"{first}_{last}@{domain}","f.last":"{f}.{last}@{domain}","lastfirst":"{last}{first}@{domain}"}
    for key in HUNTER_KEYS:
        try:
            resp = requests.get("https://api.hunter.io/v2/email-format", params={"domain":domain,"api_key":key}, timeout=8)
            if resp.status_code != 200: continue
            data = resp.json()
            errors = data.get("errors",[])
            if errors and errors[0].get("id") in ("usage_limit_reached","quota_exceeded"): continue
            formats = data.get("data",{}).get("formats",[])
            if not formats: return None
            formats.sort(key=lambda x: x.get("percentage",0), reverse=True)
            top_format = formats[0].get("format","")
            pct = formats[0].get("percentage",0)
            mapped = HUNTER_FORMAT_MAP.get(top_format)
            if mapped:
                print(f"    Hunter格式API: {top_format} ({pct}%) → {mapped}")
                return mapped
            return None
        except Exception as e:
            print(f"    Hunter格式API失败：{e}")
    return None


# ============================================================
# 9. Hunter.io 联系人搜索
# ============================================================
def query_hunter(domain):
    if not HUNTER_KEYS or not domain: return []
    BROAD = BUYER_TITLES + ["owner","president","ceo","founder","co-founder","general manager","director","manager","vice president","vp","chief","head of"]
    for key in HUNTER_KEYS:
        try:
            resp = requests.get("https://api.hunter.io/v2/domain-search", params={"domain":domain,"api_key":key,"limit":10,"department":"management,purchasing,operations"}, timeout=10)
            data = resp.json()
            errors = data.get("errors",[])
            if errors and errors[0].get("id") in ("usage_limit_reached","quota_exceeded"):
                print(f"    Hunter.io Key额度用完，切换...")
                continue
            persons = data.get("data",{}).get("emails",[])
            filtered = [p for p in persons if any(kw in (p.get("position") or "").lower() for kw in BUYER_TITLES)]
            if not filtered: filtered = [p for p in persons if any(kw in (p.get("position") or "").lower() for kw in BROAD)]
            if not filtered: filtered = persons[:5]
            contacts = []
            for p in filtered[:5]:
                name = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
                contacts.append({"name":name,"position":p.get("position",""),"email":p.get("value",""),"phone":"","whatsapp":"","linkedin":p.get("linkedin",""),"email_status":f"Hunter.io({p.get('confidence',0)}%)","email_guesses":[],"source":"Hunter.io"})
            if contacts: return contacts
            return []
        except Exception as e:
            print(f"    Hunter.io失败：{e}")
    return []


# ============================================================
# 10b. DDG/Google 直接搜索邮箱（零成本，高效）
# ============================================================

def search_email_by_ddg(company_name: str, domain: str, country: str = "") -> list:
    """
    用 DDG 搜索引擎直接找公司邮箱。
    搜索策略：
      1. "@domain.com" + 职位关键词
      2. company_name + "email" + 职位
      3. site:domain + contact/team page
    完全免费，不消耗任何 API 额度。
    """
    if not domain and not company_name:
        return []

    results = []
    found_emails = set()

    # 策略1：直接搜域名邮箱
    queries = []
    if domain:
        queries += [
            f'"@{domain}" purchasing OR procurement OR "supply chain"',
            f'"@{domain}" director OR manager OR buyer',
            f'site:{domain} email contact purchasing',
        ]
    # 策略2：公司名+邮箱关键词
    queries += [
        f'"{company_name}" email purchasing procurement {country}',
        f'"{company_name}" contact buyer "medical" email',
    ]

    SKIP = ["example","noreply","privacy","support","abuse","spam",
            "test","no-reply","@example","@test"]

    for query in queries[:4]:
        try:
            ddg_results = _safe_ddg(query, max_results=8, timeout=8)
            for r in ddg_results:
                text = (r.get("title","") + " " + r.get("body","")).lower()
                url  = r.get("href","")

                # 从摘要里提取邮箱
                emails_found = re.findall(
                    r'[a-zA-Z0-9][a-zA-Z0-9._%+\-]{1,30}@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}',
                    text
                )
                for email in emails_found:
                    email = email.lower()
                    if any(s in email for s in SKIP):
                        continue
                    if email in found_emails:
                        continue
                    if domain and domain not in email:
                        continue  # 只要匹配域名的
                    found_emails.add(email)

                    # 判断是否是真人邮箱（非部门邮箱）
                    local = email.split("@")[0]
                    DEPT = ["info","sales","contact","orders","admin","support",
                            "hello","team","office","general","enquiries","enquiry"]
                    is_personal = (
                        "." in local or "_" in local or
                        (len(local) > 3 and local not in DEPT)
                    )
                    results.append({
                        "name":     "",
                        "position": "",
                        "email":    email,
                        "phone":    "", "whatsapp": "", "linkedin": "",
                        "email_status": "DDG直接搜索（官网/目录）" if is_personal else "DDG搜索（部门邮箱）",
                        "email_guesses": [],
                        "source":   f"DDG搜索·{url[:50]}",
                    })
            time.sleep(1.2)
        except Exception:
            continue

    # 直接爬取搜到的页面里的邮箱
    if domain:
        CONTACT_PAGES = [
            f"https://{domain}/contact",
            f"https://{domain}/contact-us",
            f"https://{domain}/about",
            f"https://{domain}/team",
            f"https://{domain}/en/contact",
            f"https://{domain}/de/kontakt",
        ]
        for page_url in CONTACT_PAGES[:4]:
            try:
                resp = requests.get(page_url, headers=HEADERS, timeout=8, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                # 找邮箱（包括 mailto: 里的）
                all_found = set(re.findall(
                    r'[a-zA-Z0-9][a-zA-Z0-9._%+\-]{1,30}@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}',
                    resp.text
                ))
                # 也找 mailto:
                mailto = set(re.findall(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6})',
                    resp.text, re.IGNORECASE))
                all_found |= mailto

                for email in all_found:
                    email = email.lower()
                    if any(s in email for s in SKIP): continue
                    if email in found_emails: continue
                    if domain and domain not in email: continue
                    found_emails.add(email)
                    local = email.split("@")[0]
                    DEPT = ["info","sales","contact","orders","admin","support",
                            "hello","team","office","general","enquiries","noreply"]
                    is_personal = "." in local or "_" in local
                    status = "官网直接获取（个人邮箱）" if is_personal else "官网直接获取（部门邮箱）"
                    results.append({
                        "name": "", "position": "",
                        "email": email, "phone": "", "whatsapp": "", "linkedin": "",
                        "email_status": status,
                        "email_guesses": [],
                        "source": f"官网·{page_url}",
                    })
                time.sleep(0.5)
            except Exception:
                continue

    print(f"    DDG直搜: 找到 {len(results)} 个邮箱")
    return results


# ============================================================
# 10. AI代理搜索
# ============================================================
def ai_agent_find_contacts(company_name, country="", company_url=""):
    results = []
    domain  = ""
    if company_url:
        m = re.search(r'https?://(?:www\.)?([^/]+)', company_url)
        if m: domain = m.group(1)
    if not domain:
        r1 = _safe_ddg(f'"{company_name}" official website {country}', max_results=5)
        blacklist = ["linkedin","facebook","wikipedia","zoominfo","bloomberg","dnb.com","crunchbase","glassdoor","amazon"]
        kw = company_name.lower().split()[0][:6]
        for r in r1:
            url = r.get("href","")
            if any(b in url.lower() for b in blacklist): continue
            if kw in url.lower():
                m = re.search(r'https?://(?:www\.)?([^/]+)', url)
                if m: domain = m.group(1); break
        if not domain and r1:
            for r in r1:
                url = r.get("href","")
                if not any(b in url.lower() for b in blacklist):
                    m = re.search(r'https?://(?:www\.)?([^/]+)', url)
                    if m: domain = m.group(1); break
    if domain:
        base  = f"https://{domain}"
        pages = ["/contact","/contact-us","/about/team","/team","/about-us","/our-team","/staff","/people",
                 "/imprint","/impressum","/legal-notice"]
        for path in pages:
            try:
                resp = requests.get(base + path, headers={"User-Agent":"Mozilla/5.0"}, timeout=10, verify=False)
                if resp.status_code != 200: continue
                html = resp.text
                for tag in ["script","style","nav","footer"]:
                    html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", html, flags=re.DOTALL|re.IGNORECASE)
                text = re.sub(r"<[^>]+>"," ",html)
                text = re.sub(r"\s+"," ",text).strip()
                if len(text) < 100: continue
                emails_found = list(set(re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', text)))
                dept_kw = ["purchasing","procurement","supply","sourcing","sales","import","medical","info","contact","orders"]
                for e in emails_found:
                    local = e.split("@")[0].lower()
                    if domain.split(".")[0][:4] in e.lower() or any(k in local for k in dept_kw):
                        results.append({"name":"","position":"部门邮箱","email":e,"phone":"","whatsapp":"","source":f"官网{path}","email_status":"官网直接获取","priority":2})
                name_patterns = [r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[,\u2013-]\s*([A-Za-z][A-Za-z\s]{5,40})',r'<(?:h[2-4]|strong)[^>]*>([A-Z][a-z]+\s+[A-Z][a-z]+)</']
                for pat in name_patterns:
                    for m in re.finditer(pat, text if "h2" not in pat else html):
                        name = m.group(1).strip()
                        pos  = m.group(2).strip() if m.lastindex >= 2 else ""
                        buy_kw = ["purchas","procure","supply","sourc","buyer","import","head of"]
                        if any(k in pos.lower() for k in buy_kw) or not pos:
                            results.append({"name":name,"position":pos,"email":"","phone":"","whatsapp":"","source":f"官网{path}","email_status":"待推算","priority":1})
                if results: break
            except Exception:
                continue
    li_queries = [
        f'site:linkedin.com/in "{company_name}" purchasing procurement buyer',
        f'site:linkedin.com/in "{company_name}" supply chain sourcing import',
    ]
    buy_titles = ["purchas","procure","supply chain","sourc","buyer","import","head of","category","commercial","acquisition"]
    for q in li_queries[:2]:
        li_res = _safe_ddg(q, max_results=5)
        for r in li_res:
            title   = r.get("title","")
            snippet = r.get("body","")
            li_url  = r.get("href","")
            name_m  = re.match(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})', title)
            if not name_m: continue
            name  = name_m.group(1)
            pos_m = re.search(r'((?:' + '|'.join(buy_titles) + r')[^·|\u2013\n]{0,50})', snippet, re.IGNORECASE)
            pos   = pos_m.group(1).strip() if pos_m else ""
            if pos or any(k in snippet.lower() for k in buy_titles):
                results.append({"name":name,"position":pos[:60],"email":"","phone":"","whatsapp":"","linkedin":li_url,"source":"LinkedIn","email_status":"待推算","priority":1})
    if domain:
        hunter_fmt = None
        try: hunter_fmt = query_hunter_email_format(domain)
        except Exception: pass
        for c in [x for x in results if x.get("name") and not x.get("email")][:5]:
            guesses = generate_email_guesses(c["name"], domain)
            c["email_guesses"] = guesses[:4]
            if guesses:
                c["email"]        = guesses[0]
                c["email_status"] = "推算（通用格式）"
    seen_names, seen_emails = set(), set()
    unique = []
    for c in results:
        nk = c.get("name","").lower()[:15]
        ek = c.get("email","").lower()
        if (nk and nk in seen_names) or (ek and ek in seen_emails): continue
        if nk: seen_names.add(nk)
        if ek: seen_emails.add(ek)
        unique.append(c)
    return unique[:6]


# ============================================================
# 11. 去重合并（支持 phone / whatsapp）
# ============================================================
def deduplicate(contacts):
    merged = {}
    for c in contacts:
        name  = c.get("name","").lower().strip()
        email = c.get("email","").lower().strip()
        key   = name if (name and len(name) > 2) else email
        if not key: key = str(len(merged))
        if key in merged:
            ex = merged[key]
            if email and not ex.get("email"):
                ex["email"]         = email
                ex["email_status"]  = c.get("email_status","")
                ex["email_guesses"] = c.get("email_guesses",[])
            if c.get("linkedin") and not ex.get("linkedin"): ex["linkedin"] = c["linkedin"]
            if c.get("position") and not ex.get("position"): ex["position"] = c["position"]
            if c.get("phone")    and not ex.get("phone"):    ex["phone"]    = c["phone"]
            if c.get("whatsapp") and not ex.get("whatsapp"): ex["whatsapp"] = c["whatsapp"]
        else:
            entry = dict(c)
            entry.setdefault("phone","")
            entry.setdefault("whatsapp","")
            merged[key] = entry
    results = list(merged.values())
    results.sort(key=_priority_score, reverse=True)
    return results


# ============================================================
# 主函数
# ============================================================
def find_contacts(company_name, country="USA", company_url="", max_contacts=8):
    print(f"\n  👤 挖掘联系人：{company_name}")
    all_contacts, all_emails = [], set()

    # Step 0a: 跨境搜
    try:
        from kuajings_client import enrich_contacts_from_kuajings, _is_configured
        if _is_configured():
            kj_list = enrich_contacts_from_kuajings(company_name=company_name, website=company_url)
            for c in kj_list:
                all_contacts.append({"name":c.get("name",""),"position":c.get("title",""),"email":c.get("email",""),"phone":c.get("phone",""),"whatsapp":c.get("phone",""),"linkedin":c.get("linkedin",""),"email_status":"跨境搜实名","email_guesses":[],"source":"跨境搜"})
                if c.get("email"): all_emails.add(c["email"])
            if kj_list: print(f"    跨境搜：{len(kj_list)} 个联系人")
    except Exception:
        pass

    # Step 0b: WhatsApp 抓取
    wa_numbers = []
    if company_url:
        print(f"    📱 抓取 WhatsApp 号码...")
        try: wa_numbers = scrape_whatsapp(company_url)
        except Exception as e: print(f"    WhatsApp 抓取失败: {e}")

    # Step 0c: Apollo.io
    print(f"    🚀 Apollo.io 查询...")
    domain_for_apollo = ""
    if company_url:
        m = re.search(r'https?://(?:www\.)?([^/]+)', company_url)
        if m: domain_for_apollo = m.group(1)
    apollo_contacts = query_apollo(company_name, country, domain_for_apollo)
    all_contacts.extend(apollo_contacts)
    for c in apollo_contacts:
        if c.get("email"): all_emails.add(c["email"])

    # ★ Step 0d: Claude AI 联网搜索（WhatsApp + 实名联系人）
    print(f"    🧠 Claude AI 联网搜索...")
    try:
        claude_contacts = claude_search_contacts(company_name, country, domain_for_apollo, company_url)
        for c in claude_contacts:
            if c.get("whatsapp") and c["whatsapp"] not in wa_numbers:
                wa_numbers.append(c["whatsapp"])
            if c.get("email"):
                all_emails.add(c["email"])
        all_contacts.extend(claude_contacts)
    except Exception as ce:
        print(f"    Claude搜索跳过: {ce}")

    # ★ NEW Step 0e: 增强数据源（展会/B2B/政府采购/FB/Google Dork/行业协会）
    pdf_urls = []
    if ENHANCED_SOURCES_AVAILABLE:
        print(f"    🌐 增强数据源搜索...")
        try:
            enhanced_contacts, enhanced_wa = run_all_enhanced_sources(
                company_name, domain_for_apollo, country, company_url
            )
            pdf_urls = [c.pop("_pdf_url") for c in enhanced_contacts if c.get("_pdf_url")]
            all_contacts.extend(enhanced_contacts)
            for c in enhanced_contacts:
                if c.get("email"): all_emails.add(c["email"])
            wa_numbers.extend([n for n in enhanced_wa if n not in wa_numbers])
        except Exception as ee:
            print(f"    增强数据源跳过: {ee}")

    # Step A: AI代理
    print(f"    🤖 AI代理搜索联系人...")
    try:
        ai_contacts = ai_agent_find_contacts(company_name, country, company_url)
        for c in ai_contacts:
            e = c.get("email","").lower()
            if e and e not in all_emails: all_emails.add(e)
            all_contacts.append(c)
        if ai_contacts: print(f"    AI代理：{len(ai_contacts)} 个线索")
    except Exception as ae:
        print(f"    AI代理跳过: {ae}")

    # Step Ab: DDG直接搜索邮箱（零成本，优先于推算）
    print(f"    🔍 DDG直搜邮箱...")
    try:
        ddg_emails = search_email_by_ddg(company_name, domain_for_apollo, country)
        for c in ddg_emails:
            e = c.get("email","").lower()
            if e and e not in all_emails:
                all_emails.add(e)
                all_contacts.append(c)
        if ddg_emails: print(f"    DDG直搜：{len(ddg_emails)} 个邮箱")
    except Exception as de:
        print(f"    DDG直搜跳过: {de}")

    # Step 1: LinkedIn
    print(f"    🔍 LinkedIn搜索...")
    all_contacts.extend(search_linkedin_contacts(company_name))
    time.sleep(1)

    # Step 2: 域名
    domain = domain_for_apollo
    if not domain:
        print(f"    🌐 搜索官网域名...")
        domain = find_company_domain(company_name)
        if domain: print(f"    域名：{domain}")
        time.sleep(1)

    # Step 3: WHOIS
    if domain:
        print(f"    📋 WHOIS查询...")
        whois_contacts = query_whois(domain)
        all_contacts.extend(whois_contacts)
        for c in whois_contacts:
            if c.get("email"): all_emails.add(c["email"])

    # Step 4: FDA
    print(f"    🏥 FDA数据库查询...")
    fda_contacts = query_fda(company_name)
    all_contacts.extend(fda_contacts)
    for c in fda_contacts:
        if c.get("email"): all_emails.add(c["email"])

    # Step 5: 官网
    if domain:
        print(f"    🔎 爬取官网...")
        web_contacts, web_emails = scrape_website_contacts(company_name, domain)
        print(f"    官网：邮箱{len(web_emails)}个，成员{len(web_contacts)}个")
        all_contacts.extend(web_contacts)
        all_emails.update(web_emails)
        time.sleep(1)

    # ★ NEW Step 5b: 深度爬取（Sitemap/PDF/Wayback/robots.txt）
    if DEEP_SCRAPER_AVAILABLE and domain:
        print(f"    🔬 深度爬取启动...")
        try:
            deep_contacts = run_deep_scraping(domain, pdf_urls if 'pdf_urls' in dir() else None)
            all_contacts.extend(deep_contacts)
            for c in deep_contacts:
                if c.get("email"): all_emails.add(c["email"])
        except Exception as de:
            print(f"    深度爬取跳过: {de}")

    # Step 6: Hunter.io（优先用格式检测，联系人搜索留给高价值公司）
    hunter_format = None
    if HUNTER_KEYS and domain:
        print(f"    🎯 Hunter格式API（免费额度）...")
        try:
            hunter_format = query_hunter_email_format(domain)
        except Exception:
            pass
        # 只有在其他来源找不到真实联系人时才消耗 Hunter 联系人额度
        has_real = sum(1 for c in all_contacts
                       if c.get("email") and "推算" not in c.get("email_status","")
                       and "部门" not in c.get("email_status",""))
        if has_real < 2:
            print(f"    🏹 Hunter.io联系人查询（真实联系人不足，使用Hunter额度）...")
            hunter_contacts = query_hunter(domain)
            print(f"    Hunter.io：{len(hunter_contacts)} 个")
            all_contacts.extend(hunter_contacts)
        else:
            print(f"    Hunter.io：已有 {has_real} 个真实联系人，跳过节省额度")

    # ★ NEW Step 6b: 邮箱模式学习（从已知邮箱推断命名规律）
    learned_pattern = None
    if ENHANCED_VERIFIER_AVAILABLE and domain and all_emails:
        try:
            learned_pattern = learn_email_pattern(list(all_emails), domain)
        except Exception:
            pass

    # Step 7: 邮箱推算（优先使用学习到的模式）
    if domain and all_contacts:
        print(f"    📧 推算邮箱...")
        effective_format = hunter_format or learned_pattern
        all_contacts = enrich_with_email_guesses(all_contacts, domain, all_emails, effective_format)

    # Step 8: 部门邮箱收集
    dept_kw = ["purchasing","procurement","supply","sourcing","info","contact","sales","orders","import"]
    for e in list(all_emails):
        local = e.split("@")[0].lower()
        if any(kw in local for kw in dept_kw):
            if not any(c.get("email") == e for c in all_contacts):
                all_contacts.append({"name":"","position":"部门邮箱","email":e,"phone":"","whatsapp":"","linkedin":"","email_status":"官网直接获取","email_guesses":[],"source":"官网"})

    # Step 9: 部门兜底
    if domain:
        named = sum(1 for c in all_contacts if c.get("name"))
        if named < 2:
            print(f"    📮 生成部门兜底邮箱...")
            existing = {c.get("email","").lower() for c in all_contacts}
            for prefix in DEPT_PREFIXES[:5]:
                e = f"{prefix}@{domain}"
                if e not in existing:
                    all_contacts.append({"name":"","position":f"部门邮箱 ({prefix}@)","email":e,"phone":"","whatsapp":"","linkedin":"","email_status":"推算（部门通用邮箱）","email_guesses":[],"source":"部门邮箱推算"})

    # Step 10: 注入 WhatsApp 号码
    if wa_numbers:
        tmp = deduplicate(all_contacts)
        assigned = 0
        for c in tmp:
            if not c.get("whatsapp") and assigned < len(wa_numbers):
                c["whatsapp"] = wa_numbers[assigned]
                c["phone"]    = wa_numbers[assigned]
                assigned += 1
        if not tmp:
            for i, num in enumerate(wa_numbers[:3]):
                all_contacts.append({"name":"","position":"","email":"","phone":num,"whatsapp":num,"linkedin":"","email_status":"","email_guesses":[],"source":"Website (WhatsApp)"})

    # Step 10.5: 域名预筛 — 标记无MX和一次性域名
    _checked_domains = {}
    for c in all_contacts:
        email = c.get("email","")
        if not email or "@" not in email:
            continue
        dom = email.split("@")[-1].lower()
        if dom not in _checked_domains:
            _checked_domains[dom] = check_domain_quality(dom)
        valid, label = _checked_domains[dom]
        if not valid and label not in c.get("email_status",""):
            c["email_status"] = (c.get("email_status","") + f" {label}").strip()

    # ★ NEW Step 10.6: 三角校验（邮箱-域名-公司一致性）
    if ENHANCED_VERIFIER_AVAILABLE:
        for c in all_contacts:
            email = c.get("email", "")
            if email:
                tri = triangulate_email(email, company_name, domain)
                c["_triangulation"] = tri
                if tri == "mismatch" and "❌" not in c.get("email_status", ""):
                    c["email_status"] = (c.get("email_status", "") + " ⚠️邮箱域名不匹配").strip()

    # ★ NEW Step 10.7: 增强版验证（多验证器投票 + 域名健康度）
    if ENHANCED_VERIFIER_AVAILABLE:
        print(f"    🔍 增强版多重验证...")
        all_contacts = enhanced_verify_contacts(all_contacts, max_verify=20)
    else:
        print(f"    🔍 免费API验证邮箱...")
        all_contacts = verify_contact_emails(all_contacts, max_verify=15)

    # ★ NEW Step 10.8: 社交存在验证（Gravatar）
    if DEEP_SCRAPER_AVAILABLE:
        try:
            all_contacts = batch_social_check(all_contacts, max_check=8)
        except Exception:
            pass

    # Step 11: 去重 + 统一优先级排序
    unique = deduplicate(all_contacts)
    result = unique[:max_contacts]

    n_wa      = sum(1 for c in result if c.get("whatsapp"))
    n_real    = sum(1 for c in result if c.get("email") and "推算" not in c.get("email_status",""))
    n_verified= sum(1 for c in result if "✅" in c.get("email_status",""))
    n_guess   = sum(1 for c in result if "推算" in c.get("email_status","") and "✅" not in c.get("email_status",""))
    n_li      = sum(1 for c in result if c.get("linkedin"))
    n_invalid = sum(1 for c in result if "❌" in c.get("email_status",""))
    n_risky   = sum(1 for c in result if "⚠️" in c.get("email_status",""))
    n_social  = sum(1 for c in result if c.get("_social_verified"))

    print(f"    ✅ {len(result)}个联系人 | WhatsApp {n_wa} · 真实邮箱 {n_real} · 已验证 {n_verified} · 推算 {n_guess} · LinkedIn {n_li}")
    if n_invalid or n_risky:
        print(f"    ⚠️ 域名无效 {n_invalid} · Catch-all/不确定 {n_risky}")
    if n_social:
        print(f"    🌐 社交存在确认 {n_social}")

    # 打印决策人分层
    tiers = {}
    for c in result:
        tier = c.get("_dm_tier", "")
        if tier and tier != "unknown":
            tiers[tier] = tiers.get(tier, 0) + 1
    if tiers:
        tier_str = " · ".join(f"{k}:{v}" for k, v in sorted(tiers.items()))
        print(f"    👔 决策人: {tier_str}")

    return result


# ============================================================
# 格式化输出
# ============================================================
def format_contacts(contacts):
    if not contacts:
        return "  ⚠️ 未找到联系人，建议人工通过LinkedIn查找采购负责人。"
    lines = []
    for i, c in enumerate(contacts, 1):
        name     = c.get("name","")     or "—"
        position = c.get("position","") or "—"
        email    = c.get("email","")    or "未获取"
        whatsapp = c.get("whatsapp","") or c.get("phone","")
        linkedin = c.get("linkedin","") or ""
        source   = c.get("source","")   or ""
        status   = c.get("email_status","")
        guesses  = c.get("email_guesses",[])
        line = f"  {i}. {name}"
        if position and position not in ("—","部门邮箱"): line += f"  │  {position}"
        if whatsapp: line += f"\n     📱 WhatsApp: {whatsapp}"
        line += f"\n     📧 {email}"
        if status: line += f"  [{status}]"
        if guesses: line += f"\n     备选邮箱：{' | '.join(guesses)}"
        if linkedin: line += f"\n     🔗 {linkedin}"
        if source: line += f"\n     来源：{source}"
        lines.append(line)
    return "\n\n".join(lines)


if __name__ == "__main__":
    company = input("请输入公司名称：").strip()
    if company:
        result = find_contacts(company)
        print("\n" + "="*60)
        print(format_contacts(result))
        print("="*60)
        with open(f"联系人_{company}.json","w",encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"✅ 已保存：联系人_{company}.json")
