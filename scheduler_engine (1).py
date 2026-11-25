# ============================================================
# Ox · scheduler_engine.py
# 持续后台运行引擎
# 流程：发现 → 筛选 → 背调 → 找联系人 → 发信 → 等待 → 重复
# ============================================================

import os
import re
import time
import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# 状态管理
# ──────────────────────────────────────────────────────────────

class OxState:
    """全局运行状态，线程安全"""

    def __init__(self):
        self._lock    = threading.Lock()
        self.running  = False
        self.phase    = "idle"         # idle / discovering / analyzing / sending / waiting
        self.progress = 0              # 0-100
        self.message  = ""             # 当前操作描述
        self.stats    = {
            "companies_found":    0,
            "contacts_verified":  0,
            "whatsapp_found":     0,
            "emails_sent":        0,
            "replies_detected":   0,
            "batches_completed":  0,
            "last_run":           None,
        }
        self.recent_activity = []      # 最近 20 条活动记录
        self.current_batch   = []      # 当前批次公司列表

    def set(self, phase=None, progress=None, message=None):
        with self._lock:
            if phase    is not None: self.phase    = phase
            if progress is not None: self.progress = progress
            if message  is not None: self.message  = message

    def add_activity(self, text: str, level: str = "info"):
        """level: info | success | warning | error"""
        with self._lock:
            self.recent_activity.insert(0, {
                "text":  text,
                "level": level,
                "time":  datetime.now().strftime("%H:%M"),
            })
            self.recent_activity = self.recent_activity[:20]

    def inc(self, key: str, n: int = 1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running":         self.running,
                "phase":           self.phase,
                "progress":        self.progress,
                "message":         self.message,
                "stats":           dict(self.stats),
                "recent_activity": list(self.recent_activity),
                "current_batch":   list(self.current_batch),
            }


# 按用户独立状态（不同用户互不影响）
_states: dict = {}
_states_lock = threading.Lock()


