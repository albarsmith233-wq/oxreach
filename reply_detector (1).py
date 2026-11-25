# ============================================================
# Kingphar — 客户回复检测模块
# 支持通用 IMAP（阿里企业邮/腾讯企业邮/自建邮件服务器）
# ============================================================

import os, re, ssl, imaplib, email, email.header
from datetime import datetime, timedelta

# ── 常见邮件服务商 IMAP 配置 ─────────────────────────────
IMAP_PRESETS = {
    "aliyun":    {"host": "imap.qiye.aliyun.com",   "port": 993, "name": "阿里企业邮"},
    "tencent":   {"host": "imap.exmail.qq.com",      "port": 993, "name": "腾讯企业邮"},
    "qq":        {"host": "imap.qq.com",             "port": 993, "name": "QQ邮箱"},
    "163":       {"host": "imap.163.com",            "port": 993, "name": "网易163"},
    "126":       {"host": "imap.126.com",            "port": 993, "name": "网易126"},
    "gmail":     {"host": "imap.gmail.com",          "port": 993, "name": "Gmail (需App Password)"},
    "outlook":   {"host": "outlook.office365.com",   "port": 993, "name": "Outlook/Office365"},
    "custom":    {"host": "",                         "port": 993, "name": "自定义IMAP"},
}

def detect_provider(email_addr: str) -> str:
    """根据邮箱域名自动识别服务商"""
    domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    if "aliyun.com" in domain or "alibaba.com" in domain:
        return "aliyun"
    if "exmail.qq.com" in domain or "qq.com" in domain:
        return "tencent"
    if "163.com" in domain:
        return "163"
    if "126.com" in domain:
        return "126"
    if "gmail.com" in domain:
        return "gmail"
    if "outlook.com" in domain or "hotmail.com" in domain or "office365.com" in domain:
        return "outlook"
    return "aliyun"


def _connect(host, port, username, password):
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    conn.login(username, password)
    return conn


def _decode_str(s):
    if not s:
        return ""
    parts = email.header.decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def _extract_addr(header):
    m = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', header)
    return m.group(0).lower() if m else ""


def _get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    cs = part.get_content_charset() or "utf-8"
                    body = part.get_payload(decode=True).decode(cs, errors="replace")
                    break
                except Exception:
                    pass
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        cs = part.get_content_charset() or "utf-8"
                        html = part.get_payload(decode=True).decode(cs, errors="replace")
                        body = re.sub(r'<[^>]+>', ' ', html)
                        body = re.sub(r'\s+', ' ', body).strip()
                        break
                    except Exception:
                        pass
    else:
        try:
            cs = msg.get_content_charset() or "utf-8"
            body = msg.get_payload(decode=True).decode(cs, errors="replace")
        except Exception:
            pass
    body = re.split(r'\nOn .{10,80} wrote:', body)[0]
    body = re.split(r'\n[-]{3,}', body)[0]
    return body[:2000].strip()


def fetch_inbox(imap_config, days=7):
    host     = imap_config.get("host", "")
    port     = int(imap_config.get("port", 993))
    username = imap_config.get("username", "")
    password = imap_config.get("password", "")

    if not all([host, username, password]):
        return []

    emails = []
    try:
        conn = _connect(host, port, username, password)
        conn.select("INBOX")
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, nums = conn.search(None, f'SINCE {since}')
        if not nums[0]:
            conn.logout()
            return []

        for num in nums[0].split()[-200:]:
            try:
                _, data = conn.fetch(num, "(RFC822)")
                msg      = email.message_from_bytes(data[0][1])
                from_raw = msg.get("From", "")
                subject  = _decode_str(msg.get("Subject", ""))
                date_str = msg.get("Date", "")
                from_email = _extract_addr(from_raw)
                from_name  = _decode_str(re.sub(r'<[^>]+>', '', from_raw).strip().strip('"\''))
                is_reply   = bool(re.match(r're[\s:]+|回复[\s:]+|答复[\s:]+', subject, re.IGNORECASE))
                body       = _get_body(msg)
                try:
                    from email.utils import parsedate_to_datetime
                    date_fmt = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date_fmt = date_str[:20]
                emails.append({
                    "from_email": from_email,
                    "from_name":  from_name,
                    "subject":    subject,
                    "body":       body,
                    "date":       date_fmt,
                    "is_reply":   is_reply,
                })
            except Exception:
                continue

        conn.logout()
        print(f"  📬 扫描完成：{len(emails)} 封邮件")
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"IMAP登录失败（{e}），请检查账号密码和IMAP是否已开启")
    except Exception as e:
        raise RuntimeError(f"邮件扫描失败：{e}")
    return emails


