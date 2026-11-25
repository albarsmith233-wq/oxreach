# ============================================================
# Ox · app.py  — Flask Backend
# ============================================================
import os, json, time, threading
from billing import (get_balance, add_balance, get_transactions,
                     get_total_spent, get_all_balances, get_total_topup)
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, Response)
from dotenv import load_dotenv
# --- 在 app.py 中新增 ---
from billing import (get_balance, add_balance, get_transactions,
                     get_total_spent, get_all_balances)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


app = Flask(__name__, template_folder='.')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "ox-change-this-in-production")
app.config["SESSION_COOKIE_HTTPONLY"] = True
# ─── Simple Markdown → HTML converter ───────────────────────
def _md2html(text: str) -> str:
    """把 AI 分析里的 Markdown 转成可读 HTML"""
    import re
    if not text:
        return ""
    t = text.strip()
    # 粗体
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong></strong>', t)
    # 斜体
    t = re.sub(r'\*(.+?)\*', r'<em></em>', t)
    # 标题 ###
    t = re.sub(r'^###\s+(.+)$', r'<div class="md-h3"></div>', t, flags=re.MULTILINE)
    t = re.sub(r'^##\s+(.+)$',  r'<div class="md-h2"></div>', t, flags=re.MULTILINE)
    t = re.sub(r'^#\s+(.+)$',   r'<div class="md-h1"></div>', t, flags=re.MULTILINE)
    # 有序列表
    t = re.sub(r'^(\d+)[\.、]\s+(.+)$',
               r'<div class="md-li"><span class="md-num">.</span></div>',
               t, flags=re.MULTILINE)
    # 无序列表
    t = re.sub(r'^[-•·]\s+(.+)$',
               r'<div class="md-li"><span class="md-dot">·</span></div>',
               t, flags=re.MULTILINE)
    # 箭头
    t = t.replace('→', '<span class="md-arrow">→</span>')
    # 分隔线
    t = re.sub(r'^---+$', r'<hr class="md-hr">', t, flags=re.MULTILINE)
    # 段落（两个换行 = 间距）
    t = re.sub(r'\n\n+', '\n<div class="md-gap"></div>\n', t)
    t = t.replace('\n', '<br>')
    return t

# ─── Auth decorators ─────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

@app.route('/api/balance')
@login_required
def api_balance():
    """当前用户余额"""
    bal = get_balance(session['username'])
    spent = get_total_spent(session['username'])
    return jsonify({"balance": bal, "total_spent": spent})

@app.route('/api/transactions')
@login_required
def api_transactions():
    """当前用户流水"""
    txns = get_transactions(session['username'], limit=50)
    # datetime 序列化
    for t in txns:
        if 'created_at' in t:
            t['created_at'] = t['created_at'].isoformat()
    return jsonify(txns)

@app.route('/admin/topup', methods=['POST'])
@login_required
def admin_topup():
    """管理员给用户充值"""
    if session.get('role') != 'admin':
        return jsonify({"ok": False, "error": "无权限"}), 403
    data = request.json or {}
    target = data.get('username', '').strip()
    amount = float(data.get('amount', 0))
    note = data.get('note', '')
    if not target or amount <= 0:
        return jsonify({"ok": False, "error": "参数错误"})
    result = add_balance(target, amount, admin_user=session['username'], note=note)
    return jsonify(result)

@app.route('/admin/balances')
@login_required
def admin_balances():
    if session.get('role') != 'admin':
        return jsonify({"ok": False, "error": "无权限"}), 403
    from billing import get_total_topup
    balances = get_all_balances()
    for b in balances:
        b['total_topup'] = get_total_topup(b['username'])
        b['total_spent'] = get_total_spent(b['username'])
    return jsonify(balances)

# ─── Config helpers ──────────────────────────────────────────
def _load_user_config(username):
    try:
        from db import get_db, is_available
        if is_available():
            doc = get_db().user_configs.find_one({"username": username}, {"_id": 0})
            if doc:
                return {k: v for k, v in doc.items() if k != "username"}
    except Exception:
        pass
    path = f"ox_config_{username}.json"
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception:
            pass
    return {"product":"Medical gauze, bandages, wound care disposables",
            "countries":["UK","Germany","France"],"n_targets":5,
            "dry_run":True,"batch_interval_minutes":120}

def _save_user_config(username, config):
    try:
        from db import get_db, is_available
        if is_available():
            get_db().user_configs.update_one({"username":username},
                {"$set":{"username":username,**config}},upsert=True)
            return
    except Exception:
        pass
    with open(f"ox_config_{username}.json","w") as f:
        json.dump(config,f,ensure_ascii=False,indent=2)

def _load_imap_config(username):
    try:
        from db import get_db, is_available
        if is_available():
            doc = get_db().imap_config.find_one({"username":username},{"_id":0})
            if doc: return {k:v for k,v in doc.items() if k!="username"}
    except Exception:
        pass
    path = f"imap_config_{username}.json"
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception:
            pass
    return {}

def _save_imap_config(username, config):
    try:
        from db import get_db, is_available
        if is_available():
            get_db().imap_config.update_one({"username":username},
                {"$set":{"username":username,**config}},upsert=True)
            return
    except Exception:
        pass
    with open(f"imap_config_{username}.json","w") as f:
        json.dump(config,f,ensure_ascii=False,indent=2)

def _get_smtp_pass(username):
    """Read smtp_pass fresh from DB so changes take effect immediately"""
    try:
        from auth import get_user
        u = get_user(username)
        if u and u.get("smtp_pass"):
            try:
                session["smtp_pass"] = u["smtp_pass"]
            except RuntimeError:
                pass
            return u["smtp_pass"]
    except Exception:
        pass
    try:
        return session.get("smtp_pass", "")
    except RuntimeError:
        return ""

def _ctx():
    """Common template context vars"""
    username = session.get("username", "")
    smtp_pass = _get_smtp_pass(username) if username else ""
    return dict(
        username=username,
        name=session.get("name", ""),
        email=session.get("email", ""),
        role=session.get("role", ""),
        smtp_ok=bool(smtp_pass),
    )