def _save_stats(username: str):
    """将 stats 持久化到 MongoDB"""
    try:
        from db import get_db, is_available
        if not is_available():
            return
        state = get_state(username)
        with state._lock:
            doc = {
                "username": username,
                "stats": dict(state.stats),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        get_db().ox_engine_state.update_one(
            {"username": username},
            {"$set": doc},
            upsert=True
        )
    except Exception as e:
        logger.debug(f"stats持久化失败: {e}")


def _load_stats_into(state: OxState, username: str):
    """从 MongoDB 恢复 stats 到指定 state 对象（不调用 get_state，避免递归）"""
    try:
        from db import get_db, is_available
        if not is_available():
            return
        doc = get_db().ox_engine_state.find_one({"username": username}, {"_id": 0})
        if doc and doc.get("stats"):
            with state._lock:
                saved = doc["stats"]
                for k, v in saved.items():
                    if k in state.stats and isinstance(v, (int, float)):
                        state.stats[k] = v
                state.stats["last_run"] = saved.get("last_run")
            logger.info(f"stats已恢复 [{username}]: {state.stats}")
    except Exception as e:
        logger.debug(f"stats恢复失败: {e}")


def get_state(username: str = "default") -> OxState:
    with _states_lock:
        if username not in _states:
            _states[username] = OxState()
            _load_stats_into(_states[username], username)
        return _states[username]


# ──────────────────────────────────────────────────────────────
# 主循环
# ──────────────────────────────────────────────────────────────

def _run_loop(config: dict, username: str):
    """
    持续运行的主循环（在子线程中执行）。
    config 格式：
    {
        "product":      "医用纱布、绷带、伤口护理耗材",
        "countries":    ["UK", "Germany", "France"],
        "n_targets":    5,
        "dry_run":      False,
        "sender_email": "you@company.com",
        "smtp_pass":    "xxxxxxxx",
        "batch_interval_minutes": 120,   # 每批次间隔（分钟）
    }
    """
    state = get_state(username)
    state.running = True
    state.add_activity("Ox 已启动，开始自动开发客户", "success")

    product        = config.get("product", "medical disposables")
    countries      = config.get("countries", ["UK", "Germany"])
    n_targets      = config.get("n_targets", 5)
    dry_run        = config.get("dry_run", False)
    sender         = config.get("sender_email", "")
    smtp_pass      = config.get("smtp_pass", "")
    smtp_provider  = config.get("smtp_provider", "")
    interval       = config.get("batch_interval_minutes", 120)

    batch_num = 0

    while state.running:
        batch_num += 1
        state.add_activity(f"开始第 {batch_num} 批次 · {', '.join(countries)}", "info")

        try:
            # ══ STEP 1: 发现目标公司 ══════════════════════════
            state.set(phase="discovering", progress=5,
                      message=f"发现目标客户 · {', '.join(countries)}")

            companies = _discover(product, countries, n_targets, state, username=username)
            if not companies:
                state.add_activity("未发现新公司，等待后重试", "warning")
                _wait(state, minutes=30)
                continue

            state.inc("companies_found", len(companies))
            state.current_batch = [c.get("name","") for c in companies]
            state.set(progress=20)

            # ══ 新增: 将发现的公司全部写入DB（状态 discovered）══
            try:
                from db import get_db, is_available
                if is_available():
                    db = get_db()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                    for comp in companies:
                        cname = comp.get("name", "")
                        if not cname:
                            continue
                        if db.ox_contacts.count_documents({
                            "username": username,
                            "company": {"$regex": f"^{re.escape(cname)}$", "$options": "i"}
                        }) == 0:
                            disc_key = f"{cname.lower().replace(' ','-')}_{now_str[:10]}"
                            db.ox_contacts.insert_one({
                                "username":     username,
                                "crm_key":      disc_key,
                                "company":      cname,
                                "country":      comp.get("country", ""),
                                "website":      comp.get("website", ""),
                                "status":       "discovered",
                                "found_at":     now_str,
                                "source":       "ox_discover",
                                "contact_name": "",
                                "email":        "",
                                "whatsapp":     "",
                            })
                    logger.info(f"批量写入 discovered: {len(companies)} 家")
            except Exception as e:
                logger.warning(f"批量写入discovered失败: {e}")

            # ══ STEP 2: 逐家分析 + 找联系人 + 发信 ══════════
            total = len(companies)
            for i, company in enumerate(companies, 1):
                if not state.running:
                    break

                name    = company.get("name", "")
                country = company.get("country", "")
                website = company.get("website", "")

                # ── 去重：跳过已开发过的公司 ──────────────────
                try:
                    from db import get_db, is_available
                    if is_available():
                        db = get_db()
                        existing = db.ox_contacts.count_documents({
                            "username": username,
                            "company": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
                            "status": {"$nin": ["dismissed"]}
                        })
                        if existing > 0:
                            state.add_activity(f"跳过（已开发）: {name} ({existing}条记录)", "info")
                            logger.info(f"去重跳过: {name}，已有 {existing} 条联系人")
                            continue
                except Exception as _de:
                    logger.debug(f"去重检查失败: {_de}")
                # ── 去重结束 ──────────────────────────────────

                pct = 20 + int((i / total) * 70)
                state.set(phase="analyzing", progress=pct,
                          message=f"[{i}/{total}] 分析: {name}")
                state.add_activity(f"分析: {name} ({country})", "info")

                # ── 背调 ──────────────────────────────────────
                try:
                    analysis, entry_point, email_subject, email_body = _analyze(
                        name, country, product, company, username=username
                    )
                except Exception as e:
                    state.add_activity(f"{name}: 分析失败 — {str(e)[:60]}", "error")
                    logger.warning(f"分析异常 {name}: {e}")
                    continue

                # ── 找联系人 ──────────────────────────────────
                state.set(message=f"[{i}/{total}] 找联系人: {name}")
                try:
                    from find_contacts import find_contacts
                    contacts = find_contacts(name, country, website) or []
                except Exception as e:
                    contacts = []
                    logger.warning(f"联系人获取失败 {name}: {e}")
                    state.add_activity(f"{name}: 联系人获取失败 — {str(e)[:60]}", "warning")

                if not contacts:
                    state.add_activity(f"{name}: 未找到联系人，跳过", "warning")
                    continue

                wa_count = sum(1 for c in contacts if c.get("whatsapp"))
                state.inc("contacts_verified", len(contacts))
                state.inc("whatsapp_found", wa_count)

                contact_summary = []
                if wa_count:
                    contact_summary.append(f"WhatsApp ×{wa_count}")
                email_count = sum(1 for c in contacts if c.get("email"))
                if email_count:
                    contact_summary.append(f"邮箱 ×{email_count}")
                state.add_activity(
                    f"{name}: 找到联系人 {len(contacts)} 个 ({', '.join(contact_summary)})",
                    "success"
                )

                # ── 保存到数据库（含邮件草稿，等待人工发送）──────
                try:
                    _save_result(
                        company=company,
                        analysis=analysis,
                        entry_point=entry_point,
                        contacts=contacts,
                        email_subject=email_subject,
                        email_body=email_body,
                        username=username,
                    )
                except Exception as e:
                    logger.warning(f"保存失败 {name}: {e}")

                _save_stats(username)

                # ── 自动发信 ──────────────────────────────────
                state.set(message=f"[{i}/{total}] 发信: {name}")
                best = next((c for c in contacts if c.get("email")), None)
                if best and sender and smtp_pass and not dry_run:
                    try:
                        from email_sender import send_single_email
                        ok, msg = send_single_email(
                            to_email=best.get("email",""),
                            to_name=best.get("name",""),
                            subject=email_subject,
                            body=email_body,
                            sender_email=sender,
                            username=username,
                            user_smtp_pass=smtp_pass,
                            smtp_provider=smtp_provider,
                        )
                        if ok:
                            state.add_activity(f"{name}: ✉ 已发送 → {best['email']}", "success")
                            state.inc("emails_sent")
                            # 更新该公司所有联系人状态为 contacted
                            try:
                                from db import get_db, is_available
                                if is_available():
                                    get_db().ox_contacts.update_many(
                                        {"username": username, "company": {"$regex": f"^{re.escape(name)}$", "$options": "i"}},
                                        {"$set": {"status": "contacted", "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M")}}
                                    )
                            except Exception:
                                pass
                        else:
                            state.add_activity(f"{name}: 发信失败 — {msg[:60]}", "error")
                    except Exception as e:
                        state.add_activity(f"{name}: 发信异常 — {str(e)[:60]}", "error")
                elif dry_run:
                    state.add_activity(f"{name}: 预演模式 → {best['email'] if best else '无邮箱'}", "info")
                else:
                    wa = next((c for c in contacts if c.get("whatsapp")), None)
                    if wa:
                        state.add_activity(f"{name}: 📱 {wa['whatsapp']} — 前往 Contacts 手动联系", "success")
                    elif best:
                        state.add_activity(f"{name}: ✉ 草稿已生成 → {best['email']} — 前往 Contacts 发送", "info")

                # 公司间隔（给 API 冷却时间）
                if i < total and state.running:
                    time.sleep(15)

            # ══ STEP 3: 检测回复 ═════════════════════════════
            if state.running:
                state.set(phase="waiting", progress=92, message="检测客户回复...")
                _check_replies(username, state, config)

            # ══ STEP 4: 自动跟进未回复联系人 ══════════════════
            if state.running:
                state.set(phase="waiting", progress=96, message="检查跟进队列...")
                _check_followups(username, state, config)

            # 批次完成
            state.inc("batches_completed")
            state.stats["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            _save_stats(username)
            state.set(phase="waiting", progress=100,
                      message=f"第 {batch_num} 批完成，{interval} 分钟后开始下一批")
            state.add_activity(f"第 {batch_num} 批完成", "success")

            if state.running:
                _wait(state, minutes=interval)

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"主循环异常: {tb}")
            state.add_activity(f"异常: {str(e)[:80]}", "error")
            if state.running:
                _wait(state, minutes=10)

    state = get_state(username)
    state.running = False
    state.set(phase="idle", progress=0, message="已停止")
    state.add_activity("Ox 已停止", "info")


