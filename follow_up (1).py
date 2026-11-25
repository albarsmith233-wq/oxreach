# ============================================================
# Kingphar 跟进系统 · follow_up.py v1.0
# 功能：
#   - CRM 状态管理（sent / followed_up / replied / lost）
#   - 到期提醒（3天无回复自动标记为"待跟进"）
#   - AI 生成差异化跟进邮件（非复制首封，有新信息）
#   - 批量发送到期跟进 + 记录每一轮
# ============================================================

import os, json, re, time, smtplib, logging, random
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CRM 文件路径 & 状态定义
# ─────────────────────────────────────────────────────────────
CRM_FILE = "crm.json"  # 默认值，实际使用时被 get_crm_file(username) 覆盖

def _get_crm_path(username: str = "") -> str:
    """返回当前用户的CRM文件路径"""
    if username:
        from user_data import get_crm_file
        return get_crm_file(username)
    return CRM_FILE

STATUS_LABELS = {
    "sent":          "已发送",
    "followed_up":   "已跟进",
    "replied":       "已回复",
    "meeting_set":   "已约会议",
    "sample_sent":   "已寄样品",
    "lost":          "已流失",
    "not_interested":"无意向",
    # 兜底：pipeline dry_run模式可能写入的状态
    "dry_run":       "预演发送",
    "skipped":       "已跳过",
    "failed":        "发送失败",
}

# 跟进间隔（天）：第1轮→第2轮 4天，第2轮→第3轮 7天，之后停止
FOLLOWUP_INTERVALS = {1: 4, 2: 7}
MAX_ROUNDS = 3  # 最多发3封（1首发 + 2跟进）


# ─────────────────────────────────────────────────────────────
# CRM 读写
# ─────────────────────────────────────────────────────────────

def _get_storage(username=""):
    """优先MongoDB，降级文件"""
    try:
        from db import is_available
        if is_available():
            return "mongo"
    except Exception:
        pass
    return "file"


def _load_crm(username: str = "") -> dict:
    if os.path.exists(CRM_FILE):
        try:
            with open(CRM_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"contacts": {}}


def _save_crm(data: dict, username: str = ""):
    if _get_storage(username) == "mongo":
        from db import crm_save
        crm_save(username, data)
        return
    path = _get_crm_path(username)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _contact_key(company: str, email: str) -> str:
    """生成稳定的联系人唯一key"""
    safe_co = re.sub(r'[^a-zA-Z0-9]', '_', company)[:30]
    safe_em = email.replace("@", "_at_").replace(".", "_")
    return f"{safe_co}__{safe_em}"


# ─────────────────────────────────────────────────────────────
# 写入首发记录（由 email_sender.py 调用）
# ─────────────────────────────────────────────────────────────

