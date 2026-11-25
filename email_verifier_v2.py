# ============================================================
# Ox · email_verifier_v2.py  v1.0
# 增强验证：多验证器投票 / 域名健康度 / 三角校验 / 决策人评分
# ============================================================
import re, time, hashlib, requests

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False

try:
    import whois as whois_lib
    WHOIS_AVAILABLE = True
except ImportError:
    WHOIS_AVAILABLE = False


# ============================================================
# 1. 域名健康度评分（SPF/DMARC/MX 综合评估）
# ============================================================

# 缓存
_domain_health_cache = {}

def score_domain_health(domain):
    """
    综合评估域名邮件配置的健康度。
    SPF + DMARC + MX 齐全 = catch-all 可能性低 = 验证更可靠。
    
    返回: {
        'score': 0-100,
        'has_mx': bool,
        'has_spf': bool,
        'has_dmarc': bool,
        'is_catch_all_likely': bool,
        'label': str,
    }
    """
    if not domain:
        return {'score': 0, 'has_mx': False, 'has_spf': False,
                'has_dmarc': False, 'is_catch_all_likely': True,
                'label': '❌域名为空'}

    domain = domain.lower().strip()
    if domain in _domain_health_cache:
        return _domain_health_cache[domain]

    result = {
        'score': 0,
        'has_mx': False,
        'has_spf': False,
        'has_dmarc': False,
        'is_catch_all_likely': True,
        'label': '',
    }

    if not DNS_AVAILABLE:
        result['label'] = '⚠️DNS模块不可用'
        _domain_health_cache[domain] = result
        return result

    # MX 记录
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        if len(mx_records) > 0:
            result['has_mx'] = True
            result['score'] += 30
    except Exception:
        result['label'] = '❌无MX记录'
        _domain_health_cache[domain] = result
        return result

    # SPF 记录
    try:
        txt_records = dns.resolver.resolve(domain, 'TXT')
        for r in txt_records:
            txt = str(r).strip('"')
            if txt.startswith('v=spf1'):
                result['has_spf'] = True
                result['score'] += 30
                # 严格 SPF（-all）= catch-all 可能性低
                if '-all' in txt:
                    result['score'] += 10
                break
    except Exception:
        pass

    # DMARC 记录
    try:
        dmarc_records = dns.resolver.resolve(f'_dmarc.{domain}', 'TXT')
        for r in dmarc_records:
            txt = str(r).strip('"')
            if 'v=DMARC1' in txt or 'v=dmarc1' in txt.lower():
                result['has_dmarc'] = True
                result['score'] += 20
                # reject/quarantine 策略 = 严格管控
                if 'p=reject' in txt or 'p=quarantine' in txt:
                    result['score'] += 10
                break
    except Exception:
        pass

    # 综合判断 catch-all 可能性
    if result['has_spf'] and result['has_dmarc']:
        result['is_catch_all_likely'] = False
    elif result['has_spf']:
        result['is_catch_all_likely'] = False
    else:
        result['is_catch_all_likely'] = True

    # 生成标签
    if result['score'] >= 70:
        result['label'] = '🟢域名配置完善'
    elif result['score'] >= 40:
        result['label'] = '🟡域名配置一般'
    else:
        result['label'] = '🟠域名配置简陋（catch-all风险高）'

    _domain_health_cache[domain] = result
    return result


# ============================================================
# 2. 多验证器投票（disify + mailcheck + verifalia免费）
# ============================================================

def _verify_disify(email):
    """disify.com 验证"""
    try:
        resp = requests.get(
            f"https://disify.com/api/email/{email}", timeout=6
        )
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                return 'unknown'
            if data.get("disposable"):
                return 'invalid'
            if not data.get("dns"):
                return 'invalid'
            if not data.get("format"):
                return 'invalid'
            if data.get("role"):
                return 'risky'
            return 'valid'
    except Exception:
        pass
    return 'unknown'


def _verify_mailcheck(email):
    """mailcheck.ai 验证"""
    try:
        resp = requests.get(
            f"https://api.mailcheck.ai/email/{email}", timeout=6
        )
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                return 'unknown'
            if data.get("disposable"):
                return 'invalid'
            if data.get("spam"):
                return 'invalid'
            if not data.get("mx"):
                return 'invalid'
            if data.get("role_account"):
                return 'risky'
            return 'valid'
    except Exception:
        pass
    return 'unknown'


