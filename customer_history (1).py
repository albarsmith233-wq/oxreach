# ============================================================
# Kingphar — 客户历史记录（MongoDB版）
# ============================================================

import re
from datetime import datetime


def _normalize(name):
    n = name.lower().strip()
    for suffix in [" ltd"," llc"," inc"," gmbh"," co."," corp",
                   " s.a."," bv"," ag"," sas"," kg"," plc",
                   " limited"," incorporated",".",","]:
        n = n.replace(suffix, "")
    return re.sub(r"[^\w]", "", n)


def _get_db_or_file(username=""):
    """优先用MongoDB，降级用文件"""
    try:
        from db import get_db, is_available
        if is_available():
            return "mongo"
    except Exception:
        pass
    return "file"


def _file_path(username=""):
    if username:
        try:
            from user_data import get_history_file
            return get_history_file(username)
        except Exception:
            pass
    return "customer_history.json"


def _load_file(username=""):
    import json, os
    path = _file_path(username)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"companies": {}}


def _save_file(data, username=""):
    import json
    with open(_file_path(username), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def mark_analyzed(result, username=""):
    name = result.get("company", "")
    if not name:
        return
    key = _normalize(name)
    record = {
        "name":          name,
        "country":       result.get("country", ""),
        "status":        result.get("status", ""),
        "data_quality":  result.get("data_quality", ""),
        "analyzed_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "has_analysis":  bool(result.get("analysis")),
        "contact_count": len(result.get("contacts", [])),
        "summary":       (result.get("analysis") or "")[:200],
    }
    if _get_db_or_file(username) == "mongo":
        from db import history_upsert
        history_upsert(username, key, record)
    else:
        data = _load_file(username)
        data["companies"][key] = record
        _save_file(data, username)


def mark_batch(results, username=""):
    for r in results:
        mark_analyzed(r, username)


def filter_new_companies(companies, username=""):
    # 从 history 集合读已分析过的公司
    if _get_db_or_file(username) == "mongo":
        from db import history_load
        data = history_load(username)
    else:
        data = _load_file(username)
    existing = set(data.get("companies", {}).keys())

    # 也从 ox_contacts 读已入库的公司（含 discovered/ready_to_send/sent 等）
    try:
        from db import get_db, is_available
        if is_available() and username:
            db = get_db()
            in_crm = db.ox_contacts.distinct("company", {
                "username": username,
                "status": {"$nin": ["dismissed"]}
            })
            for name in in_crm:
                existing.add(_normalize(name))
    except Exception:
        pass

    new_cos, dup_cos = [], []
    for c in companies:
        key = _normalize(c.get("name", ""))
        if key in existing:
            dup_cos.append(c)
        else:
            new_cos.append(c)
    return new_cos, dup_cos


def get_history_stats(username=""):
    if _get_db_or_file(username) == "mongo":
        from db import history_load
        data = history_load(username)
    else:
        data = _load_file(username)
    companies = data.get("companies", {})
    total     = len(companies)
    success   = sum(1 for c in companies.values() if c.get("status") == "success")
    countries = list(set(c.get("country","") for c in companies.values() if c.get("country")))
    return {
        "total":        total,
        "success":      success,
        "failed":       total - success,
        "countries":    len(countries),
        "country_list": sorted(countries),
    }


def get_history_list(username=""):
    if _get_db_or_file(username) == "mongo":
        from db import history_load
        data = history_load(username)
    else:
        data = _load_file(username)
    records = list(data.get("companies", {}).values())
    records.sort(key=lambda x: x.get("analyzed_at", ""), reverse=True)
    return records


def clear_history(username=""):
    if _get_db_or_file(username) == "mongo":
        from db import history_save
        history_save(username, {"companies": {}})
    else:
        _save_file({"companies": {}}, username)


# ─────────────────────────────────────────────────────────────
# 候选池（Backlog）— 落选公司暂存，下批次复用
# ─────────────────────────────────────────────────────────────

def save_backlog(companies, username=""):
    """落选公司写入候选池，upsert去重"""
    if _get_db_or_file(username) != "mongo":
        return
    from db import get_db
    coll = get_db().ox_backlog
    for c in companies:
        key = _normalize(c.get("name", ""))
        if not key:
            continue
        coll.update_one(
            {"username": username or "default", "key": key},
            {"$setOnInsert": {
                "name":       c.get("name", ""),
                "country":    c.get("country", ""),
                "website":    c.get("website", ""),
                "reason":     c.get("reason", ""),
                "source":     c.get("source", ""),
                "discovered": datetime.now().strftime("%Y-%m-%d"),
                "status":     "backlog",
            }},
            upsert=True,
        )


def pull_backlog(countries, n=10, username=""):
    """从候选池拉取待处理公司，FIFO"""
    if _get_db_or_file(username) != "mongo":
        return []
    from db import get_db
    query = {"username": username or "default", "status": "backlog"}
    if countries:
        lc = [c.lower().strip() for c in countries]
        query["$or"] = [
            {"country": {"$in": countries}},
            {"country": {"$regex": "|".join(lc), "$options": "i"}},
        ]
    results = list(get_db().ox_backlog.find(query).sort("discovered", 1).limit(n))
    return [{"name": r["name"], "country": r.get("country", ""),
             "website": r.get("website", ""), "reason": r.get("reason", "")}
            for r in results]


def promote_backlog(names, username=""):
    """标记候选池公司为已提升"""
    if _get_db_or_file(username) != "mongo":
        return
    from db import get_db
    keys = [_normalize(n) for n in names if n]
    if keys:
        get_db().ox_backlog.update_many(
            {"username": username or "default", "key": {"$in": keys}},
            {"$set": {"status": "promoted"}},
        )