# ─── Auth routes ─────────────────────────────────────────────
@app.route("/register", methods=["GET","POST"])
def register():
    error  = None
    success = None
    if request.method == "POST":
        username  = request.form.get("username","").strip().lower()
        name      = request.form.get("name","").strip()
        email     = request.form.get("email","").strip().lower()
        company   = request.form.get("company","").strip()
        password  = request.form.get("password","").strip()
        password2 = request.form.get("password2","").strip()

        if not all([username, name, email, company, password]):
            error = "请填写所有字段"
        elif len(username) < 3:
            error = "用户名至少3个字符"
        elif len(password) < 6:
            error = "密码至少6位"
        elif password != password2:
            error = "两次密码不一致"
        else:
            try:
                from auth import add_user
                ok, msg = add_user(username, name, email, password, "")
                if ok:
                    # 设置试用套餐 + 公司信息
                    try:
                        from db import get_db, is_available
                        if is_available():
                            db = get_db()
                            db.user_configs.update_one(
                                {"username": username},
                                {"$set": {
                                    "username":    username,
                                    "plan":        "trial",
                                    "company_name": company,
                                    "product":     "",
                                    "countries":   ["UK","Germany","USA"],
                                    "n_targets":   5,
                                    "dry_run":     True,
                                    "sender_name": name,
                                    "registered_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                                }},
                                upsert=True
                            )
                    except Exception:
                        pass
                    success = username
                else:
                    error = msg
            except Exception as e:
                error = f"注册失败: {e}"

    return render_template("register.html", error=error, success=success)

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username","").strip().lower()
        p = request.form.get("password","").strip()
        if not u or not p:
            error = "Please fill in all fields"
        else:
            try:
                from auth import authenticate
                user = authenticate(u, p)
                if user:
                    session.clear()
                    session.update(logged_in=True,username=u,
                        name=user.get("name",""),email=user.get("email",""),
                        role=user.get("role","operator"),smtp_pass=user.get("smtp_pass",""))
                    return redirect(url_for("dashboard"))
                else:
                    error = "Invalid username or password"
            except Exception as e:
                error = f"Auth error: {e}"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── Dashboard ───────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    cfg = _load_user_config(username)
    try:
        import scheduler_engine as eng
        state = eng.get_state(username).snapshot()
    except Exception:
        state = {"running":False,"phase":"idle","progress":0,"message":"",
                 "stats":{},"recent_activity":[],"current_batch":[]}
    return render_template("dashboard.html", state=state, cfg=cfg,
        api_ok=bool(os.getenv("ANTHROPIC_API_KEY","")), **_ctx())

@app.route("/api/state")
@login_required
def api_state():
    username = session.get("username", "default")
    try:
        import scheduler_engine as eng
        return jsonify(eng.get_state(username).snapshot())
    except Exception:
        return jsonify({"running":False,"phase":"idle","progress":0,"message":"",
                        "stats":{},"recent_activity":[],"current_batch":[]})

@app.route("/stream")
@login_required
def stream():
    # 在进入生成器前捕获 username（session 在生成器内不可用）
    username = session.get("username", "default")
    def generate():
        while True:
            try:
                import scheduler_engine as eng
                data = eng.get_state(username).snapshot()
            except Exception:
                data = {"running":False,"phase":"idle","progress":0}
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(2)
    return Response(generate(), mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/start", methods=["POST"])
@login_required
def start_engine():
    username = session["username"]
    cfg = _load_user_config(username)
    try:
        import scheduler_engine as eng
        if not eng.is_running(username):
            smtp_pass = _get_smtp_pass(username) or os.getenv("SMTP_PASSWORD","")
            run_cfg = {**cfg,
                "sender_email": session.get("email","") or os.getenv("SMTP_USER",""),
                "smtp_pass":    smtp_pass,
            }
            eng.start(run_cfg, username)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500

@app.route("/stop", methods=["POST"])
@login_required
def stop_engine():
    try:
        import scheduler_engine as eng
        username = session.get("username","default")
        eng.stop(username)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500

# ─── Contacts ────────────────────────────────────────────────
# 替换 app.py 中从 @app.route("/contacts") 到 export_contacts() 末尾的整段

@app.route("/contacts")
@login_required
def contacts():
    username = session["username"]
    all_c = []

    def _parse_contact(c, key_field="crm_key"):
        return {
            "_key":          c.get(key_field,"") or c.get("key",""),
            "company":       c.get("company",""),
            "country":       c.get("country",""),
            "name":          c.get("contact_name",""),
            "title":         c.get("title",""),
            "email":         c.get("email",""),
            "phone":         c.get("phone",""),
            "whatsapp":      c.get("whatsapp","") or c.get("phone",""),
            "linkedin":      c.get("linkedin",""),
            "status":        c.get("status","ready_to_send"),
            "sent_at":       c.get("sent_at",""),
            "found_at":      c.get("found_at",""),
            "email_subject": c.get("email_subject",""),
            "email_body":    c.get("email_body",""),
            "entry_point":   c.get("entry_point",""),
            "rounds":        len(c.get("rounds",[])),
            "source":        c.get("source",""),
            "email_status":  c.get("email_status",""),
            "analysis_full": _md2html(c.get("analysis_full","") or c.get("analysis_snippet","")),
        }

    # ── 筛选参数（必须在分页之前）──
    q                = request.args.get("q","").lower()
    wa_only          = request.args.get("wa","") == "1"
    ready_only       = request.args.get("ready","") == "1"
    sent_only        = request.args.get("sent","") == "1"
    selected_country = request.args.get("country","")
    selected_company = request.args.get("company","").lower()
    sort_by          = request.args.get("sort","date")

    # ── 分页参数 ──
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 30  # 每页30家公司

    try:
        from db import get_db, is_available
        if is_available():
            db = get_db()
            base_query = {"username": username}

            # 快速统计（不拉数据）
            _total_raw   = db.ox_contacts.count_documents(base_query)
            _wa_raw      = db.ox_contacts.count_documents({**base_query, "$or":[{"whatsapp":{"$exists":True,"$ne":""}},{"phone":{"$exists":True,"$ne":""}}]})
            _ready_raw   = db.ox_contacts.count_documents({**base_query, "status":"ready_to_send"})
            _sent_raw    = db.ox_contacts.count_documents({**base_query, "status":"sent"})
            _verified_raw= db.ox_contacts.count_documents({**base_query, "email_status":{"$regex":"✅"}})

            # 国家统计（aggregation，不拉全量）
            _country_agg = list(db.ox_contacts.aggregate([
                {"$match": base_query},
                {"$group": {"_id":"$country","count":{"$sum":1}}},
                {"$sort": {"count":-1}},
                {"$limit": 10}
            ]))
            _country_stats_db = {r["_id"] or "其他": r["count"] for r in _country_agg}

            # 构建查询条件（带筛选）
            filter_q = dict(base_query)
            if q:
                filter_q["$or"] = [
                    {"company":{"$regex":q,"$options":"i"}},
                    {"contact_name":{"$regex":q,"$options":"i"}},
                    {"email":{"$regex":q,"$options":"i"}},
                ]
            if selected_country:
                filter_q["country"] = {"$regex": f"^{selected_country}$", "$options":"i"}
            if selected_company:
                filter_q["company"] = {"$regex": f"^{selected_company}$", "$options":"i"}
            if wa_only:
                filter_q["$or"] = [{"whatsapp":{"$exists":True,"$ne":""}},{"phone":{"$exists":True,"$ne":""}}]
            if ready_only:
                filter_q["status"] = "ready_to_send"
            if sent_only:
                filter_q["status"] = "sent"

            # 排序
            sort_field = [("found_at",-1)]
            if sort_by == "name":
                sort_field = [("company",1)]

            # 先拿当前筛选下的公司名列表（去重），再分页
            _co_names = db.ox_contacts.distinct("company", filter_q)
            if sort_by == "contacts":
                # 按联系人数排序公司
                _co_cnt = list(db.ox_contacts.aggregate([
                    {"$match": filter_q},
                    {"$group":{"_id":"$company","n":{"$sum":1}}},
                    {"$sort":{"n":-1}}
                ]))
                _co_names = [r["_id"] for r in _co_cnt]
            elif sort_by == "name":
                _co_names = sorted(_co_names)
            else:
                # 按最新found_at排序公司
                _co_latest = list(db.ox_contacts.aggregate([
                    {"$match": filter_q},
                    {"$group":{"_id":"$company","latest":{"$max":"$found_at"}}},
                    {"$sort":{"latest":-1}}
                ]))
                _co_names = [r["_id"] for r in _co_latest]

            total_companies = len(_co_names)
            total_pages = max(1, (total_companies + per_page - 1) // per_page)
            page = min(page, total_pages)
            page_co_names = _co_names[(page-1)*per_page : page*per_page]

            # 只拉当前页公司的联系人
            for c in db.ox_contacts.find(
                {**base_query, "company": {"$in": page_co_names}},
                {"_id": 0}
            ):
                all_c.append(_parse_contact(c, "crm_key"))

    except Exception as _pe:
        import traceback; traceback.print_exc()
        total_companies = 0
        total_pages = 1
        _total_raw = _wa_raw = _ready_raw = _sent_raw = _verified_raw = 0
        _country_stats_db = {}
        _co_names = []
        page_co_names = []

    # 降级：从 follow_up 文件读
    if not all_c:
        try:
            import follow_up as _fu
            crm = _fu._load_crm(username)
            for key, c in crm.get("contacts",{}).items():
                parsed = _parse_contact(c, "crm_key")
                parsed["_key"] = key
                all_c.append(parsed)
        except Exception:
            pass

    # ── 筛选参数已在上方定义 ──

    # 文字搜索
    if q:
        all_c = [c for c in all_c if q in c.get("company","").lower()
                 or q in c.get("name","").lower()
                 or q in c.get("email","").lower()
                 or q in c.get("country","").lower()]
    # 国家筛选
    if selected_country:
        all_c = [c for c in all_c if c.get("country","").lower() == selected_country.lower()]
    # 公司筛选
    if selected_company:
        all_c = [c for c in all_c if c.get("company","").lower() == selected_company]
    # 状态筛选
    if wa_only:
        all_c = [c for c in all_c if c.get("whatsapp") or c.get("phone")]
    if ready_only:
        all_c = [c for c in all_c if c.get("status") == "ready_to_send"]
    if sent_only:
        all_c = [c for c in all_c if c.get("status") == "sent"]

    # ── 排序 ──
    if sort_by == "date":
        # 按 found_at 倒序（最新的在前），无日期的排最后
        all_c.sort(key=lambda c: c.get("found_at","") or "0000", reverse=True)
    elif sort_by == "name":
        all_c.sort(key=lambda c: c.get("company","").lower())
    # contacts 排序在分组后处理

    # ── 按公司分组 ──
    companies = {}
    for c in all_c:
        co = c.get("company","") or "Unknown"
        companies.setdefault(co, []).append(c)

    # 如果按联系人数排序，重新排列 companies dict
    if sort_by == "contacts":
        companies = dict(sorted(companies.items(), key=lambda x: len(x[1]), reverse=True))

    # ── 统计 ──
    wa_count       = sum(1 for c in all_c if c.get("whatsapp") or c.get("phone"))
    ready_count    = sum(1 for c in all_c if c.get("status") == "ready_to_send")
    sent_count     = sum(1 for c in all_c if c.get("status") == "sent")
    verified_count = sum(1 for c in all_c if "✅" in c.get("email_status",""))

    # ── 国家统计（用于 filter chips）──
    country_stats = {}
    # 从未筛选的全量数据计算国家分布（如果当前有筛选则从 DB 重新算）
    for c in all_c:
        cn = c.get("country","") or "其他"
        country_stats[cn] = country_stats.get(cn, 0) + 1
    # 按数量倒序，只展示 top 10
    country_stats = dict(sorted(country_stats.items(), key=lambda x: x[1], reverse=True)[:10])

    # ── 构建 filter URL helper（传给模板）──
    def build_filter_url(**overrides):
        """生成筛选链接，保留当前参数"""
        params = {
            "q": q, "wa": "1" if wa_only else "",
            "ready": "1" if ready_only else "",
            "sent": "1" if sent_only else "",
            "country": selected_country,
            "company": selected_company,
            "sort": sort_by,
            "page": str(page) if page > 1 else "",
        }
        params.update(overrides)
        parts = [f"{k}={v}" for k, v in params.items() if v]
        return "/contacts?" + "&".join(parts) if parts else "/contacts"

    return render_template("contacts.html",
        companies=companies,
        total_contacts=locals().get("_total_raw", len(all_c)),
        page=page, total_pages=total_pages,
        per_page=per_page,
        wa_count=wa_count,
        ready_count=ready_count,
        sent_count=sent_count,
        verified_count=verified_count,
        country_stats=country_stats,
        selected_country=selected_country,
        selected_company=selected_company,
        sort_by=sort_by,
        sent_only=sent_only,
        q=q, wa_only=wa_only, ready_only=ready_only,
        build_filter_url=build_filter_url,
        **_ctx())




# ════════════════════════════════════════════════════════════
# Patch v2: 问题1 — 已有联系人批量发信
# ════════════════════════════════════════════════════════════

@app.route("/contacts/send", methods=["POST"])
@login_required
def contacts_send():
    """对 ready_to_send 联系人批量/指定发信，支持发信间隔配置"""
    import random, time as _t
    username = session["username"]
    data = request.json or {}
    company_filter = data.get("company", "")
    send_interval = data.get("interval", 10)  # 默认10秒，前端可配
    try:
        send_interval = max(3, min(120, int(send_interval)))
    except (ValueError, TypeError):
        send_interval = 10

    # ── SMTP 配置：统一用 _get_smtp_pass 读取 ──
    sender = ""
    smtp_pass = ""
    try:
        from db import get_db, is_available
        if not is_available():
            return jsonify({"ok": False, "msg": "数据库不可用"})
        db = get_db()
        cfg = db.user_configs.find_one({"username": username}) or {}
        sender = cfg.get("sender_email") or session.get("email", "")
        # 优先 _get_smtp_pass（从 accounts 集合读）
        smtp_pass = _get_smtp_pass(username)
        if not smtp_pass:
            smtp_pass = cfg.get("smtp_pass", "")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"数据库错误: {e}"})

    if not sender:
        return jsonify({"ok": False, "msg": "请先在设置中配置发信邮箱"})
    if not smtp_pass:
        return jsonify({"ok": False, "msg": "请先在设置中配置SMTP密码"})

    query = {"username": username, "status": "ready_to_send"}
    if company_filter:
        query["company"] = company_filter
    contacts = list(db.ox_contacts.find(query).limit(50))
    if not contacts:
        return jsonify({"ok": False, "msg": "无待发送联系人"})

    # ── 将发信任务放入后台线程，立即返回 ──
    import threading
    task_id = f"send_{username}_{int(__import__('time').time())}"

    # 全局发信进度存储
    if not hasattr(app, '_send_progress'):
        app._send_progress = {}
    app._send_progress[username] = {
        "task_id": task_id, "total": len(contacts),
        "sent": 0, "failed": 0, "current": "",
        "running": True, "errors": [],
    }

    def _bg_send(contacts_list, sender_email, smtp_password, uname, interval, db_ref, smtp_prov=""):
        from email_sender import send_single_email
        prog = app._send_progress[uname]
        for i, c in enumerate(contacts_list):
            if not prog["running"]:
                break
            email = c.get("email", "")
            if not email:
                continue
            subject = c.get("email_subject") or f"Partnership Inquiry — {c.get('company','')}"
            body = c.get("email_body", "")
            if not body or len(body) < 20:
                continue
            prog["current"] = f"{c.get('company','')} → {email}"
            ok, msg = send_single_email(
                to_email=email, to_name=c.get("contact_name", ""),
                subject=subject, body=body,
                sender_email=sender_email, username=uname,
                user_smtp_pass=smtp_password,
                smtp_provider=smtp_prov,
            )
            if ok:
                db_ref.ox_contacts.update_one({"_id": c["_id"]}, {"$set": {
                    "status": "sent",
                    "sent_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                }})
                try:
                    from follow_up import record_first_send
                    record_first_send(
                        company=c.get("company",""), contact_name=c.get("contact_name",""),
                        email=email, subject=subject, body=body, sender=sender_email,
                    )
                except Exception:
                    pass
                prog["sent"] += 1
            else:
                prog["failed"] += 1
                prog["errors"].append(f"{c.get('company','')}: {msg[:60]}")
            # 间隔（±30% 随机化防检测）
            jitter = random.uniform(0.7, 1.3)
            if i < len(contacts_list) - 1:
                _t.sleep(int(interval * jitter))
        prog["running"] = False
        prog["current"] = ""

    t = threading.Thread(
        target=_bg_send,
        args=(contacts, sender, smtp_pass, username, send_interval, db, smtp_provider),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "msg": f"发信任务已启动：{len(contacts)} 封", "task_id": task_id})



# ════════════════════════════════════════════════════════════
# Patch v2: 问题3 — 跟进提醒 API
# ════════════════════════════════════════════════════════════

@app.route("/api/followup/due")
@login_required
def api_followup_due():
    """返回当前用户到期跟进列表"""
    from follow_up import get_due_followups
    username = session["username"]
    due = get_due_followups(username=username)
    return jsonify({
        "count": len(due),
        "items": [{
            "key":      d["_key"],
            "company":  d.get("company", ""),
            "email":    d.get("email", ""),
            "contact":  d.get("contact_name", ""),
            "round":    d.get("_round", 2),
            "overdue":  d.get("_days_overdue", 0),
            "due_date": d.get("_due_date", ""),
        } for d in due[:20]]
    })


@app.route("/api/followup/send", methods=["POST"])
@login_required
def api_followup_send():
    """手动触发单个跟进：AI生成 + 发送（或仅预览）"""
    from follow_up import (get_due_followups, generate_followup_email,
                           send_followup_mt)
    username = session["username"]
    data = request.json or {}
    key = data.get("key", "")

    due = get_due_followups(username=username)
    item = next((d for d in due if d["_key"] == key), None)
    if not item:
        return jsonify({"ok": False, "msg": "该联系人无到期跟进"})

    try:
        from company_config import get_user_profile
        profile = get_user_profile(username)
    except Exception:
        profile = {}

    subj, body = generate_followup_email(item, item["_round"], profile)

    # 预览模式
    if data.get("preview"):
        return jsonify({"ok": True, "subject": subj, "body": body})

    # 真实发送
    try:
        from db import get_db
        cfg = get_db().user_configs.find_one({"username": username}) or {}
    except Exception:
        cfg = {}
    sender    = cfg.get("sender_email") or session.get("email", "")
    smtp_pass = _get_smtp_pass(username)
    smtp_provider = cfg.get("smtp_provider", "aliyun")
    if not sender or not smtp_pass:
        return jsonify({"ok": False, "msg": "请先配置SMTP"})

    ok, msg = send_followup_mt(
        key, subj, body,
        username=username, sender=sender, smtp_pass=smtp_pass,
    )
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/followup/dismiss", methods=["POST"])
@login_required
def api_followup_dismiss():
    """推迟跟进"""
    from follow_up import postpone_followup
    data = request.json or {}
    ok, msg = postpone_followup(
        data.get("key", ""),
        days=data.get("days", 3),
        username=session["username"],
    )
    return jsonify({"ok": ok, "msg": msg})


@app.route("/contacts/export")
@login_required
def export_contacts():
    """导出CSV — 从 MongoDB 读取（修复空导出问题）"""
    import csv, io
    username = session["username"]
    rows = []

    try:
        from db import get_db, is_available
        if is_available():
            db = get_db()
            seen = set()
            # 主集合
            for c in db.ox_contacts.find({"username": username}, {"_id": 0}):
                key = c.get("crm_key","") or c.get("key","")
                if key in seen: continue
                seen.add(key)
                rows.append([
                    c.get("company",""),
                    c.get("country",""),
                    c.get("contact_name",""),
                    c.get("title",""),
                    c.get("whatsapp","") or c.get("phone",""),
                    c.get("email",""),
                    c.get("email_status",""),
                    c.get("linkedin",""),
                    c.get("source",""),
                    c.get("status",""),
                    c.get("found_at",""),
                    c.get("sent_at",""),
                ])
            # 旧集合
            for c in db.crm.find({"username": username}, {"_id": 0}):
                key = c.get("crm_key","") or c.get("key","")
                if key in seen: continue
                seen.add(key)
                rows.append([
                    c.get("company",""),
                    c.get("country",""),
                    c.get("contact_name",""),
                    c.get("title",""),
                    c.get("whatsapp","") or c.get("phone",""),
                    c.get("email",""),
                    c.get("email_status",""),
                    c.get("linkedin",""),
                    c.get("source",""),
                    c.get("status",""),
                    c.get("found_at",""),
                    c.get("sent_at",""),
                ])
    except Exception:
        pass

    # 降级：follow_up 文件
    if not rows:
        try:
            import follow_up as _fu
            crm = _fu._load_crm(username)
            for c in crm.get("contacts",{}).values():
                rows.append([
                    c.get("company",""),
                    c.get("country",""),
                    c.get("contact_name",""),
                    c.get("title",""),
                    c.get("whatsapp","") or c.get("phone",""),
                    c.get("email",""),
                    c.get("email_status",""),
                    c.get("linkedin",""),
                    c.get("source",""),
                    c.get("status",""),
                    c.get("found_at",""),
                    c.get("sent_at",""),
                ])
        except Exception:
            pass

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["公司","国家","联系人","职位","WhatsApp","邮箱","邮箱状态","LinkedIn","来源","状态","发现时间","发送时间"])
    w.writerows(rows)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ox_contacts_{datetime.now().strftime('%Y%m%d')}.csv"})
# ─── Settings ────────────────────────────────────────────────
COUNTRY_OPTIONS = ["USA","UK","Germany","France","Netherlands","Belgium","Switzerland",
    "Austria","Sweden","Denmark","Norway","Finland","Italy","Spain","Portugal",
    "Poland","Czech Republic","Romania","Hungary","Greece","UAE","Saudi Arabia",
    "Turkey","Israel","Qatar","Kuwait","Egypt","Japan","South Korea","India",
    "Singapore","Thailand","Vietnam","Malaysia","Indonesia","Philippines",
    "Bangladesh","Pakistan","Australia","New Zealand","Brazil","Argentina",
    "Chile","Colombia","South Africa","Nigeria","Kenya","Canada","Mexico"]

@app.route("/settings", methods=["GET"])
@login_required
def settings():
    username = session["username"]
    cfg      = _load_user_config(username)
    imap_cfg = _load_imap_config(username)
    imap_presets = {}
    try:
        from reply_detector import IMAP_PRESETS
        imap_presets = IMAP_PRESETS
    except Exception:
        imap_presets = {"aliyun":{"name":"Alibaba Mail","host":"imap.qiye.aliyun.com","port":993},
                        "gmail":{"name":"Gmail","host":"imap.gmail.com","port":993},
                        "outlook":{"name":"Outlook","host":"outlook.office365.com","port":993}}
    smtp_pass_val = _get_smtp_pass(username)
    return render_template("settings.html", cfg=cfg, imap_cfg=imap_cfg,
        imap_presets=imap_presets, country_options=COUNTRY_OPTIONS,
        smtp_pass_val=smtp_pass_val,
        has_apollo=bool(os.getenv("APOLLO_API_KEY","")),
        has_hunter=bool(os.getenv("HUNTER_API_KEY","")),
        has_anthropic=bool(os.getenv("ANTHROPIC_API_KEY","")),
        has_brevo=bool(os.getenv("BREVO_API_KEY","")),
        saved=request.args.get("saved",""), **_ctx())

@app.route("/settings", methods=["POST"])
@login_required
def settings_save():
    username = session["username"]
    form = request.form
    smtp_pass = form.get("smtp_pass","").strip()
    smtp_err = ""
    if smtp_pass:
        try:
            from auth import update_user_smtp
            ok, msg = update_user_smtp(username, smtp_pass)
            if ok:
                session["smtp_pass"] = smtp_pass
            else:
                smtp_err = msg or "SMTP密码保存失败"
        except Exception as e:
            smtp_err = f"SMTP密码保存异常: {e}"
    countries = form.getlist("countries")
    try: n_targets = int(form.get("n_targets",5))
    except: n_targets = 5
    try: interval = int(form.get("interval_hours",2)) * 60
    except: interval = 120
    _save_user_config(username, {
        "product":      form.get("product","").strip(),
        "countries":    countries,
        "n_targets":    n_targets,
        "dry_run":      form.get("dry_run")=="1",
        "batch_interval_minutes": interval,
        "sender_name":  form.get("sender_name","").strip(),
        "sender_title": form.get("sender_title","").strip(),
        "smtp_provider": form.get("smtp_provider","aliyun").strip(),
        # 公司档案（通用化关键）
        "company_name":            form.get("company_name","").strip(),
        "company_industry":        form.get("company_industry","").strip(),
        "company_advantages":      form.get("company_advantages","").strip(),
        "company_target_customer": form.get("company_target_customer","").strip(),
        "company_cta":             form.get("company_cta","").strip(),
    })
    imap_provider = form.get("imap_provider","aliyun")
    imap_user = form.get("imap_user","").strip()
    imap_pass = form.get("imap_pass","").strip()
    if imap_user and imap_pass:
        try:
            from reply_detector import IMAP_PRESETS
            preset = IMAP_PRESETS.get(imap_provider,{})
            _save_imap_config(username,{"provider":imap_provider,
                "host":preset.get("host",""),"port":preset.get("port",993),
                "username":imap_user,"password":imap_pass})
        except Exception:
            pass
    for env_key, form_key in [("APOLLO_API_KEY","apollo_key"),
                               ("HUNTER_API_KEY","hunter_key"),
                               ("ANTHROPIC_API_KEY","anthropic_key"),
                               ("BREVO_API_KEY","brevo_key")]:
        val = form.get(form_key,"").strip()
        if val:
            try:
                from dotenv import set_key
                set_key(os.path.join(os.path.dirname(os.path.abspath(__file__)),".env"),env_key,val)
                os.environ[env_key] = val
            except Exception:
                pass
    return redirect(url_for("settings", saved="" if smtp_err else "1", smtp_err=smtp_err))

SMTP_PRESETS = {
    "aliyun":  {"host": "smtp.qiye.aliyun.com", "port": 465, "ssl": True},
    "tencent": {"host": "smtp.exmail.qq.com",   "port": 465, "ssl": True},
    "gmail":   {"host": "smtp.gmail.com",        "port": 587, "ssl": False},
    "outlook": {"host": "smtp.office365.com",    "port": 587, "ssl": False},
    "163":     {"host": "smtp.163.com",          "port": 465, "ssl": True},
    "qq":      {"host": "smtp.qq.com",           "port": 465, "ssl": True},
}

@app.route("/settings/test-smtp", methods=["POST"])
@login_required
def test_smtp():
    data = request.get_json()
    email    = data.get("email", "").strip()
    password = data.get("password", "").strip()
    provider = data.get("provider", "").strip()
    if not password:
        password = _get_smtp_pass(session.get("username",""))
    if not email or not password:
        return jsonify({"ok": False, "msg": "请先填写邮箱和密码"})
    if not provider:
        return jsonify({"ok": False, "msg": "请选择邮箱服务商"})
    preset  = SMTP_PRESETS.get(provider, SMTP_PRESETS["aliyun"])
    host    = preset["host"]
    port    = preset["port"]
    use_ssl = preset["ssl"]
    try:
        import smtplib
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=12) as smtp:
                smtp.ehlo()
                smtp.login(email, password)
        else:
            with smtplib.SMTP(host, port, timeout=12) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(email, password)
        return jsonify({"ok": True, "msg": f"✅ SMTP 连接成功 ({host}:{port})"})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"ok": False, "msg": "❌ 认证失败：请检查密码是否正确"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"❌ 连接失败：{e}"})