# ──────────────────────────────────────────────────────────────
# 步骤实现
# ──────────────────────────────────────────────────────────────


def _generate_search_queries(product: str, country: str, username: str = "",
                             batch_num: int = 1) -> list:
    """
    让 Claude 生成多样化搜索查询。
    输入：产品线、目标国家、已开发过的公司名单（避免重复角度）
    输出：3-5 个不同角度的搜索关键词
    """
    import os, json
    api_key  = os.getenv("ANTHROPIC_API_KEY", "")
    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model    = os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6")
    if not api_key:
        return [f"{product} distributor {country}"]

    # 读已开发过的公司名（避免重复搜索角度）
    existing_names = []
    try:
        from db import get_db, is_available
        if is_available() and username:
            db = get_db()
            existing_names = db.ox_contacts.distinct("company", {
                "username": username,
                "country": {"$regex": country, "$options": "i"},
            })
    except Exception:
        pass

    prompt = f"""You are a B2B lead generation search strategist. Generate 5 diverse DuckDuckGo search queries to find NEW potential buyers/distributors/importers of: {product}

Target market: {country}
Batch number: {batch_num} (vary your approach from previous batches)

{"Already contacted companies in this country (DO NOT search for these again): " + ", ".join(existing_names[:30]) if existing_names else "No companies contacted yet in this country."}

Requirements:
- Each query should use a DIFFERENT search angle:
  1. Industry-specific trade directories or associations
  2. Import/export records or customs data
  3. Regional distributors by city/province (pick specific cities in {country})
  4. Hospital/pharmacy supply chains or procurement portals
  5. Trade show exhibitor lists or industry event participants
  6. Government tender/procurement databases
  7. Competitor analysis (who buys from similar suppliers)
  8. LinkedIn company search terms
  9. B2B marketplace listings
  10. News about market entrants or expansions

- Queries should be in English, 4-8 words each
- Include the country name or a major city in {country}
- Be SPECIFIC — "wound care distributor Hamburg Germany" not "medical supplies Germany"
- Batch {batch_num}: {'start broad' if batch_num <= 2 else 'try niche/creative angles — the obvious ones are exhausted'}

Return ONLY a JSON array of 5 query strings, nothing else.
Example: ["wound care distributor Hamburg Germany", "medical supplies importer Berlin tender 2024", ...]"""

    import httpx
    try:
        url = base_url.rstrip("/").rstrip("/v1") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }
        with httpx.Client(timeout=20) as hc:
            resp = hc.post(url, headers=headers, json=payload)
        text = resp.json().get("content", [{}])[0].get("text", "")
        if not text:
            return [f"{product} distributor {country}"]

        # 解析 JSON 数组
        import re
        # 提取 [...] 部分
        m = re.search(r'\[.*?\]', text, re.DOTALL)
        if m:
            queries = json.loads(m.group())
            if isinstance(queries, list) and len(queries) >= 2:
                logger.info(f"AI 生成 {len(queries)} 个搜索查询: {country}")
                return queries[:5]

        return [f"{product} distributor {country}"]
    except Exception as e:
        logger.debug(f"搜索策略生成失败: {e}")
        return [f"{product} distributor {country}"]