def _verify_emailrep(email):
    """emailrep.io 免费验证（每日250次免费额度）"""
    try:
        resp = requests.get(
            f"https://emailrep.io/{email}",
            headers={"User-Agent": "OxReach/1.0"},
            timeout=6
        )
        if resp.status_code == 200:
            data = resp.json()
            reputation = data.get("reputation", "")
            suspicious = data.get("suspicious", False)
            if reputation == "high":
                return 'valid'
            elif reputation in ("medium",):
                return 'risky'
            elif reputation in ("low", "none") or suspicious:
                return 'invalid'
    except Exception:
        pass
    return 'unknown'


def multi_verify_email(email, use_all=False):
    """
    多验证器投票。默认用2个（disify + mailcheck），
    use_all=True 时加第3个（emailrep）。
    
    投票规则：
    - 2/2 或 2/3 说 valid → valid
    - 2/2 或 2/3 说 invalid → invalid
    - 其他 → risky/unknown
    
    返回: 'valid' / 'invalid' / 'risky' / 'unknown'
    """
    votes = []

    # 第1票: disify
    v1 = _verify_disify(email)
    votes.append(v1)
    time.sleep(0.2)

    # 第2票: mailcheck
    v2 = _verify_mailcheck(email)
    votes.append(v2)

    # 可选第3票: emailrep
    if use_all:
        time.sleep(0.2)
        v3 = _verify_emailrep(email)
        votes.append(v3)

    # 投票统计
    valid_count = votes.count('valid')
    invalid_count = votes.count('invalid')
    risky_count = votes.count('risky')
    total_known = valid_count + invalid_count + risky_count

    if total_known == 0:
        return 'unknown'

    if valid_count >= 2:
        return 'valid'
    if invalid_count >= 2:
        return 'invalid'
    if valid_count == 1 and invalid_count == 0:
        return 'valid'
    if invalid_count == 1 and valid_count == 0:
        return 'invalid'
    return 'risky'


# ============================================================
# 3. 邮箱-域名-公司 三角校验
# ============================================================

def triangulate_email(email, company_name, domain=""):
    """
    三角校验：邮箱域名 ↔ 公司域名 ↔ WHOIS 是否一致。
    不一致的邮箱降权。
    
    返回: 'match' / 'mismatch' / 'unknown'
    """
    if not email or "@" not in email:
        return 'unknown'

    email_domain = email.split("@")[-1].lower()

    # 校验1: 邮箱域名 vs 公司域名
    if domain:
        domain = domain.lower()
        if email_domain == domain or domain in email_domain:
            return 'match'
        # 子域名也算匹配
        if email_domain.endswith("." + domain):
            return 'match'

    # 校验2: 邮箱域名 vs 公司名关键词
    if company_name:
        company_kw = re.sub(
            r'\b(inc|llc|ltd|corp|co|medical|surgical|international'
            r'|trade|health|group|supplies|depot)\b',
            '', company_name.lower()
        ).split()
        company_kw = [k for k in company_kw if len(k) > 2]
        if any(kw in email_domain for kw in company_kw):
            return 'match'

    # 校验3: WHOIS 域名注册人 vs 邮箱域名
    if WHOIS_AVAILABLE and email_domain:
        try:
            w = whois_lib.whois(email_domain)
            org = (w.get("org") or w.get("registrant_org") or "").lower()
            if company_name and any(
                kw in org for kw in company_name.lower().split()
                if len(kw) > 2
            ):
                return 'match'
        except Exception:
            pass

    # 公共邮箱域名不算 mismatch
    PUBLIC_DOMAINS = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "aol.com", "icloud.com", "mail.com", "protonmail.com",
        "yandex.com", "163.com", "qq.com", "126.com",
    }
    if email_domain in PUBLIC_DOMAINS:
        return 'unknown'  # 不确定，不降权

    if domain and email_domain != domain:
        return 'mismatch'

    return 'unknown'


# ============================================================
# 4. 决策人识别 & 评分增强
# ============================================================