@app.route("/settings/test-imap", methods=["POST"])
@login_required
def test_imap():
    data = request.get_json()
    try:
        from reply_detector import test_connection, IMAP_PRESETS
        preset = IMAP_PRESETS.get(data.get("provider","aliyun"),{})
        ok, msg = test_connection({"host":preset.get("host",""),"port":preset.get("port",993),
            "username":data.get("username",""),"password":data.get("password","")})
        return jsonify({"ok":ok,"msg":msg})
    except Exception as e:
        return jsonify({"ok":False,"msg":str(e)})

# ─── Admin ───────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    from auth import list_users
    from usage_tracker import get_all_users_usage, calc_bill, PLANS, get_current_month, PRICE_PER_VERIFIED_CONTACT

    # 可用月份（当月 + 过去5个月）
    from datetime import date, timedelta
    months = []
    d = date.today()
    for _ in range(6):
        months.append(d.strftime("%Y-%m"))
        d = d.replace(day=1) - timedelta(days=1)
    selected_month = request.args.get("month", get_current_month())

    # 所有用户列表（套餐从 user_configs 读，与其他配置共用同一集合）
    users_raw = list_users()
    try:
        from db import get_db, is_available
        if is_available():
            db = get_db()
            for u in users_raw:
                doc = db.user_configs.find_one(
                    {"username": u["username"]}, {"plan": 1, "_id": 0}
                )
                u["plan"] = (doc or {}).get("plan", "trial")
    except Exception:
        for u in users_raw:
            u["plan"] = "trial"

    # 用量：直接从 ox_contacts 实时统计（覆盖历史数据）
    usage_rows = []
    try:
        from db import get_db, is_available
        _db_ok = is_available()
    except Exception:
        _db_ok = False

    for u in users_raw:
        uname    = u["username"]
        plan_doc = get_db().user_configs.find_one({"username": uname}, {"plan":1,"_id":0}) if _db_ok else {}
        plan_key = (plan_doc or {}).get("plan","trial")
        plan     = PLANS.get(plan_key, PLANS["trial"])

        # 直接从 ox_contacts 统计（不受 usage_log 是否存在影响）
        contacts_all     = 0
        contacts_verified = 0
        whatsapp_count   = 0
        companies_set    = set()
        email_sent       = 0

        if _db_ok:
            db = get_db()
            # ── ox_contacts 集合 ────────────────────────────
            try:
                q = {"username": uname}
                contacts_all = db.ox_contacts.count_documents(q)
                companies_set = set(db.ox_contacts.distinct("company", q))
                email_sent    = db.ox_contacts.count_documents({**q, "status": "sent"})
                # 有邮箱（非空非null）
                has_email = db.ox_contacts.count_documents({
                    **q, "email": {"$exists": True, "$nin": [None, "", " "]}
                })
                # 有WhatsApp或phone（非空非null）
                has_wa = db.ox_contacts.count_documents({
                    **q, "$or": [
                        {"whatsapp": {"$exists": True, "$nin": [None, "", " "]}},
                        {"phone":    {"$exists": True, "$nin": [None, "", " "]}},
                    ]
                })
                whatsapp_count = has_wa
                # 已验证：有邮箱或WhatsApp（用集合运算避免重复计数）
                contacts_verified = db.ox_contacts.count_documents({
                    **q, "$or": [
                        {"email":    {"$exists": True, "$nin": [None, "", " "]}},
                        {"whatsapp": {"$exists": True, "$nin": [None, "", " "]}},
                        {"phone":    {"$exists": True, "$nin": [None, "", " "]}},
                    ]
                })
            except Exception:
                pass
            # ── crm 集合（旧数据补充）───────────────────────
            try:
                qc = {"username": uname}
                crm_total = db.crm.count_documents(qc)
                crm_companies = set(db.crm.distinct("company", qc))
                crm_wa = db.crm.count_documents({
                    **qc, "$or": [
                        {"whatsapp": {"$exists": True, "$nin": [None, "", " "]}},
                        {"phone":    {"$exists": True, "$nin": [None, "", " "]}},
                    ]
                })
                crm_verified = db.crm.count_documents({
                    **qc, "$or": [
                        {"email":    {"$exists": True, "$nin": [None, "", " "]}},
                        {"whatsapp": {"$exists": True, "$nin": [None, "", " "]}},
                        {"phone":    {"$exists": True, "$nin": [None, "", " "]}},
                    ]
                })
                contacts_all      += crm_total
                companies_set     |= crm_companies
                whatsapp_count    += crm_wa
                contacts_verified += crm_verified
            except Exception:
                pass

        companies_count = len(companies_set)
        total_bill      = round(contacts_verified * PRICE_PER_VERIFIED_CONTACT, 2)

        usage_rows.append({
            "username":      uname,
            "name":          u.get("name",""),
            "month":         selected_month,
            "plan":          plan["name"],
            "plan_key":      plan_key,
            "quota":         plan["quota"],
            "used":          companies_count,
            "overage":       max(0, companies_count - plan["quota"]),
            "overage_price": 0,
            "verified":      contacts_verified,
            "unit_price":    PRICE_PER_VERIFIED_CONTACT,
            "base_fee":      0,
            "overage_fee":   0,
            "total":         total_bill,
            "usage": {
                "company_analyzed": companies_count,
                "contact_found":    contacts_all,
                "contact_verified": contacts_verified,
                "whatsapp_found":   whatsapp_count,
                "email_sent":       email_sent,
            },
        })

    # 汇总
    total_companies = sum(r["used"]     for r in usage_rows)
    total_contacts  = sum(r["usage"].get("contact_found",0) for r in usage_rows)
    total_emails    = sum(r["usage"].get("email_sent",0)    for r in usage_rows)
    total_revenue   = sum(r["total"]    for r in usage_rows)

    return render_template("admin.html",
        users=users_raw,
        plans=PLANS,
        usage_rows=usage_rows,
        available_months=months,
        selected_month=selected_month,
        usage_summary={
            "total_companies": total_companies,
            "total_contacts":  total_contacts,
            "total_emails":    total_emails,
            "total_revenue":   total_revenue,
        },
        msg=request.args.get("msg",""), **_ctx())