def _discover(product: str, countries: list, n: int, state: OxState, username: str = "") -> list:
    """发现目标公司（复用现有 discover_customers 模块）"""
    try:
        import importlib
        import discover_customers as dc
        importlib.reload(dc)
        dc.TARGET_PRODUCT = product
        dc.TOP_N = n

        all_companies = []

        # ── 候选池复用：优先从上批落选公司中拉取 ──
        try:
            from customer_history import pull_backlog, promote_backlog
            _bl = pull_backlog(countries, n=n*2, username=username)
            if _bl:
                all_companies.extend(_bl)
                state.add_activity(f"候选池复用 {len(_bl)} 家", "info")
                promote_backlog([c.get("name","") for c in _bl], username=username)
        except Exception as _e:
            logger.warning(f"候选池读取跳过: {_e}")


        us_variants = {"美国","USA","US","United States"}
        include_us  = bool(us_variants & set(countries))
        non_us      = [c for c in countries if c not in us_variants]

        if include_us:
            state.add_activity("扫描 ImportYeti（美国）", "info")
            for code, name in [("5803","gauze"),("3005","bandages")]:
                try:
                    r = dc.search_importyeti_by_hs(code, name)
                    all_companies.extend(r)
                    logger.info(f"ImportYeti HS{code}: {len(r)} 家")
                except Exception as e:
                    logger.warning(f"ImportYeti 失败: {e}")
                time.sleep(1.2)

        for cty in non_us:
            found = []

            # 优先用 Apollo.io（不受 DDG 封锁影响）
            try:
                from apollo_client import search_companies as apollo_search, is_configured as apollo_ok
                if apollo_ok():
                    found = apollo_search(cty, product, max_results=10)
                    logger.info(f"Apollo {cty}: {len(found)} 家")
                    state.add_activity(f"Apollo 搜索 {cty}: 找到 {len(found)} 家", "info")
            except Exception as e:
                logger.warning(f"Apollo 搜索 {cty} 失败: {e}")

            # ── AI 驱动多角度搜索 ──
            if not found:
                try:
                    # 让 Claude 生成多样化查询
                    _batch = state.stats.get("batches_completed", 0) + 1
                    queries = _generate_search_queries(product, cty, username=username, batch_num=_batch)
                    state.add_activity(f"AI 生成 {len(queries)} 个搜索角度: {cty}", "info")

                    for qi, query in enumerate(queries):
                        if not state.running:
                            break
                        try:
                            # 用 DDG 执行每个查询
                            _qfound = dc.search_companies_by_country(cty, max_results=6, query_override=query)
                            if _qfound:
                                found.extend(_qfound)
                                logger.info(f"  查询{qi+1} [{query[:40]}]: {len(_qfound)} 家")
                        except Exception as _qe:
                            logger.debug(f"  查询{qi+1} 失败: {_qe}")
                        time.sleep(0.8)
                except Exception as e:
                    logger.warning(f"AI搜索策略失败，降级单查询: {e}")

                # 降级：传统单查询（保底）
                if not found:
                    try:
                        found = dc.search_companies_by_country(cty, max_results=10)
                        logger.info(f"DDG 降级搜索 {cty}: {len(found)} 家")
                    except Exception as e:
                        logger.warning(f"DDG 搜索 {cty} 失败: {e}")
                        state.add_activity(f"搜索 {cty} 失败: {str(e)[:60]}", "warning")
                else:
                    state.add_activity(f"多角度搜索 {cty}: 找到 {len(found)} 家", "info")

            all_companies.extend(found)
            time.sleep(1.0)

        logger.info(f"搜索完成，原始候选: {len(all_companies)} 家")
        state.add_activity(f"搜索完成，原始候选 {len(all_companies)} 家", "info")

        unique = dc.deduplicate(all_companies)
        logger.info(f"去重后: {len(unique)} 家")

        if not unique:
            state.add_activity("DDG搜索无结果，可能被限速——将在下一批次重试", "warning")
            return []

        # 过滤已分析过的公司
        try:
            from customer_history import filter_new_companies
            before = len(unique)
            result = filter_new_companies(unique, username=username)
            # filter_new_companies 返回 (new_list, dup_list) 元组
            if isinstance(result, tuple) and len(result) == 2:
                new_cos, dup_cos = result
                if new_cos is not None:
                    unique = [c for c in new_cos if isinstance(c, dict) and c.get("name")]
            elif isinstance(result, list):
                unique = [c for c in result if isinstance(c, dict) and c.get("name")]
            logger.info(f"历史过滤: {before} → {len(unique)} 家（{before - len(unique)} 重复）")
            if before > 0 and len(unique) == 0:
                state.add_activity(f"本批次 {before} 家全部已开发过", "warning")
        except Exception as e:
            logger.warning(f"历史过滤跳过: {e}")

        # 最终确保全是 dict
        unique = [c for c in unique if isinstance(c, dict) and c.get("name")]

        if not unique:
            state.add_activity(f"历史过滤后无新公司（已开发 {before} 家），下批次将变换搜索角度", "warning")
            return []

        state.add_activity(f"AI 筛选 Top {n} 目标公司（共 {len(unique)} 个候选）...", "info")
        try:
            top = dc.ai_score_and_select(unique, product, n)
            logger.info(f"AI 筛选结果: {len(top) if top else 0} 家")
        except Exception as e:
            logger.error(f"AI 筛选失败: {e}")
            state.add_activity(f"AI 筛选失败: {str(e)[:80]}", "error")
            return []
        # ── 落选公司写入候选池 ──
        try:
            from customer_history import save_backlog
            _top_set = {re.sub(r'[^\w]','',c.get('name','').lower()) for c in (top or [])}
            _backlog = [c for c in unique
                        if re.sub(r'[^\w]','',c.get('name','').lower()) not in _top_set]
            if _backlog:
                save_backlog(_backlog, username=username)
                logger.info(f"候选池写入 {len(_backlog)} 家")
        except Exception as _e:
            logger.warning(f"候选池写入失败: {_e}")

        return top or []

    except Exception as e:
        logger.error(f"发现公司失败: {e}", exc_info=True)
        state.add_activity(f"发现步骤异常: {str(e)[:80]}", "error")
        return []


def _analyze(company_name: str, country: str, product: str,
             company_raw: dict, username: str = "") -> tuple:
    """背调 + AI 分析，返回 (analysis, entry_point, subject, body)"""
    import kingphar_intel as ki

    website = company_raw.get("website", "")
    reason  = company_raw.get("reason", "")

    # 从用户配置读取公司档案（通用化）
    company_profile = {}
    try:
        from db import get_db, is_available
        if is_available() and username:
            cfg = get_db().user_configs.find_one({"username": username}, {"_id":0})
            if cfg:
                company_profile = {
                    "company_name":       cfg.get("company_name",""),
                    "product":            cfg.get("product", product),
                    "advantages":         cfg.get("company_advantages",""),
                    "target_customer":    cfg.get("company_target_customer",""),
                    "industry":           cfg.get("company_industry",""),
                }
    except Exception:
        pass

    # 降级到 company_config.py（Kingphar 默认）
    if not company_profile.get("company_name"):
        try:
            from company_config import COMPANY_PROFILE
            company_profile = COMPANY_PROFILE
        except Exception:
            company_profile = {"company_name": "our company", "advantages": "", "product": product}

    raw, dq = ki.collect_with_fallback(company_name, country)
    if not raw:
        raw = f"公司：{company_name}\n国家：{country}"
        dq  = "minimal"
    if reason:
        raw += f"\n\n## 初步评估\n{reason}"

    comp_intel = {}
    try:
        from competitor_intel import get_competitor_intel
        comp_intel = get_competitor_intel(company_name, country) or {}
    except Exception:
        pass

    analysis = ki.analyze_customer(
        company_name, country, product, raw,
        company_profile, dq, competitor_intel=comp_intel
    )

    # 提取切入点和邮件
    entry_point = _extract_section(analysis, ["切入","approach","策略"])
    from email_sender import extract_email_from_analysis
    subject, body = extract_email_from_analysis(analysis, company_name)

    # AI 精炼邮件：简洁 + 突出亮点 + 真实落款
    try:
        subject, body = _refine_email(company_name, country, entry_point, subject, body, username=username)
    except Exception as e:
        logger.warning(f"邮件精炼跳过: {e}")

    return analysis, entry_point, subject, body



