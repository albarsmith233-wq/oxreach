# ============================================================
# Kingphar 自动发信模块 v1.1
# 修复：
#   - 删除重复的 send_single_email 定义
#   - 优先使用用户自己的 smtp_pass，无则降级到环境变量
#   - 发信记录写入 MongoDB（降级 CSV）
# ============================================================

import os
import csv
import time
import random
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

logger = logging.getLogger(__name__)

ALIBABA_SMTP_DEFAULTS = {
    "host": "smtp.qiye.aliyun.com",
    "port": "465",
    "use_ssl": "true",
}

SEND_LOG_FILE = "发信记录.csv"


def _get_send_log(username: str = "") -> str:
    if username:
        from user_data import get_send_log_file
        return get_send_log_file(username)
    return SEND_LOG_FILE


# ============================================================
# SMTP 配置检查 & 连通性测试
# ============================================================

def setup_smtp_config():
    """检查 SMTP 环境变量是否齐全（Railway 云端用）"""
    required_keys = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]
    missing = [k for k in required_keys if not os.getenv(k)]
    if missing:
        logger.warning(f"  ⚠️ SMTP配置缺失: {missing}")
        return False
    return True


def test_smtp_connection(host=None, port=None, use_ssl=None, user=None, password=None):
    """测试 SMTP 连通性，参数可选（不传则读环境变量）"""
    host     = host     or os.getenv("SMTP_HOST")
    port     = port     or int(os.getenv("SMTP_PORT", "465"))
    use_ssl  = use_ssl  if use_ssl is not None else (os.getenv("SMTP_USE_SSL", "true").lower() == "true")
    user     = user     or os.getenv("SMTP_USER")
    password = password or os.getenv("SMTP_PASSWORD")

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            server.starttls()
        server.login(user, password)
        server.quit()
        logger.info("  ✅ SMTP连接测试通过")
        return True
    except Exception as e:
        logger.error(f"  ❌ SMTP连接失败：{e}")
        return False


# ============================================================
# 核心：发送单封邮件
# ============================================================

SMTP_PRESETS = {
    "aliyun":  {"host": "smtp.qiye.aliyun.com", "port": 465, "ssl": True},
    "tencent": {"host": "smtp.exmail.qq.com",   "port": 465, "ssl": True},
    "gmail":   {"host": "smtp.gmail.com",        "port": 587, "ssl": False},
    "outlook": {"host": "smtp.office365.com",    "port": 587, "ssl": False},
    "163":     {"host": "smtp.163.com",          "port": 465, "ssl": True},
    "qq":      {"host": "smtp.qq.com",           "port": 465, "ssl": True},
}