@app.route("/admin/set-plan", methods=["POST"])
@admin_required
def admin_set_plan():
    username = request.form.get("username","")
    plan     = request.form.get("plan","trial")
    try:
        from db import get_db, is_available
        if is_available():
            # 存入 user_configs（已存在的持久集合）
            get_db().user_configs.update_one(
                {"username": username},
                {"$set": {"plan": plan}},
                upsert=True
            )
        msg = f"✅ {username} 套餐已更新为 {plan}"
    except Exception as e:
        msg = f"❌ 更新失败: {e}"
    return redirect(url_for("admin", msg=msg))

@app.route("/admin/add", methods=["POST"])
@admin_required
def admin_add():
    from auth import add_user
    username = request.form.get("username","")
    ok, msg  = add_user(username, request.form.get("name",""),
        request.form.get("email",""), request.form.get("password",""),
        request.form.get("smtp_pass",""))
    # Set plan in user_configs
    if ok:
        plan = request.form.get("plan","trial")
        try:
            from db import get_db, is_available
            if is_available():
                get_db().user_configs.update_one(
                    {"username": username},
                    {"$set": {"plan": plan}},
                    upsert=True
                )
        except Exception:
            pass
    return redirect(url_for("admin", msg=msg))

@app.route("/admin/delete/<uname>", methods=["POST"])
@admin_required
def admin_delete(uname):
    from auth import delete_user
    ok, msg = delete_user(uname)
    return redirect(url_for("admin", msg=msg))