def _personalize_email(base_subject: str, base_body: str,
                       contact_name: str, contact_title: str,
                       company_name: str, country: str,
                       username: str = "") -> tuple:
    """
    千人千面：基于公司级邮件草稿，为每个联系人做轻量级个性化改写。
    - 有姓名: 加称呼 + 根据职位微调切入角度
    - 无姓名或无职位: 直接返回原邮件
    耗时约 2-3 秒 / 联系人，token 消耗约 300-400
    """
    import os, re
    # 无姓名或无职位 → 不改写
    if not contact_name or not contact_title:
        return base_subject, base_body

    api_key  = os.getenv("ANTHROPIC_API_KEY", "")
    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model    = os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6")
    if not api_key:
        return base_subject, base_body

    prompt = f"""Adapt this B2B outreach email for a specific recipient. Keep it under 150 words, same 3-paragraph structure.

Recipient: {contact_name}, {contact_title} at {company_name} ({country})

Original email:
{base_body[:1000]}

Rules:
- Address them by first name (extract from "{contact_name}")
- Adjust paragraph 1 hook to resonate with their role ({contact_title}):
  * C-level/Director → focus on strategic partnership value
  * Procurement/Purchasing → focus on cost savings, supply reliability
  * Technical/R&D → focus on product specs, certifications, customization
  * Sales/Marketing → focus on how our products help them serve their customers
- Keep paragraphs 2 and 3 essentially the same (minor wording tweaks OK)
- Keep the exact same sign-off block
- Do NOT change the core message or add new claims

Also adjust the subject line to be more relevant to their role.

Return ONLY:
SUBJECT: <adjusted subject>
---
<adjusted email body>"""

    import httpx
    try:
        url = base_url.rstrip("/").rstrip("/v1") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}],
        }
        with httpx.Client(timeout=25) as hc:
            resp = hc.post(url, headers=headers, json=payload)
        text = resp.json().get("content", [{}])[0].get("text", "")
        if not text:
            return base_subject, base_body

        subj_m = re.search(r'SUBJECT:\s*(.+)', text)
        new_subject = subj_m.group(1).strip() if subj_m else base_subject

        body_m = re.search(r'---\s*\n(.*)', text, re.DOTALL)
        new_body = body_m.group(1).strip() if body_m else base_body

        if len(new_body) > 80:
            logger.info(f"    ✉ 个性化: {contact_name} ({contact_title}) → {len(new_body)} chars")
            return new_subject, new_body
        return base_subject, base_body
    except Exception as e:
        logger.debug(f"个性化改写跳过 {contact_name}: {e}")
        return base_subject, base_body


def _refine_email(company_name: str, country: str, entry_point: str,
                  subject: str, body: str, username: str = "") -> tuple:
    """
    用 AI 把生成的开发信精炼为：
    - 3 段，不超过 150 词
    - 第 1 段：个性化切入（entry_point）
    - 第 2 段：核心优势
    - 第 3 段：CTA
    - 落款自动使用用户真实姓名和职衔
    """
    import os, re
    api_key  = os.getenv("ANTHROPIC_API_KEY","")
    base_url = os.getenv("ANTHROPIC_BASE_URL","https://api.anthropic.com")
    model    = os.getenv("PRIMARY_MODEL","claude-sonnet-4-6")
    if not api_key:
        return subject, body

    # 读取用户公司档案（通用化核心）
    sender_name  = ""
    sender_title = ""
    company_name_sender  = ""
    company_advantages   = ""
    company_cta          = ""

    try:
        from db import get_db, is_available
        if is_available() and username:
            cfg = get_db().user_configs.find_one({"username": username}, {"_id":0})
            if cfg:
                sender_name          = cfg.get("sender_name","")
                sender_title         = cfg.get("sender_title","")
                company_name_sender  = cfg.get("company_name","")
                company_advantages   = cfg.get("company_advantages","")
                company_cta          = cfg.get("company_cta","")
    except Exception:
        pass

    # 降级默认值
    if not sender_name:
        sender_name = username.capitalize() if username else "Sales Team"
    if not company_name_sender:
        company_name_sender = "our company"
    if not sender_title:
        sender_title = f"International Sales | {company_name_sender}"
    if not company_advantages:
        company_advantages = "industry-leading quality, competitive pricing, reliable supply chain, OEM available"
    if not company_cta:
        company_cta = "a brief 15-minute call or product catalog"

    sign_off = f"{sender_name}\n{sender_title}"

    prompt = f"""You are a B2B cold email expert. Rewrite this outreach email to be sharp, specific, and under 150 words.

Sender company: {company_name_sender}
Target company: {company_name} ({country})
Approach angle: {entry_point or 'Supply chain partnership'}

Original email:
{body[:1200]}

Our company's key advantages to highlight (pick the 2 most relevant):
{company_advantages}

Preferred CTA: {company_cta}

Rules:
- Exactly 3 short paragraphs
- Para 1 (1 sentence): Hook using the specific approach angle — make it feel researched, not generic
- Para 2 (2 sentences): Highlight the 2 most relevant advantages from the list above
- Para 3 (1 sentence): CTA based on the preferred CTA above
- Sign off MUST be exactly:
  Best regards,
  {sign_off}
- No subject line in body, no bullet points, no "I hope this finds you well"

Also suggest a better subject line (max 8 words, specific to {company_name}).

Return ONLY this format:
SUBJECT: <subject line>
---
<3-paragraph email body + sign off>"""

    import httpx
    try:
        url = base_url.rstrip("/").rstrip("/v1") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
        }
        with httpx.Client(timeout=30) as hc:
            resp = hc.post(url, headers=headers, json=payload)
        text = resp.json().get("content",[{}])[0].get("text","")
        if not text:
            return subject, body

        # Parse subject
        subj_m = re.search(r'SUBJECT:\s*(.+)', text)
        new_subject = subj_m.group(1).strip() if subj_m else subject

        # Parse body (after ---)
        body_m = re.search(r'---\s*\n(.*)', text, re.DOTALL)
        new_body = body_m.group(1).strip() if body_m else body

        if len(new_body) > 80:
            logger.info(f"  ✉ 邮件精炼完成 ({len(new_body)} chars)")
            return new_subject, new_body
        return subject, body
    except Exception as e:
        logger.warning(f"邮件精炼HTTP失败: {e}")
        return subject, body