def record_first_send(company: str, contact_name: str, email: str,
                       subject: str, body: str, sender: str,
                       status: str = "sent"):
    """
    首封邮件发出后调用，写入CRM。
    status: "sent"(真发) 或 "dry_run"(预演)
    """
    crm = _load_crm()
    key = _contact_key(company, email)

    # 计算下次跟进日期
    interval = FOLLOWUP_INTERVALS.get(1, 4)
    next_date = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")

    crm["contacts"][key] = {
        "company":      company,
        "contact_name": contact_name,
        "email":        email,
        "sender":       sender,
        "status":       status,
        "rounds": [
            {
                "round":   1,
                "type":    "first",
                "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "subject": subject,
                "body":    body[:500],  # 只存前500字节，节省空间
                "status":  status,
            }
        ],
        "next_followup": next_date if status == "sent" else None,
        "notes":         "",
        "created_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_updated":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_crm(crm)
    logger.info(f"  📋 CRM已记录：{company} → {email} (下次跟进：{next_date})")


# ─────────────────────────────────────────────────────────────
# 查询到期跟进列表
# ─────────────────────────────────────────────────────────────

def get_due_followups(sender_filter: str = "", username: str = "") -> list:
    """
    返回今天到期或已逾期的跟进列表。
    sender_filter: 只看某个发信人的（多用户场景）
    username: 用户数据隔离
    """
    crm = _load_crm(username)
    today = datetime.now().date()
    due = []

    for key, c in crm["contacts"].items():
        # 已终态的跳过
        if c["status"] in ("replied", "meeting_set", "sample_sent",
                            "lost", "not_interested"):
            continue
        # 已达最大轮次
        current_round = len(c.get("rounds", []))
        if current_round >= MAX_ROUNDS:
            continue
        # 发信人过滤
        if sender_filter and c.get("sender") != sender_filter:
            continue
        # 检查跟进日期
        nf = c.get("next_followup")
        if not nf:
            continue
        try:
            nf_date = datetime.strptime(nf, "%Y-%m-%d").date()
        except ValueError:
            continue

        days_overdue = (today - nf_date).days
        if days_overdue >= 0:
            due.append({
                **c,
                "_key":         key,
                "_round":       current_round + 1,   # 下一轮轮次
                "_days_overdue": days_overdue,
                "_due_date":    nf,
            })

    # 按逾期天数降序（最紧急的先显示）
    due.sort(key=lambda x: x["_days_overdue"], reverse=True)
    return due


def get_all_contacts(sender_filter: str = "") -> list:
    """返回所有联系人，用于看板展示"""
    crm = _load_crm()
    result = []
    for key, c in crm["contacts"].items():
        if sender_filter and c.get("sender") != sender_filter:
            continue
        result.append({**c, "_key": key})
    # 按最新更新时间倒序
    result.sort(key=lambda x: x.get("last_updated", ""), reverse=True)
    return result


def get_stats(sender_filter: str = "") -> dict:
    """返回统计数据"""
    contacts = get_all_contacts(sender_filter)
    stats = {s: 0 for s in STATUS_LABELS}
    stats["total"] = len(contacts)
    stats["due_today"] = len(get_due_followups(sender_filter))
    for c in contacts:
        s = c.get("status", "sent")
        stats[s] = stats.get(s, 0) + 1
    return stats


# ─────────────────────────────────────────────────────────────
# 更新联系人状态
# ─────────────────────────────────────────────────────────────

def update_status(key: str, new_status: str, notes: str = ""):
    """更新某联系人的跟进状态"""
    crm = _load_crm()
    if key not in crm["contacts"]:
        return False, "联系人不存在"

    c = crm["contacts"][key]
    old_status = c["status"]
    c["status"] = new_status
    c["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 终态：清除跟进日期
    if new_status in ("replied", "meeting_set", "sample_sent", "lost", "not_interested"):
        c["next_followup"] = None

    if notes:
        ts = datetime.now().strftime("%m-%d %H:%M")
        c["notes"] = f"[{ts}] {notes}\n" + c.get("notes", "")

    _save_crm(crm)
    logger.info(f"  ✅ {c['company']} 状态更新：{old_status} → {new_status}")
    return True, f"已更新为「{STATUS_LABELS.get(new_status, new_status)}」"


def add_note(key: str, note: str):
    """给联系人添加备注"""
    crm = _load_crm()
    if key not in crm["contacts"]:
        return False
    ts = datetime.now().strftime("%m-%d %H:%M")
    c = crm["contacts"][key]
    c["notes"] = f"[{ts}] {note}\n" + c.get("notes", "")
    c["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_crm(crm)
    return True


# ─────────────────────────────────────────────────────────────
# AI 生成跟进邮件
# ─────────────────────────────────────────────────────────────

FOLLOWUP_TEMPLATES = {
    2: {
        "angle": "提供新价值信息（案例/认证/产品亮点）",
        "hint":  "Reference first email briefly, share one specific new value — a case study, certification detail, or product spec. Keep it very short (under 120 words). No hard sell.",
    },
    3: {
        "angle": "最后轻触达（给对方一个简单的回应方式）",
        "hint":  "Final gentle touch. Make it extremely easy to respond — offer a simple yes/no question or let them opt out gracefully. Under 80 words. Warm, not pushy.",
    },
}

def generate_followup_email(contact: dict, round_num: int,
                              company_profile: dict = None) -> tuple:
    """
    用AI生成差异化跟进邮件。
    返回 (subject, body)
    """
    from company_profile_loader import get_company_profile
    profile = company_profile or get_company_profile(contact.get("username", ""))

    first_round = next((r for r in contact.get("rounds", []) if r["round"] == 1), {})
    first_subject = first_round.get("subject", "our previous email")
    first_body_snippet = first_round.get("body", "")[:200]

    template = FOLLOWUP_TEMPLATES.get(round_num, FOLLOWUP_TEMPLATES[2])

    prompt = f"""You are writing a follow-up email for a B2B medical supplies outreach.

Sender: {profile.get('short_name', 'Kingphar Medical')} (China, FDA/CE/ISO13485 certified, founded 1954)
Recipient: {contact.get('contact_name', 'the team')} at {contact.get('company', '')}
Round: {round_num} (follow-up #{round_num - 1})
First email subject was: "{first_subject}"
First email opening: "{first_body_snippet[:120]}..."

Angle for this follow-up: {template['angle']}
Guidance: {template['hint']}

Key facts you can reference (pick ONE most relevant):
- Zero supply disruption during COVID-19 (proven resilience)
- FDA registered since 1998 (25+ years US market experience)  
- ISO 13485 + CE MDR — ready for EU tender requirements
- Clients include distributors in 70+ countries
- Annual capacity: 1000 × 40HQ containers

Write ONLY the email. Format:
Subject: [subject line]

[email body]

Rules:
- Do NOT repeat the first email content
- Do NOT be pushy or apologetic
- Reference their company name naturally
- Sign off as: [Your Name] | Kingphar Medical | {profile.get('contact_email', '')}"""

    # 调用AI
    api_key = os.getenv("ANTHROPIC_API_KEY")
    api_url = os.getenv("ANTHROPIC_BASE_URL")
    if not api_key or not ANTHROPIC_OK:
        return _fallback_followup(contact, round_num, profile)

    for attempt in range(2):
        try:
            key  = api_key if attempt == 0 else os.getenv("BACKUP_API_KEY", api_key)
            url  = api_url if attempt == 0 else os.getenv("BACKUP_BASE_URL", api_url)
            model= os.getenv("PRIMARY_MODEL" if attempt == 0 else "BACKUP_MODEL",
                             "claude-sonnet-4-6")
            client = anthropic.Anthropic(
                api_key=key, base_url=url, max_retries=0,
                default_headers={"User-Agent": "Mozilla/5.0"},
            )
            r = client.messages.create(
                model=model, max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
                timeout=60,
            )
            raw = r.content[0].text.strip()
            subj_m = re.search(r'Subject:\s*(.+?)[\n\r]', raw, re.IGNORECASE)
            subject = subj_m.group(1).strip() if subj_m else f"Following up – Kingphar Medical"
            body_raw = raw[subj_m.end():].strip() if subj_m else raw
            body = re.sub(r'\*{1,2}([^*\n]+)\*{1,2}', r'\1', body_raw)
            body = re.sub(r'\n{3,}', '\n\n', body).strip()
            if body and len(body) > 30:
                logger.info(f"  ✅ AI生成跟进邮件成功（Round {round_num}）")
                return subject, body
        except Exception as e:
            logger.warning(f"  ⚠️ AI跟进邮件生成失败（attempt {attempt+1}）: {e}")

    return _fallback_followup(contact, round_num, profile)


def _fallback_followup(contact: dict, round_num: int, profile: dict) -> tuple:
    """AI失败时的模板备用"""
    name    = contact.get("contact_name", "")
    company = contact.get("company", "")
    greeting = f"Dear {name.split()[0]}," if name else f"Dear {company} Team,"

    if round_num == 2:
        subject = f"Re: Supply Partnership – One More Thing | Kingphar"
        body = f"""{greeting}

I wanted to follow up on my previous note about Kingphar Medical's wound care range.

One thing worth highlighting: we've been FDA registered since 1998 and maintained uninterrupted supply throughout COVID-19 — something our US distributor partners found especially valuable.

Would a brief 15-minute call make sense to explore fit?

Best regards,
[Your Name] | Kingphar Medical
{profile.get('contact_email', '')}"""

    else:
        subject = f"Last Note – Kingphar Medical"
        body = f"""{greeting}

I'll keep this brief — I've reached out a couple of times about a potential supply partnership for gauze and wound care products.

If the timing isn't right, no worries at all. But if you'd ever like to compare options on certified Chinese supply, I'm here.

Best,
[Your Name] | Kingphar Medical"""

    return subject, body


# ─────────────────────────────────────────────────────────────
# 发送跟进邮件
# ─────────────────────────────────────────────────────────────

def send_followup(key: str, subject: str, body: str,
                   dry_run: bool = True) -> tuple:
    """
    向某联系人发送跟进邮件，并更新CRM记录。
    返回 (success: bool, message: str)
    """
    crm = _load_crm()
    if key not in crm["contacts"]:
        return False, "联系人不存在"

    c = crm["contacts"][key]
    to_email = c["email"]
    to_name  = c["contact_name"]
    round_num = len(c.get("rounds", [])) + 1

    if not dry_run:
        ok, msg = _send_smtp(to_email, to_name, subject, body)
        if not ok:
            return False, msg

    # 更新CRM
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c["rounds"].append({
        "round":   round_num,
        "type":    "followup",
        "sent_at": now,
        "subject": subject,
        "body":    body[:500],
        "status":  "dry_run" if dry_run else "sent",
    })
    c["status"] = "followed_up"
    c["last_updated"] = now

    # 计算下一次跟进日期（如果还有轮次）
    if round_num < MAX_ROUNDS:
        interval = FOLLOWUP_INTERVALS.get(round_num, 7)
        c["next_followup"] = (
            datetime.now() + timedelta(days=interval)
        ).strftime("%Y-%m-%d")
    else:
        c["next_followup"] = None  # 已达最大轮次，停止跟进

    _save_crm(crm)
    mode = "预演" if dry_run else "成功"
    logger.info(f"  ✅ 跟进邮件{mode}：{c['company']} → {to_email} (Round {round_num})")
    return True, f"Round {round_num} 跟进{mode}"


def send_all_due(dry_run: bool = True, sender_filter: str = "") -> dict:
    """
    批量发送所有到期跟进邮件。
    返回 summary dict。
    """
    from company_profile_loader import get_company_profile

    due = get_due_followups(sender_filter)
    summary = {"sent": 0, "failed": 0, "skipped": 0}

    if not due:
        print("  ✅ 没有到期的跟进任务")
        return summary

    print(f"\n{'='*60}")
    print(f"  📬 {'[预演] ' if dry_run else ''}批量跟进 — {len(due)} 封")
    print(f"{'='*60}")

    for item in due:
        company = item["company"]
        email   = item["email"]
        round_n = item["_round"]
        print(f"\n  [{round_n}轮] {company} <{email}> (逾期{item['_days_overdue']}天)")

        # 生成跟进邮件
        try:
            subject, body = generate_followup_email(item, round_n, get_company_profile(item.get("username", "")))
        except Exception as e:
            print(f"  ❌ 生成邮件失败：{e}")
            summary["failed"] += 1
            continue

        print(f"  Subject: {subject}")

        ok, msg = send_followup(item["_key"], subject, body, dry_run=dry_run)
        if ok:
            print(f"  ✅ {msg}")
            summary["sent"] += 1
        else:
            print(f"  ❌ {msg}")
            summary["failed"] += 1

        if not dry_run:
            delay = random.randint(8, 15)
            time.sleep(delay)

    print(f"\n{'='*60}")
    print(f"  完成：成功{summary['sent']} / 失败{summary['failed']}")
    print(f"{'='*60}")
    return summary


# ─────────────────────────────────────────────────────────────
# SMTP 发送（内部）
# ─────────────────────────────────────────────────────────────

def _send_smtp(to_email: str, to_name: str, subject: str, body: str) -> tuple:
    host    = os.getenv("SMTP_HOST", "smtp.qiye.aliyun.com")
    port    = int(os.getenv("SMTP_PORT", "465"))
    use_ssl = os.getenv("SMTP_USE_SSL", "true").lower() == "true"
    user    = os.getenv("SMTP_USER", "")
    passwd  = os.getenv("SMTP_PASSWORD", "")
    fname   = os.getenv("SMTP_FROM_NAME", "Kingphar Medical")

    if not user or not passwd:
        return False, "SMTP未配置"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{fname} <{user}>"
        msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email
        msg["Reply-To"] = user
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()

        server.login(user, passwd)
        server.sendmail(user, [to_email], msg.as_string())
        server.quit()
        return True, "发送成功"
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────
# 命令行入口
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        stats = get_stats()
        print(f"\n{'='*50}\n  Kingphar CRM 跟进状态\n{'='*50}")
        print(f"  总联系人：{stats['total']}")
        print(f"  今日待跟进：{stats['due_today']}")
        for s, label in STATUS_LABELS.items():
            n = stats.get(s, 0)
            if n: print(f"  {label}：{n}")

    elif cmd == "due":
        due = get_due_followups()
        print(f"\n  今日待跟进 ({len(due)} 封)：")
        for d in due:
            print(f"  · {d['company']} <{d['email']}> 逾期{d['_days_overdue']}天 Round{d['_round']}")

    elif cmd == "run":
        dry = "--live" not in sys.argv
        send_all_due(dry_run=dry)

    else:
        print("用法: python follow_up.py [status|due|run|run --live]")


# ─────────────────────────────────────────────────────────────
# 推迟跟进
# ─────────────────────────────────────────────────────────────

def postpone_followup(key: str, days: int = 3, username: str = ""):
    """推迟某联系人的跟进日期"""
    crm = _load_crm(username)
    if key not in crm.get("contacts", {}):
        return False, "联系人不存在"
    c = crm["contacts"][key]
    c["next_followup"] = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    c["last_updated"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_crm(crm, username)
    return True, f"已推迟 {days} 天"


def send_followup_mt(key: str, subject: str, body: str,
                     username: str = "", sender: str = "",
                     smtp_pass: str = "", dry_run: bool = False):
    """
    多租户版跟进发信（使用用户自己的SMTP配置）。
    返回 (success, message)
    """
    crm = _load_crm(username)
    if key not in crm.get("contacts", {}):
        return False, "联系人不存在"
    c = crm["contacts"][key]
    to_email  = c["email"]
    to_name   = c.get("contact_name", "")
    round_num = len(c.get("rounds", [])) + 1

    if not dry_run:
        from email_sender import send_single_email
        ok, msg = send_single_email(
            to_email=to_email, to_name=to_name,
            subject=subject, body=body,
            sender_email=sender, username=username,
            user_smtp_pass=smtp_pass,
        )
        if not ok:
            return False, msg

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.setdefault("rounds", []).append({
        "round":   round_num,
        "type":    "followup",
        "sent_at": now,
        "subject": subject,
        "body":    body[:500],
        "status":  "dry_run" if dry_run else "sent",
    })
    c["status"]       = "followed_up"
    c["last_updated"] = now

    if round_num < MAX_ROUNDS:
        interval = FOLLOWUP_INTERVALS.get(round_num, 7)
        c["next_followup"] = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
    else:
        c["next_followup"] = None

    _save_crm(crm, username)
    return True, f"Round {round_num} {'预演' if dry_run else '发送成功'}"