# ─── Railway health checks ───────────────────────────────────
@app.route("/_stcore/health")
def health():
    return "ok", 200

@app.route("/_stcore/host-config")
def host_config():
    return jsonify({"allowedOrigins": ["*"], "useExternalAuthToken": False})

@app.route("/api/enrich-linkedin-contacts", methods=["POST"])
@login_required
def enrich_linkedin_contacts():
    """
    对已有 LinkedIn 联系人批量补充推算邮箱。
    只处理：有姓名 + 有LinkedIn + 没有邮箱 的联系人。
    """
    username = session.get("username","")
    updated  = 0
    skipped  = 0
    errors   = []

    try:
        from db import get_db, is_available
        if not is_available():
            return jsonify({"error": "DB not available"})

        db   = get_db()
        from find_contacts import (generate_email_guesses,
                                   query_hunter_email_format,
                                   find_company_domain)

        # 找出有名字有LinkedIn但没邮箱的联系人
        candidates = list(db.ox_contacts.find({
            "username": username,
            "contact_name": {"$nin": [None, ""]},
            "linkedin":     {"$nin": [None, ""]},
            "$or": [
                {"email": {"$in": [None, ""]}},
                {"email": {"$exists": False}},
            ]
        }, {"_id":1, "contact_name":1, "company":1, "linkedin":1,
            "crm_key":1, "country":1}))

        print(f"  候选 LinkedIn 联系人: {len(candidates)} 个")

        # 按公司分组，减少 Hunter 调用次数
        by_company = {}
        for c in candidates:
            co = c.get("company","")
            by_company.setdefault(co, []).append(c)

        domain_fmt_cache = {}  # company → (domain, format)

        for company, contacts in list(by_company.items())[:20]:  # 每次最多20家
            # 找域名
            domain = ""
            try:
                import re
                # 从 LinkedIn URL 猜域名
                for c in contacts:
                    li = c.get("linkedin","")
                    # 尝试从公司名直接猜
                    pass
                domain = find_company_domain(company)
            except Exception:
                pass

            if not domain:
                skipped += len(contacts)
                continue

            # Hunter 格式检测（每个域名只调一次）
            fmt = domain_fmt_cache.get(domain)
            if fmt is None:
                try:
                    fmt = query_hunter_email_format(domain) or ""
                except Exception:
                    fmt = ""
                domain_fmt_cache[domain] = fmt

            for c in contacts:
                name = c.get("contact_name","").strip()
                if not name or len(name.split()) < 2:
                    skipped += 1
                    continue

                guesses = generate_email_guesses(name, domain)
                if not guesses:
                    skipped += 1
                    continue

                # 选最佳格式
                if fmt:
                    parts = name.lower().strip().split()
                    first = re.sub(r"[^a-z]","",parts[0])
                    last  = re.sub(r"[^a-z]","",parts[-1])
                    f     = first[0] if first else ""
                    if first and last:
                        best   = fmt.format(first=first,last=last,f=f,domain=domain)
                        source = "Hunter格式+LinkedIn推算"
                    else:
                        best   = guesses[0]
                        source = "LinkedIn推算（通用格式）"
                else:
                    best   = guesses[0]
                    source = "LinkedIn推算（通用格式）"

                # 更新 MongoDB
                try:
                    db.ox_contacts.update_one(
                        {"_id": c["_id"]},
                        {"$set": {
                            "email":         best,
                            "email_status":  f"推算（{source}）",
                            "email_guesses": [g for g in guesses if g != best][:2],
                            "source":        source,
                        }}
                    )
                    updated += 1
                except Exception as e:
                    errors.append(str(e))

    except Exception as e:
        return jsonify({"error": str(e)})

    return jsonify({
        "updated":  updated,
        "skipped":  skipped,
        "errors":   errors[:5],
        "message":  f"✅ 补充了 {updated} 个推算邮箱，跳过 {skipped} 个（无姓名/无域名）",
    })