def _extract_section(text: str, keywords: list) -> str:
    import re
    if not text:
        return ""
    kw_pat = "|".join(keywords)
    pat = rf'(?:^|\n)(?:#+\s*|\*{{1,2}})?\d+[\.、]\s*[^\n]*?(?:{kw_pat})[^\n]*\n'
    m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    next_m = re.search(r'\n(?:#+\s*|\*{0,2})\d+[.、]\s', text[start:], re.MULTILINE)
    end = start + next_m.start() if next_m else start + 400
    return text[start:end].strip()[:400]


def _save_result(company: dict, analysis: str, entry_point: str,
                 contacts: list, username: str,
                 email_subject: str = "", email_body: str = ""):
    """保存分析结果到历史记录，并把每个联系人写入 CRM（含邮件草稿）"""
    name    = company.get("name","")
    country = company.get("country","")

    # 写入历史记录
    result = {
        "company":      name,
        "country":      country,
        "status":       "success",
        "data_quality": "full",
        "analysis":     analysis,
        "contacts":     contacts,
        "entry_point":  entry_point,
    }
    try:
        from customer_history import mark_batch
        mark_batch([result], username=username)
    except Exception as e:
        logger.warning(f"保存历史失败: {e}")

    # 直接写入 MongoDB（不依赖 follow_up 文件系统）
    try:
        from db import get_db, is_available
        if not is_available():
            raise RuntimeError("MongoDB 不可用")
        db = get_db()

        # 删除此公司的 discovered 占位记录（即将被真正的联系人记录替换）
        db.ox_contacts.delete_many({
            "username": username,
            "company": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
            "status": "discovered",
        })

        saved = 0
        for c in contacts:
            email  = c.get("email","")
            wa     = c.get("whatsapp","") or c.get("phone","")
            c_name = c.get("name","")
            email_status = c.get("email_status","")
            source       = c.get("source","")

            # 质量过滤：跳过纯部门兜底邮箱（无真人姓名+推算来源）
            is_dept_only = (
                not c_name and email and
                any(email.lower().startswith(p+"@")
                    for p in ["info","sales","contact","orders","purchasing",
                              "procurement","supply","import"]) and
                "部门邮箱推算" in source
            )
            if is_dept_only:
                logger.debug(f"  跳过部门兜底: {email}")
                continue

            key    = f"{name}_{email or wa or c_name}".replace(" ","_").replace("@","_at_")
            # ── 千人千面：针对每个联系人微调邮件 ──
            _p_subj, _p_body = email_subject, email_body
            if c_name and (c.get("position","") or c.get("title","")):
                try:
                    _p_subj, _p_body = _personalize_email(
                        email_subject, email_body,
                        c_name, c.get("position","") or c.get("title",""),
                        name, country, username=username,
                    )
                except Exception as _pe:
                    logger.debug(f"个性化跳过 {c_name}: {_pe}")
            doc    = {
                "username":      username,
                "crm_key":       key,
                "company":       name,
                "country":       country,
                "contact_name":  c_name,
                "title":         c.get("position","") or c.get("title",""),
                "email":         email,
                "email_status":  email_status,
                "source":        source,
                "whatsapp":      wa,
                "phone":         c.get("phone",""),
                "linkedin":      c.get("linkedin",""),
                "status":        "ready_to_send",
                "email_subject": _p_subj,
                "email_body":    _p_body,
                "entry_point":   entry_point,
                "analysis_full": analysis,
                "analysis_snippet": analysis[:800] if analysis else "",
                "found_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
                "rounds":        [],
                "notes":         "",
            }
            db.ox_contacts.update_one(
                {"username": username, "crm_key": key},
                {"$setOnInsert": doc},
                upsert=True
            )
            saved += 1
        logger.info(f"MongoDB ox_contacts 保存 {saved} 个联系人: {name}")
        # 计量：记录使用量（核心计费：已验证联系人数）
        try:
            from usage_tracker import record_usage
            # 分析公司数（额度参考）
            record_usage(username, "company_analyzed", 1,
                         meta={"company": name, "country": country})
            # 联系人总数
            if contacts:
                record_usage(username, "contact_found", len(contacts))
            # ★ 已验证联系人（有邮箱或WhatsApp = 真正有价值，这是计费依据）
            verified = [c for c in contacts if c.get("email") or c.get("whatsapp")]
            if verified:
                record_usage(username, "contact_verified", len(verified),
                             meta={"company": name, "verified_count": len(verified)})
            # ★ 实时扣费
            from billing import deduct_contact, has_sufficient_balance
            state = get_state(username)
            for vc in verified:
                r = deduct_contact(username, contact_info=f"{vc.get('name','')} <{vc.get('email','')}>")
                if not r["ok"]:
                    logger.warning(f"[{username}] 余额不足，剩余 ¥{r['balance']:.1f}，暂停引擎")
                    state.message = "余额不足，请充值后继续"
                    state.running = False
                    break
            if not state.running:
                return
            # WhatsApp（单独统计，价值更高）
            wa_n = sum(1 for c in contacts if c.get("whatsapp"))
            if wa_n:
                record_usage(username, "whatsapp_found", wa_n)
        except Exception as _ue:
            logger.debug(f"usage_tracker 跳过: {_ue}")
        # 验证写入成功
        count = db.ox_contacts.count_documents({"username": username, "company": name})
        logger.info(f"验证: ox_contacts 中 {name} 共 {count} 条")
    except Exception as e:
        logger.warning(f"MongoDB 保存失败，降级到 follow_up: {e}")
        # 降级到 follow_up
        try:
            from follow_up import _load_crm, _save_crm
            crm = _load_crm(username)
            contacts_db = crm.setdefault("contacts", {})
            for c in contacts:
                email  = c.get("email","")
                wa     = c.get("whatsapp","") or c.get("phone","")
                c_name = c.get("name","")
                key    = f"{name}_{email or wa or c_name}".replace(" ","_").replace("@","_at_")
                if key not in contacts_db:
                    contacts_db[key] = {
                        "company": name, "country": country,
                        "contact_name": c_name,
                        "title": c.get("position","") or c.get("title",""),
                        "email": email, "whatsapp": wa, "phone": c.get("phone",""),
                        "linkedin": c.get("linkedin",""),
                        "status": "ready_to_send",
                        "email_subject": email_subject,
                        "email_body": email_body,
                        "entry_point": entry_point,
                        "found_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "rounds": [], "notes": "",
                    }
            _save_crm(crm, username)
        except Exception as e2:
            logger.warning(f"follow_up 降级也失败: {e2}")