def send_single_email(to_email, to_name, subject, body,
                      sender_email="", sender_name=None,
                      username="", user_smtp_pass="",
                      smtp_provider=""):
    """
    发送单封邮件。

    密码优先级：
      1. user_smtp_pass（用户在侧边栏配置的密码）
      2. os.getenv("SMTP_PASSWORD")（Railway 环境变量）

    smtp_provider: 用户配置的邮箱服务商key（aliyun/gmail/outlook等），
                   为空则回退到环境变量。

    返回: (bool, str)  成功=True, 失败=False + 错误信息
    """
    # ★ 优先用用户选择的服务商预设，回退到环境变量
    if smtp_provider and smtp_provider in SMTP_PRESETS:
        preset  = SMTP_PRESETS[smtp_provider]
        host    = preset["host"]
        port    = preset["port"]
        use_ssl = preset["ssl"]
    else:
        host    = os.getenv("SMTP_HOST", "")
        port    = int(os.getenv("SMTP_PORT", "465"))
        use_ssl = os.getenv("SMTP_USE_SSL", "true").lower() == "true"
    user      = sender_email or os.getenv("SMTP_USER", "")
    # ★ 关键修复：优先用用户自己的密码
    password  = user_smtp_pass or os.getenv("SMTP_PASSWORD", "")
    from_name = sender_name or os.getenv("SMTP_FROM_NAME", "Kingphar Medical")

    if not all([host, user, password, to_email]):
        return False, (
            f"缺少配置: "
            f"host={bool(host)}, user={bool(user)}, "
            f"pass={bool(password)}, to={bool(to_email)}"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{user}>"
    msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email
    msg.attach(MIMEText(body, "plain", "utf-8"))

    html_body = body.replace("\n", "<br>\n")
    html_body = (
        f'<html><body style="font-family:Arial,sans-serif;font-size:14px;'
        f'color:#333;line-height:1.6">{html_body}</body></html>'
    )
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                smtp.login(user, password)
                smtp.sendmail(user, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.sendmail(user, [to_email], msg.as_string())

        # ── 写发信日志 ────────────────────────────────────────
        _record_send_log(username, user, to_name, to_email, subject, "成功", "")
        return True, "发送成功"

    except smtplib.SMTPAuthenticationError:
        return False, "SMTP认证失败，请检查账号密码（或客户端专用密码）"
    except smtplib.SMTPRecipientsRefused:
        return False, "收件人地址被拒绝（邮箱可能不存在）"
    except smtplib.SMTPException as e:
        return False, f"SMTP错误：{e}"
    except Exception as e:
        return False, f"发送异常：{e}"


def _record_send_log(username, sender, to_name, to_email, subject, status, note):
    """写发信记录：优先 MongoDB，降级 CSV"""
    try:
        record = {
            "sent_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sender":   sender,
            "to_name":  to_name,
            "to_email": to_email,
            "subject":  subject,
            "status":   status,
            "note":     note,
        }
        try:
            from db import sendlog_append, is_available
            if is_available():
                sendlog_append(username or "default", record)
                return
        except Exception:
            pass
        # 降级 CSV
        log_path   = _get_send_log(username)
        log_exists = os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if not log_exists:
                w.writerow(["发送时间","发信人","收件人","邮箱","邮件主题","发送状态","备注"])
            w.writerow([record["sent_at"], sender, to_name, to_email, subject, status, note])
    except Exception:
        pass


# ============================================================
# CRM 写入
# ============================================================

def add_to_crm_after_send(company, country, to_email, to_name,
                           subject, body, sender_email="", username=""):
    """发信后自动写入 CRM，设置跟进计划"""
    try:
        from follow_up import _load_crm, _save_crm
        crm      = _load_crm(username)
        contacts = crm.get("contacts", {})
        key      = f"{company}_{to_email}".replace(" ", "_").replace("@", "_at_")
        now      = datetime.now()
        followup = (now + __import__("datetime").timedelta(days=3)).strftime("%Y-%m-%d")

        if key not in contacts:
            contacts[key] = {
                "company":      company,
                "country":      country,
                "email":        to_email,
                "contact_name": to_name,
                "status":       "sent",
                "sent_at":      now.strftime("%Y-%m-%d %H:%M"),
                "next_followup": followup,
                "rounds": [{
                    "round":   1,
                    "type":    "outreach",
                    "sent_at": now.strftime("%Y-%m-%d %H:%M"),
                    "subject": subject,
                    "body":    body[:500],
                    "status":  "sent",
                }],
                "notes": "",
            }
        else:
            rounds = contacts[key].get("rounds", [])
            rounds.append({
                "round":   len(rounds) + 1,
                "type":    "outreach",
                "sent_at": now.strftime("%Y-%m-%d %H:%M"),
                "subject": subject,
                "body":    body[:500],
                "status":  "sent",
            })
            contacts[key]["rounds"] = rounds
            contacts[key]["status"] = "sent"

        crm["contacts"] = contacts
        _save_crm(crm, username)
        # ── 同步更新 MongoDB ox_contacts 状态 ──────────────────
        try:
            from db import get_db, is_available
            if is_available():
                _db = get_db()
                _db.ox_contacts.update_many(
                    {"username": username, "crm_key": key},
                    {"$set": {
                        "status": "sent",
                        "sent_at": now.strftime("%Y-%m-%d %H:%M"),
                        "next_followup": followup,
                    }}
                )
        except Exception as _me:
            print(f"MongoDB status同步失败: {_me}")
    except Exception as e:
        print(f"CRM写入失败: {e}")


# ============================================================
# 从联系人列表选最优目标
# ============================================================

def pick_best_contact(contacts):
    """优先 Hunter.io / 已验证 / 官网直接获取"""
    if not contacts:
        return None
    priority_order = ["Hunter.io", "✅已验证", "官网直接获取", "FDA", "WHOIS直接获取"]
    valid = [c for c in contacts if "❌验证失败" not in c.get("email_status", "")]
    if not valid:
        valid = contacts

    def score(c):
        s = c.get("email_status", "") + c.get("source", "")
        for i, kw in enumerate(priority_order):
            if kw in s:
                return i
        return len(priority_order)

    valid.sort(key=score)
    return valid[0]


# ============================================================
# 批量发信（Pipeline Phase 3 调用）
# ============================================================

def send_emails_for_results(all_results, dry_run=False,
                             sender_email="", user_smtp_pass="", username=""):
    """
    对所有成功分析的公司发送开发信。
    增加 user_smtp_pass 参数，供 Pipeline 传入当前用户的密码。
    """
    if not dry_run and not user_smtp_pass and not setup_smtp_config():
        logger.error("  ❌ SMTP未配置，跳过发信")
        return {"sent": 0, "skipped": len(all_results), "failed": 0}

    if not dry_run:
        if not test_smtp_connection(
            user=os.getenv("SMTP_USER") or sender_email,
            password=user_smtp_pass or os.getenv("SMTP_PASSWORD"),
        ):
            logger.error("  ❌ SMTP连接失败，取消发信")
            return {"sent": 0, "skipped": 0, "failed": 0}

    summary = {"sent": 0, "skipped": 0, "failed": 0}
    print(f"\n{'='*60}\n  📤 {'【预演】' if dry_run else ''}开始批量发信\n{'='*60}")

    for result in all_results:
        if result.get("status") != "success":
            summary["skipped"] += 1
            continue

        company  = result.get("company", "")
        contacts = result.get("contacts", [])
        analysis = result.get("analysis", "")

        target = pick_best_contact(contacts)
        if not target or not target.get("email"):
            logger.warning(f"  ⚠️ {company}：无有效联系人，跳过")
            summary["skipped"] += 1
            continue

        to_email = target.get("email", "")
        to_name  = target.get("name", "")
        subject, body = extract_email_from_analysis(analysis, company)
        if not subject or not body:
            logger.warning(f"  ⚠️ {company}：未能提取开发信，跳过")
            summary["skipped"] += 1
            continue

        print(f"\n  📬 {'[预演] ' if dry_run else ''}→ {company} | {to_email}")

        if dry_run:
            summary["sent"] += 1
            _record_send_log(username, sender_email, to_name, to_email, subject, "预演", "dry_run")
            try:
                from follow_up import record_first_send
                record_first_send(company, to_name, to_email, subject, body, sender_email, status="dry_run")
            except Exception:
                pass
            continue

        ok, err = send_single_email(
            to_email, to_name, subject, body,
            sender_email=sender_email,
            username=username,
            user_smtp_pass=user_smtp_pass,
        )

        if ok:
            logger.info(f"  ✅ {company} → {to_email}")
            summary["sent"] += 1
            try:
                from follow_up import record_first_send
                record_first_send(company, to_name, to_email, subject, body, sender_email, status="sent")
            except Exception:
                pass
        else:
            logger.error(f"  ❌ {company} → {err}")
            summary["failed"] += 1
            _record_send_log(username, sender_email, to_name, to_email, subject, "失败", err)

        if result != all_results[-1]:
            delay = random.randint(8, 15)
            logger.info(f"  ⏳ 等待 {delay}s...")
            time.sleep(delay)

    print(f"\n{'='*60}\n  📊 完成{'（预演）' if dry_run else ''} | 成功{summary['sent']} 跳过{summary['skipped']} 失败{summary['failed']}\n{'='*60}")
    return summary


# ============================================================
# 从 AI 分析结果提取开发信主题和正文
# ============================================================

def extract_email_from_analysis(analysis_text, company_name):
    import re
    if not analysis_text:
        return "", ""

    section_match = re.search(
        r'(?:#{1,3}\s*|^\*{1,2})?4[\.、]\s*开发信',
        analysis_text, re.IGNORECASE | re.MULTILINE
    )
    if not section_match:
        section_match = re.search(
            r'(?:#{1,3}\s*)?4[\.、]\s*(?:outreach|email|cold\s*email)',
            analysis_text, re.IGNORECASE | re.MULTILINE
        )

    if section_match:
        email_section = analysis_text[section_match.end():]
        next_sec = re.search(r'\n(?:#{1,3}\s*\d|^\*{1,2}\d|\*{0,2}5[\.、])', email_section, re.MULTILINE)
        if next_sec:
            email_section = email_section[:next_sec.start()]
    else:
        email_section = analysis_text

    subject_match = re.search(r'\*{0,2}Subject\*{0,2}[:\s*]+(.+?)[\n\r]', email_section, re.IGNORECASE)
    subject = (subject_match.group(1).strip().strip("*>：:").strip()
               if subject_match else f"Supply Partnership – Kingphar Medical")

    body_raw = email_section[subject_match.end():].strip() if subject_match else email_section.strip()
    body = re.sub(r'\*{1,2}([^*\n]+)\*{1,2}', r'\1', body_raw)
    body = re.sub(r'`{1,3}([^`]+)`{1,3}', r'\1', body)
    body = re.sub(r'^[-–—>#]+\s*', '', body, flags=re.MULTILINE)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()

    if not body or len(body) < 30:
        body = (
            f"Dear {company_name} Team,\n\n"
            f"I'm reaching out from Kingphar Medical, a leading Chinese manufacturer "
            f"of medical disposables (FDA registered since 1998, CE MDR, ISO 13485).\n\n"
            f"We specialize in gauze, bandages, and wound care products. "
            f"I'd love to explore a supply partnership.\n\n"
            f"Could we schedule a brief call this week?\n\nBest regards,\nKingphar Medical"
        )
    return subject, body