@app.route("/api/test-ddg-email")
@login_required
def test_ddg_email():
    """测试 DDG 直接邮箱搜索是否有效"""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from find_contacts import search_email_by_ddg
        results = search_email_by_ddg(
            company_name="Dispomedica",
            domain="dispomedica.de",
            country="Germany"
        )
        return jsonify({
            "found": len(results),
            "sample": results[:5],
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-500:]})

@app.route("/api/test-hunter")
@login_required
def test_hunter():
    """测试 Hunter.io 是否正常工作"""
    import os, requests as _req
    key = os.getenv("HUNTER_API_KEY","")
    result = {"api_key_set": bool(key), "prefix": key[:8]+"..." if key else ""}
    if not key:
        result["status"] = "❌ HUNTER_API_KEY 未设置"
        return jsonify(result)

    # 测试1：域名搜索
    try:
        r = _req.get("https://api.hunter.io/v2/domain-search",
            params={"domain":"hartmann.info","api_key":key,"limit":3},
            timeout=10)
        d = r.json()
        if r.status_code == 200:
            emails = d.get("data",{}).get("emails",[])
            result["domain_search"] = {
                "status": f"✅ {r.status_code}",
                "emails_found": len(emails),
                "sample": [{"email":e.get("value"),"name":f"{e.get('first_name','')} {e.get('last_name','')}","confidence":e.get("confidence")} for e in emails[:3]],
            }
        else:
            result["domain_search"] = {"status":f"❌ {r.status_code}","error":str(d)[:200]}
    except Exception as e:
        result["domain_search"] = {"error":str(e)}

    # 测试2：账户额度
    try:
        r2 = _req.get("https://api.hunter.io/v2/account",
            params={"api_key":key}, timeout=10)
        d2 = r2.json()
        if r2.status_code == 200:
            data = d2.get("data",{})
            calls = data.get("calls",{})
            result["account"] = {
                "plan":       data.get("plan_name",""),
                "used":       calls.get("used",0),
                "available":  calls.get("available",0),
            }
        else:
            result["account"] = {"error": str(d2)[:150]}
    except Exception as e:
        result["account"] = {"error": str(e)}

    return jsonify(result)