def _send_outreach(contact: dict, subject: str, body: str,
                   sender: str, smtp_pass: str, dry_run: bool,
                   username: str, smtp_provider: str = ""):
    """发送开发信并写入 CRM"""
    from email_sender import send_single_email, add_to_crm_after_send

    ok, msg = send_single_email(
        to_email=contact.get("email",""),
        to_name=contact.get("name",""),
        subject=subject,
        body=body,
        sender_email=sender,
        username=username,
        user_smtp_pass=smtp_pass,
        smtp_provider=smtp_provider,
    )

    if dry_run or ok:
        try:
            add_to_crm_after_send(
                company=contact.get("company",""),
                country=contact.get("country",""),
                to_email=contact.get("email",""),
                to_name=contact.get("name",""),
                subject=subject,
                body=body,
                username=username,
            )
        except Exception:
            pass

    if not dry_run and not ok:
        raise RuntimeError(msg)


def _check_replies(username: str, state: OxState, config: dict):
    """检测客户回复"""
    try:
        from reply_detector import process_replies
        import follow_up as _fu

        from app import _load_imap_config
        imap_cfg = _load_imap_config(username)
        if not imap_cfg.get("host"):
            return

        crm = _fu._load_crm(username)
        contacts = crm.get("contacts", {})
        results  = process_replies(username, imap_cfg, contacts, days=3)

        for item in results:
            key    = item["crm_key"]
            intent = item["intent"]
            company_name = item["company"]
            _fu.update_status(key, intent.get("crm_status","replied"))
            state.add_activity(
                f"回复: {company_name} — {intent.get('label','')}",
                "success"
            )
            state.inc("replies_detected")

            # 同步更新 ox_contacts 中该公司的状态
            try:
                from db import get_db, is_available
                if is_available():
                    get_db().ox_contacts.update_many(
                        {"username": username, "company": {"$regex": f"^{re.escape(company_name)}$", "$options": "i"}},
                        {"$set": {"status": "replied", "replied_at": datetime.now().strftime("%Y-%m-%d %H:%M")}}
                    )
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"回复检测跳过: {e}")