def match_to_crm(inbox, crm_contacts):
    index = {
        c.get("email", "").lower(): (k, c)
        for k, c in crm_contacts.items()
        if c.get("email")
    }
    matched = []
    for mail in inbox:
        if mail["from_email"] in index:
            key, contact = index[mail["from_email"]]
            matched.append({
                "crm_key": key,
                "contact": contact,
                "email":   mail,
                "company": contact.get("company", ""),
            })
    return matched


def analyze_reply_intent(company, body):
    if not body or len(body) < 10:
        return {"intent": "neutral", "label": "无内容", "summary": "",
                "next_action": "", "crm_status": "replied", "urgency": "low"}
    try:
        from kingphar_intel import call_ai_with_retry
        import json as _json

        prompt = f"""分析以下来自 {company} 的回复邮件，判断客户意向。

邮件内容：
{body[:800]}

请只返回JSON，格式：
{{
  "intent": "interested" | "need_sample" | "meeting" | "pricing" | "reject" | "neutral",
  "label": "中文标签",
  "summary": "一句话总结（20字以内）",
  "next_action": "建议下一步（30字以内）",
  "urgency": "high" | "medium" | "low"
}}"""

        result = call_ai_with_retry(prompt=prompt)
        if not result:
            raise ValueError("AI返回空")

        clean = re.sub(r'```[a-z]*\s*|\s*```', '', result).strip()
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        data = _json.loads(m.group() if m else clean)
        status_map = {
            "interested": "replied", "need_sample": "sample_sent",
            "meeting": "meeting_set", "pricing": "replied",
            "reject": "not_interested", "neutral": "replied",
        }
        data["crm_status"] = status_map.get(data.get("intent", "neutral"), "replied")
        return data
    except Exception as e:
        print(f"  ⚠️ 意向分析失败: {e}")
        return {
            "intent": "neutral", "label": "已回复",
            "summary": "客户有回复", "next_action": "人工查看并跟进",
            "crm_status": "replied", "urgency": "medium",
        }


def process_replies(username, imap_config, crm_contacts, days=7):
    print(f"\n📬 开始扫描回复：{username}")
    inbox = fetch_inbox(imap_config, days=days)
    if not inbox:
        print("  收件箱为空或扫描失败")
        return []

    replies = [m for m in inbox if m["is_reply"]]
    print(f"  📨 发现 {len(replies)} 封回复邮件")

    matched = match_to_crm(replies, crm_contacts)
    print(f"  🎯 匹配到 CRM 联系人: {len(matched)} 个")
    if not matched:
        return []

    results = []
    for item in matched:
        company = item["company"]
        body    = item["email"]["body"]
        print(f"  🤖 分析意向: {company}...")
        intent = analyze_reply_intent(company, body)
        results.append({
            "crm_key": item["crm_key"],
            "contact": item["contact"],
            "company": company,
            "email":   item["email"],
            "intent":  intent,
        })
        print(f"  ✅ {company}: {intent.get('label','')} — {intent.get('summary','')}")
    return results


def test_connection(imap_config):
    try:
        conn = _connect(
            imap_config["host"],
            int(imap_config.get("port", 993)),
            imap_config["username"],
            imap_config["password"],
        )
        conn.select("INBOX")
        _, nums = conn.search(None, "ALL")
        count = len(nums[0].split()) if nums[0] else 0
        conn.logout()
        return True, f"连接成功，收件箱共 {count} 封邮件"
    except imaplib.IMAP4.error as e:
        return False, f"登录失败：{e}（请确认账号密码正确，且已开启IMAP）"
    except Exception as e:
        return False, f"连接失败：{e}"