@app.route("/api/test-apollo")
@login_required
def test_apollo():
    """测试 Apollo.io 是否正常工作"""
    import os, requests as _req
    api_key = os.getenv("APOLLO_API_KEY","")
    result  = {"api_key_set": bool(api_key), "api_key_prefix": api_key[:8]+"..." if api_key else ""}

    if not api_key:
        result["status"] = "❌ APOLLO_API_KEY 未设置"
        return jsonify(result)

    hdrs = {
        "Content-Type":  "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key":     api_key,   # Key 必须在 Header 里
    }

    # 测试联系人搜索
    try:
        resp = _req.post(
            "https://api.apollo.io/v1/mixed_people/search",
            headers=hdrs,
            json={
                "q_organization_name": "Hartmann",
                "organization_locations": ["Germany"],
                "page": 1, "per_page": 3,
                "person_titles": ["purchasing","procurement","supply chain"],
            },
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200:
            people = data.get("people",[])
            result["status"]       = f"✅ 联系人搜索正常 ({resp.status_code})"
            result["test_results"] = len(people)
            result["sample"]       = [{"name":p.get("name"),"title":p.get("title"),
                                       "email":p.get("email"),"org":p.get("organization",{}).get("name")}
                                      for p in people[:2]]
        else:
            result["status"] = f"❌ HTTP {resp.status_code}"
            result["error"]  = str(data)[:300]
    except Exception as e:
        result["status"] = f"❌ 请求失败: {e}"

    # 测试公司搜索
    try:
        resp2 = _req.post(
            "https://api.apollo.io/v1/mixed_companies/search",
            headers=hdrs,
            json={
                "organization_locations": ["Germany"],
                "q_organization_keyword_tags": ["medical supplies","wound care"],
                "page": 1, "per_page": 3,
            },
            timeout=15,
        )
        data2 = resp2.json()
        orgs  = data2.get("organizations",[])
        result["company_search"] = {
            "status": f"✅ {resp2.status_code}" if resp2.status_code==200 else f"❌ {resp2.status_code}: {str(data2)[:150]}",
            "count":  len(orgs),
            "sample": [{"name":o.get("name"),"domain":o.get("primary_domain")} for o in orgs[:2]],
        }
    except Exception as e:
        result["company_search"] = {"error": str(e)}

    return jsonify(result)

@app.route("/api/contact-quality")
@admin_required
def contact_quality():
    """分析联系人质量分布"""
    from db import get_db, is_available
    if not is_available():
        return jsonify({"error": "DB not available"})
    db = get_db()
    result = {}
    for col in ["ox_contacts", "crm"]:
        docs = list(db[col].find({}, {"email":1,"whatsapp":1,"phone":1,
                                      "linkedin":1,"email_status":1,"source":1,"_id":0}))
        if not docs:
            continue
        total = len(docs)
        has_wa    = sum(1 for d in docs if d.get("whatsapp") or d.get("phone"))
        has_email = sum(1 for d in docs if d.get("email"))
        VERIFIED_SIGNALS = ["✅","Hunter","FDA","WHOIS","官网直接","Apollo","跨境搜"]
        GUESSED_SIGNALS  = ["推算","待推算"]
        DEPT_PREFIXES    = ["info@","sales@","contact@","purchasing@","procurement@",
                            "supply@","orders@","import@"]
        verified  = sum(1 for d in docs if d.get("email") and
                        any(x in (d.get("email_status","") or "") for x in VERIFIED_SIGNALS))
        guessed   = sum(1 for d in docs if
                        any(x in (d.get("email_status","") or "") for x in GUESSED_SIGNALS))
        dept      = sum(1 for d in docs if d.get("email") and
                        any(d.get("email","").lower().startswith(p) for p in DEPT_PREFIXES))
        linkedin_only = sum(1 for d in docs if
                            d.get("linkedin") and not d.get("email") and not d.get("whatsapp"))
        no_contact = sum(1 for d in docs if
                         not d.get("email") and not d.get("whatsapp") and not d.get("phone"))
        # 来源分布
        sources = {}
        for d in docs:
            src = (d.get("source","") or "unknown").split("(")[0].strip()
            sources[src] = sources.get(src,0) + 1
        quality_n = verified + has_wa
        result[col] = {
            "total":            total,
            "has_whatsapp":     has_wa,
            "has_email":        has_email,
            "✅_verified":      verified,
            "📧_guessed":       guessed,
            "🏢_dept_generic":  dept,
            "🔗_linkedin_only": linkedin_only,
            "❌_no_contact":    no_contact,
            "quality_score":    f"{round(quality_n/total*100)}% ({quality_n}/{total})" if total else "0%",
            "sources":          dict(sorted(sources.items(), key=lambda x:-x[1])[:8]),
        }
    return jsonify(result)

@app.route("/api/user-plans")
@admin_required
def api_user_plans():
    """验证各用户套餐是否正确写入 MongoDB"""
    from auth import list_users
    result = []
    try:
        from db import get_db, is_available
        db_ok = is_available()
        users = list_users()
        for u in users:
            uname = u["username"]
            plan_in_db = None
            collection_used = None
            if db_ok:
                db = get_db()
                # 尝试几个可能的集合名
                for col in ["users", "accounts", "user_accounts"]:
                    doc = db[col].find_one({"username": uname}, {"plan":1,"_id":0})
                    if doc:
                        plan_in_db = doc.get("plan","未设置")
                        collection_used = col
                        break
            result.append({
                "username":         uname,
                "name":             u.get("name",""),
                "plan_in_db":       plan_in_db,
                "collection_found": collection_used,
                "all_collections":  get_db().list_collection_names() if db_ok else [],
            })
    except Exception as e:
        return jsonify({"error": str(e)})
    return jsonify(result)

@app.route("/api/smtp-test")
@login_required
def smtp_test_all():
    """全面测试 Railway 上哪些 SMTP 端口可用"""
    import smtplib, socket, requests as _req
    results = {}

    # Test 1: 端口连通性（不需要认证，只测 TCP 连接）
    test_hosts = [
        ("smtp.qiye.aliyun.com", 25),
        ("smtp.qiye.aliyun.com", 465),
        ("smtp.qiye.aliyun.com", 587),
        ("smtp.gmail.com", 465),
        ("smtp.gmail.com", 587),
        ("smtp.resend.com", 465),
        ("smtp.resend.com", 587),
        ("smtp.sendgrid.net", 587),
    ]
    tcp_results = {}
    for host, port in test_hosts:
        key = f"{host}:{port}"
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            tcp_results[key] = "✅ OPEN"
        except socket.timeout:
            tcp_results[key] = "❌ TIMEOUT"
        except ConnectionRefusedError:
            tcp_results[key] = "❌ REFUSED"
        except Exception as e:
            tcp_results[key] = f"❌ {type(e).__name__}"
    results["tcp_ports"] = tcp_results

    # Test 2: 尝试 HTTP API 发信（不走 SMTP 端口）
    try:
        r = _req.get("https://api.resend.com/emails", timeout=5,
                     headers={"Authorization": "Bearer test"})
        results["resend_api_reachable"] = f"✅ HTTP {r.status_code}"
    except Exception as e:
        results["resend_api_reachable"] = f"❌ {e}"

    try:
        r = _req.post("https://api.sendgrid.com/v3/mail/send", timeout=5,
                      headers={"Authorization": "Bearer test"}, json={})
        results["sendgrid_api_reachable"] = f"✅ HTTP {r.status_code}"
    except Exception as e:
        results["sendgrid_api_reachable"] = f"❌ {e}"

    # Test 3: 如果有 Anthropic key，测试出站 HTTPS
    results["outbound_https"] = "checking..."
    try:
        r = _req.get("https://httpbin.org/ip", timeout=5)
        results["outbound_https"] = f"✅ IP: {r.json().get('origin','?')}"
    except Exception as e:
        results["outbound_https"] = f"❌ {e}"

    return jsonify(results)

@app.route("/api/contacts-raw")
@login_required
def contacts_raw():
    """直接返回 ox_contacts 原始数据，验证读取是否正常"""
    username = session["username"]
    result = []
    try:
        from db import get_db, is_available
        if is_available():
            for c in get_db().ox_contacts.find({"username": username}, {"_id": 0, "email_body": 0}):
                result.append(c)
    except Exception as e:
        return jsonify({"error": str(e)})
    return jsonify({"count": len(result), "sample": result[:2]})

@app.route("/api/debug-crm")
@login_required
def debug_crm():
    """调试用：查看CRM原始数据"""
    username = session["username"]
    result = {"username": username, "collections": {}, "files": {}}
    try:
        from db import get_db, is_available
        if is_available():
            db = get_db()
            # 检查所有相关集合
            for col in ["ox_contacts", "crm_data", "contacts", "crm"]:
                try:
                    count = db[col].count_documents({"username": username})
                    sample = list(db[col].find({"username": username}, {"_id":0}).limit(1))
                    result["collections"][col] = {
                        "count": count,
                        "sample_keys": list(sample[0].keys()) if sample else []
                    }
                except Exception as e:
                    result["collections"][col] = {"error": str(e)}
            # 列出所有集合名
            result["all_collections"] = db.list_collection_names()
        else:
            result["mongodb"] = "not available"
    except Exception as e:
        result["mongodb_error"] = str(e)
    # 检查文件
    import os, json as _json
    for fname in os.listdir("."):
        if fname.endswith(".json") and ("crm" in fname.lower() or username in fname.lower()):
            try:
                with open(fname) as f:
                    data = _json.load(f)
                result["files"][fname] = {
                    "contacts_count": len(data.get("contacts",{})),
                    "keys": list(data.keys())
                }
            except Exception as e:
                result["files"][fname] = {"error": str(e)}
    # Test save directly
    try:
        from db import get_db, is_available
        if is_available():
            db = get_db()
            test_doc = {"username": username, "crm_key": "_debug_test", "test": True}
            db.ox_contacts.update_one(
                {"username": username, "crm_key": "_debug_test"},
                {"$set": test_doc}, upsert=True)
            verify = db.ox_contacts.find_one({"username": username, "crm_key": "_debug_test"})
            result["write_test"] = "OK" if verify else "FAILED"
            # cleanup
            db.ox_contacts.delete_one({"username": username, "crm_key": "_debug_test"})
    except Exception as e:
        result["write_test"] = f"ERROR: {e}"
    return jsonify(result)


# ════════════════════════════════════════════════════════════
# 转化漏斗 API
# ════════════════════════════════════════════════════════════

@app.route("/api/funnel")
@login_required
def api_funnel():
    """返回当前用户各漏斗阶段数量 + 回复率"""
    username = session["username"]
    try:
        from db import get_db, is_available
        if not is_available():
            return jsonify({"ok": False, "error": "DB不可用"})
        db = get_db()
        pipeline = [
            {"$match": {"username": username, "status": {"$ne": "dismissed"}}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]
        results = {r["_id"]: r["count"] for r in db.ox_contacts.aggregate(pipeline)}

        funnel = {
            "discovered": results.get("discovered", 0),
            "ready":      results.get("ready_to_send", 0),
            "sent":       results.get("sent", 0) + results.get("contacted", 0),
            "follow_up":  results.get("follow_up_1", 0) + results.get("follow_up_2", 0),
            "replied":    results.get("replied", 0),
            "qualified":  results.get("qualified", 0),
            "cold":       results.get("cold", 0),
        }
        total_outbound = funnel["sent"] + funnel["follow_up"]
        reply_rate = round(funnel["replied"] / total_outbound * 100, 1) if total_outbound else 0

        return jsonify({
            "ok": True,
            "funnel": funnel,
            "reply_rate": reply_rate,
            "total_outbound": total_outbound,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/contact/dismiss", methods=["POST"])
@login_required
def dismiss_contact():
    """软删除：将联系人标记为 dismissed（可通过 show_dismissed=1 查看）"""
    username = session["username"]
    data = request.get_json(force=True)
    keys = data.get("keys", [])
    if not keys:
        return jsonify({"ok": False, "error": "无选中项"}), 400
    try:
        from db import get_db, is_available
        if not is_available():
            return jsonify({"ok": False, "error": "DB不可用"})
        db = get_db()
        from datetime import datetime as _dt
        result = db.ox_contacts.update_many(
            {"username": username, "crm_key": {"$in": keys}},
            {"$set": {
                "status": "dismissed",
                "dismissed_at": _dt.now().strftime("%Y-%m-%d %H:%M"),
            }}
        )
        return jsonify({"ok": True, "dismissed": result.modified_count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/contact/delete", methods=["POST"])
@login_required
def delete_contact():
    """物理删除联系人（不可恢复）"""
    username = session["username"]
    data = request.get_json(force=True)
    keys = data.get("keys", [])
    if not keys:
        return jsonify({"ok": False, "error": "无选中项"}), 400
    try:
        from db import get_db, is_available
        if not is_available():
            return jsonify({"ok": False, "error": "DB不可用"})
        db = get_db()
        result = db.ox_contacts.delete_many(
            {"username": username, "crm_key": {"$in": keys}}
        )
        return jsonify({"ok": True, "deleted": result.deleted_count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/contact/keys-by-company", methods=["POST"])
@login_required
def contact_keys_by_company():
    """根据公司名列表返回对应的 crm_key 列表（供批量 dismiss/delete 使用）"""
    username = session["username"]
    data = request.get_json(force=True)
    companies = data.get("companies", [])
    if not companies:
        return jsonify({"ok": False, "keys": []})
    try:
        from db import get_db, is_available
        if not is_available():
            return jsonify({"ok": False, "keys": []})
        db = get_db()
        docs = list(db.ox_contacts.find(
            {"username": username, "company": {"$in": companies}},
            {"crm_key": 1, "_id": 0}
        ))
        keys = [d["crm_key"] for d in docs if d.get("crm_key")]
        return jsonify({"ok": True, "keys": keys})
    except Exception as e:
        return jsonify({"ok": False, "keys": [], "error": str(e)})



@app.route("/api/send-progress")
@login_required
def api_send_progress():
    """查询当前用户的发信进度"""
    username = session["username"]
    if not hasattr(app, '_send_progress') or username not in app._send_progress:
        return jsonify({"running": False})
    return jsonify(app._send_progress[username])


@app.route("/api/send-progress/stop", methods=["POST"])
@login_required
def api_send_progress_stop():
    """停止发信任务"""
    username = session["username"]
    if hasattr(app, '_send_progress') and username in app._send_progress:
        app._send_progress[username]["running"] = False
    return jsonify({"ok": True})


@app.route("/favicon.ico")
def favicon(): return "", 204

# ─── Entry ───────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        def _init_db():
            try:
                from db import init_indexes
                init_indexes()
            except Exception:
                pass
        threading.Thread(target=_init_db, daemon=True).start()
    except Exception:
        pass
    port = int(os.getenv("PORT", 8501))
    app.run(host="0.0.0.0", port=port, debug=False)