def _check_followups(username: str, state: OxState, config: dict):
    """检查超时未回复的已发信联系人，自动生成跟进邮件并发送。
    时间线：contacted 3天 → follow_up_1，follow_up_1 7天 → follow_up_2，follow_up_2 7天 → cold
    """
    try:
        from db import get_db, is_available
        if not is_available():
            return
        db = get_db()

        sender        = config.get("sender_email", "")
        smtp_pass     = config.get("smtp_pass", "")
        smtp_provider = config.get("smtp_provider", "")
        dry_run       = config.get("dry_run", False)
        product       = config.get("product", "")

        if not sender or not smtp_pass:
            return

        now = datetime.now()
        transitions = [
            # (当前状态, 等待天数, 目标状态, 跟进轮次)
            ("contacted",   3, "follow_up_1", 1),
            ("follow_up_1", 7, "follow_up_2", 2),
        ]

        for from_status, wait_days, to_status, round_num in transitions:
            cutoff = (now - timedelta(days=wait_days)).strftime("%Y-%m-%d %H:%M")
            stale = list(db.ox_contacts.find({
                "username": username,
                "status": from_status,
                "sent_at": {"$exists": True, "$lt": cutoff},
                "email": {"$ne": ""},
            }).limit(10))  # 每批最多处理10个，防止阻塞

            for contact in stale:
                cname  = contact.get("company", "")
                cemail = contact.get("email", "")
                if not cemail:
                    continue

                # 生成跟进邮件
                try:
                    fu_subject, fu_body = _generate_followup_email(
                        company=cname,
                        contact_name=contact.get("contact_name", ""),
                        original_subject=contact.get("email_subject", ""),
                        original_body=contact.get("email_body", ""),
                        round_num=round_num,
                        product=product,
                        username=username,
                    )
                except Exception as e:
                    logger.warning(f"跟进邮件生成失败 {cname}: {e}")
                    continue

                # 发送
                if not dry_run:
                    try:
                        from email_sender import send_single_email
                        ok, msg = send_single_email(
                            to_email=cemail,
                            to_name=contact.get("contact_name", ""),
                            subject=fu_subject,
                            body=fu_body,
                            sender_email=sender,
                            username=username,
                            user_smtp_pass=smtp_pass,
                            smtp_provider=smtp_provider,
                        )
                        if not ok:
                            logger.warning(f"跟进发信失败 {cname}: {msg}")
                            continue
                    except Exception as e:
                        logger.warning(f"跟进发信异常 {cname}: {e}")
                        continue

                # 更新状态
                now_str = now.strftime("%Y-%m-%d %H:%M")
                db.ox_contacts.update_one(
                    {"_id": contact["_id"]},
                    {
                        "$set": {"status": to_status, "sent_at": now_str},
                        "$push": {"rounds": {
                            "round": round_num,
                            "sent_at": now_str,
                            "subject": fu_subject,
                            "body": fu_body,
                        }}
                    }
                )
                state.add_activity(
                    f"跟进#{round_num}: {cname} → {cemail}",
                    "info" if dry_run else "success"
                )
                state.inc("emails_sent")
                time.sleep(5)  # 间隔冷却

        # follow_up_2 超7天 → cold（不再发信）
        cold_cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        cold_result = db.ox_contacts.update_many(
            {
                "username": username,
                "status": "follow_up_2",
                "sent_at": {"$exists": True, "$lt": cold_cutoff},
            },
            {"$set": {"status": "cold"}}
        )
        if cold_result.modified_count:
            state.add_activity(f"{cold_result.modified_count} 个联系人标记为冷 (无回复)", "info")

    except Exception as e:
        logger.warning(f"跟进检查异常: {e}")


def _generate_followup_email(company: str, contact_name: str,
                              original_subject: str, original_body: str,
                              round_num: int, product: str,
                              username: str) -> tuple:
    """用 Claude 生成跟进邮件，返回 (subject, body)"""
    # 加载用户公司配置
    sender_name = ""
    sender_title = ""
    company_name_sender = ""
    try:
        from db import get_db, is_available
        if is_available():
            cfg = get_db().user_configs.find_one({"username": username}, {"_id": 0})
            if cfg:
                sender_name = cfg.get("sender_name", "")
                sender_title = cfg.get("sender_title", "")
                company_name_sender = cfg.get("company_name", "")
    except Exception:
        pass

    if not sender_name:
        sender_name = username.capitalize()
    if not company_name_sender:
        company_name_sender = "our company"

    prompt = f"""Generate a follow-up email (round #{round_num}) for a B2B outreach.

Context:
- Our company: {company_name_sender}
- Product: {product}
- Target company: {company}
- Contact name: {contact_name or 'the purchasing team'}
- Original subject: {original_subject}

{"This is the first follow-up (3 days after initial email). Be brief, reference the original email, add a small new value point." if round_num == 1 else "This is the second follow-up (10+ days since first email). Be concise, slightly more direct, offer a specific next step like a free sample or 15-min call."}

Return ONLY a JSON object:
{{"subject": "Re: ...", "body": "..."}}

Sign off as: {sender_name}, {sender_title}
Body must be plain text, 3-5 sentences max. No markdown."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        text = resp.content[0].text.strip()
        # 尝试提取JSON
        if "{" in text:
            text = text[text.index("{"):text.rindex("}")+1]
        data = json.loads(text)
        return data.get("subject", f"Re: {original_subject}"), data.get("body", "")
    except Exception as e:
        # 降级：简单模板
        logger.warning(f"Claude跟进邮件生成失败，使用模板: {e}")
        subj = f"Re: {original_subject}"
        body = (
            f"Hi {contact_name or 'there'},\n\n"
            f"I wanted to follow up on my previous email regarding {product}. "
            f"We'd love to explore how {company_name_sender} can support {company}.\n\n"
            f"Would you have 15 minutes for a quick call this week?\n\n"
            f"Best regards,\n{sender_name}\n{sender_title}"
        )
        return subj, body


def _wait(state: OxState, minutes: int):
    """等待指定分钟，每 30 秒检查一次是否被停止"""
    total_secs = minutes * 60
    waited = 0
    while waited < total_secs and state.running:
        remaining = (total_secs - waited) // 60
        state.set(phase="waiting",
                  message=f"等待下一批 · 还剩 {remaining} 分钟")
        time.sleep(30)
        waited += 30


# ──────────────────────────────────────────────────────────────
# 公开 API：启动 / 停止
# ──────────────────────────────────────────────────────────────

# 每个用户一个线程
_threads: dict = {}

def start(config: dict, username: str = "default") -> bool:
    """启动后台循环，已在运行则返回 False"""
    state = get_state(username)
    if state.running:
        return False

    state.running  = True
    state.phase    = "discovering"
    state.progress = 0
    state.recent_activity = []
    state.current_batch = []

    t = threading.Thread(
        target=_run_loop,
        args=(config, username),
        daemon=True,
        name=f"OxEngine-{username}",
    )
    _threads[username] = t
    t.start()
    logger.info(f"Ox Engine 已启动 [{username}]")
    return True


def stop(username: str = "default"):
    """停止后台循环（软停止）"""
    state = get_state(username)
    state.running = False
    state.add_activity("收到停止指令，完成当前任务后停止", "info")
    logger.info(f"Ox Engine 停止中 [{username}]...")


def is_running(username: str = "default") -> bool:
    return get_state(username).running