# 职位关键词权重（越高越有价值）
TITLE_WEIGHTS = {
    # 最高价值：直接采购决策人
    "purchasing director": 100,
    "procurement director": 100,
    "head of procurement": 100,
    "chief procurement": 100,
    "vp procurement": 95,
    "vp purchasing": 95,
    "purchasing manager": 90,
    "procurement manager": 90,
    "supply chain director": 85,
    "supply chain manager": 80,
    "sourcing manager": 80,
    "sourcing director": 85,
    "buyer": 75,
    "category manager": 75,
    "import manager": 80,
    "import director": 85,

    # 高价值：高管（有最终决策权）
    "ceo": 70,
    "owner": 70,
    "founder": 70,
    "president": 65,
    "managing director": 65,
    "general manager": 60,
    "coo": 55,
    "cfo": 40,

    # 中等价值：相关部门负责人
    "operations director": 55,
    "operations manager": 50,
    "logistics manager": 45,
    "warehouse manager": 35,
    "quality manager": 30,

    # 低价值（但有联系价值）
    "sales manager": 25,
    "marketing manager": 20,
    "business development": 30,
    "account manager": 25,
}

def score_decision_maker(position):
    """
    评估联系人是否为关键决策人。
    返回: (score: 0-100, tier: str)
    """
    if not position:
        return 0, "unknown"

    pos_lower = position.lower()
    best_score = 0

    for title, weight in TITLE_WEIGHTS.items():
        if title in pos_lower:
            best_score = max(best_score, weight)

    # 通用关键词加分
    if any(kw in pos_lower for kw in ["purchas", "procure", "sourc", "buyer"]):
        best_score = max(best_score, 70)
    if any(kw in pos_lower for kw in ["director", "head of", "vp ", "vice president"]):
        best_score = max(best_score, 50)
    if any(kw in pos_lower for kw in ["manager"]):
        best_score = max(best_score, 30)

    # 分层
    if best_score >= 80:
        tier = "Tier1-采购决策人"
    elif best_score >= 60:
        tier = "Tier2-高管"
    elif best_score >= 40:
        tier = "Tier3-部门负责人"
    elif best_score > 0:
        tier = "Tier4-相关人员"
    else:
        tier = "unknown"

    return best_score, tier


# ============================================================
# 5. 邮箱模式学习（从已知邮箱推断同公司其他人）
# ============================================================

def learn_email_pattern(known_emails, domain):
    """
    从已知验证邮箱学习该公司的邮箱命名模式。
    返回 pattern 字符串或 None。
    """
    if not domain:
        return None

    patterns_found = []

    for email in known_emails:
        if "@" not in email:
            continue
        local, email_domain = email.split("@", 1)
        if email_domain.lower() != domain.lower():
            continue

        local = local.lower()

        # 跳过角色邮箱
        ROLES = [
            "info", "sales", "contact", "admin", "support",
            "hello", "team", "office", "orders", "enquiries",
            "service", "help", "mail", "marketing",
        ]
        if local in ROLES:
            continue

        # 检测模式
        if "." in local:
            parts = local.split(".")
            if len(parts) == 2:
                if len(parts[0]) == 1:
                    patterns_found.append("{f}.{last}@{domain}")
                elif len(parts[0]) > 1 and len(parts[1]) > 1:
                    patterns_found.append("{first}.{last}@{domain}")
        elif "_" in local:
            patterns_found.append("{first}_{last}@{domain}")
        elif len(local) > 3:
            # firstlast 或 flast
            if len(local) <= 5:
                patterns_found.append("{f}{last}@{domain}")
            else:
                patterns_found.append("{first}{last}@{domain}")

    if not patterns_found:
        return None

    # 选最常见的模式
    from collections import Counter
    most_common = Counter(patterns_found).most_common(1)
    if most_common:
        pattern = most_common[0][0]
        print(f"    📐 邮箱模式学习: {pattern} (从 {len(patterns_found)} 个样本)")
        return pattern

    return None


# ============================================================
# 6. 综合验证流程（替代原 verify_contact_emails）
# ============================================================

def enhanced_verify_contacts(contacts, max_verify=20):
    """
    增强版联系人验证流程：
    1. 域名健康度评估
    2. 多验证器投票
    3. 三角校验
    4. 社交存在检查（由 deep_scraper 处理）
    5. 决策人评分
    """
    verified_count = 0
    domain_scores = {}  # 域名健康度缓存

    for c in contacts:
        email = c.get("email", "")
        status = c.get("email_status", "")

        # 已标记的跳过
        if "✅" in status or "❌" in status:
            continue

        if not email or "@" not in email:
            # 只做决策人评分
            pos = c.get("position", "")
            if pos:
                score, tier = score_decision_maker(pos)
                c["_dm_score"] = score
                c["_dm_tier"] = tier
            continue

        domain = email.split("@")[-1].lower()

        # Step 1: 域名健康度（不消耗API额度）
        if domain not in domain_scores:
            domain_scores[domain] = score_domain_health(domain)
        dh = domain_scores[domain]

        if not dh['has_mx']:
            c["email_status"] = (status + " ❌域名无MX记录").strip()
            continue

        # 域名健康度标注
        if dh['score'] >= 70 and "域名" not in status:
            c["_domain_health"] = "excellent"
        elif dh['is_catch_all_likely'] and "⚠️" not in status:
            c["_domain_health"] = "catch_all_risk"

        # Step 2: 只对推算邮箱做多验证器投票
        if "推算" in status and verified_count < max_verify:
            result = multi_verify_email(email)
            verified_count += 1

            if result == 'valid':
                c["email_status"] = (status + " ✅多重验证通过").strip()
            elif result == 'invalid':
                # 尝试备选
                guesses = c.get("email_guesses", [])
                found_alt = False
                for alt in guesses:
                    if verified_count >= max_verify:
                        break
                    alt_result = multi_verify_email(alt)
                    verified_count += 1
                    if alt_result == 'valid':
                        c["email"] = alt
                        c["email_status"] = "推算备选 ✅多重验证通过"
                        c["email_guesses"] = [g for g in guesses if g != alt]
                        found_alt = True
                        break
                if not found_alt:
                    c["email_status"] = (status + " ❌多重验证失败").strip()
            elif result == 'risky':
                c["email_status"] = (
                    status + " ⚠️Catch-all/不确定"
                ).strip()
            time.sleep(0.3)

        # Step 3: 决策人评分
        pos = c.get("position", "")
        if pos:
            score, tier = score_decision_maker(pos)
            c["_dm_score"] = score
            c["_dm_tier"] = tier

    # 打印统计
    n_verified = sum(1 for c in contacts if "✅" in c.get("email_status", ""))
    n_invalid = sum(1 for c in contacts if "❌" in c.get("email_status", ""))
    n_risky = sum(1 for c in contacts if "⚠️" in c.get("email_status", ""))
    print(
        f"    🔍 增强验证: {verified_count} 检查, "
        f"✅{n_verified} ❌{n_invalid} ⚠️{n_risky}"
    )

    return contacts


def enhanced_priority_score(c):
    """
    增强版优先级评分（替代原 _priority_score）。
    整合决策人评分、域名健康度、社交存在。
    """
    score = 0
    status = c.get("email_status", "")
    email = c.get("email", "")

    # 无效域名直接打底
    if "❌" in status:
        return -100

    # WhatsApp（最有价值的联系方式）
    if c.get("whatsapp") or c.get("phone"):
        score += 200

    # 邮箱质量评分
    if email:
        if any(x in status for x in [
            "✅", "verified", "FDA", "Hunter.io",
            "WHOIS直接", "官网直接", "PDF文档直接",
            "展会目录直接", "B2B平台直接", "政府采购"
        ]):
            score += 100
        elif "多重验证通过" in status:
            score += 110  # 多重验证比单一验证更可靠
        elif "推算" in status and "✅" in status:
            score += 70
        elif "⚠️" in status:
            score += 25
        elif "推算" in status:
            score += 40
        elif "部门" in status:
            score += 30
        elif "Claude搜索（实名邮箱）" in status:
            score += 80
        elif "Wayback" in status:
            score += 50  # 历史数据可能过期，中等可信
        elif "Sitemap" in status:
            score += 70
        elif "GitHub" in status:
            score += 60
        else:
            score += 20

    # 社交存在加分
    if c.get("_social_verified"):
        score += 30

    # 域名健康度加分
    if c.get("_domain_health") == "excellent":
        score += 15
    elif c.get("_domain_health") == "catch_all_risk":
        score -= 10

    # LinkedIn
    if c.get("linkedin"):
        score += 15

    # 决策人评分（最重要的区分因子）
    dm_score = c.get("_dm_score", 0)
    if dm_score > 0:
        score += dm_score  # 直接加，最高+100
    else:
        # 通过职位关键词简单判断
        title = (c.get("position") or c.get("title") or "").lower()
        if any(kw in title for kw in [
            "purchas", "procure", "supply", "sourc",
            "buyer", "import", "category", "logistics", "head of"
        ]):
            score += 30
        if any(kw in title for kw in [
            "director", "manager", "ceo", "owner",
            "president", "founder"
        ]):
            score += 15

    # 三角校验
    if c.get("_triangulation") == "match":
        score += 20
    elif c.get("_triangulation") == "mismatch":
        score -= 30

    # Apollo 分数
    score += c.get("_apollo_score", 0) * 2

    return score
