"""
billing.py — Ox 余额与计费系统
- 余额查询/充值/扣费
- 交易流水记录
- 余额不足检查（引擎启动前 & 每次扣费前）
"""
from datetime import datetime, timezone
from db import get_db

COST_PER_CONTACT = 0.5  # ¥0.5 / 已验证联系人

# ============================================================
# 余额操作
# ============================================================

def get_balance(username: str) -> float:
    """查询用户余额，不存在则返回0"""
    db = get_db()
    doc = db.balances.find_one({"username": username})
    return doc["balance"] if doc else 0.0


def add_balance(username: str, amount: float, admin_user: str = "", note: str = "") -> dict:
    """管理员手动充值（加余额）"""
    if amount <= 0:
        return {"ok": False, "error": "金额必须大于0"}

    db = get_db()
    now = datetime.now(timezone.utc)

    # 原子更新余额
    result = db.balances.update_one(
        {"username": username},
        {"$inc": {"balance": amount}, "$set": {"updated_at": now}},
        upsert=True
    )

    new_balance = get_balance(username)

    # 记录流水
    db.transactions.insert_one({
        "username": username,
        "type": "topup",
        "amount": amount,
        "balance_after": new_balance,
        "note": note or f"管理员 {admin_user} 手动充值",
        "admin_user": admin_user,
        "created_at": now,
    })

    return {"ok": True, "balance": new_balance}


def deduct_contact(username: str, contact_info: str = "") -> dict:
    """
    发现已验证联系人时扣费 ¥0.5
    返回 {"ok": True/False, "balance": ..., "error": ...}
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    cost = COST_PER_CONTACT

    # 原子扣减：仅当余额>=cost时才扣
    result = db.balances.update_one(
        {"username": username, "balance": {"$gte": cost}},
        {"$inc": {"balance": -cost}, "$set": {"updated_at": now}}
    )

    if result.modified_count == 0:
        current = get_balance(username)
        return {"ok": False, "balance": current, "error": "余额不足"}

    new_balance = get_balance(username)

    # 记录流水
    db.transactions.insert_one({
        "username": username,
        "type": "deduct",
        "amount": -cost,
        "balance_after": new_balance,
        "contact_info": contact_info,
        "created_at": now,
    })

    return {"ok": True, "balance": new_balance}


def has_sufficient_balance(username: str) -> bool:
    """检查余额是否够扣至少1个联系人"""
    return get_balance(username) >= COST_PER_CONTACT


# ============================================================
# 流水查询
# ============================================================

def get_transactions(username: str, limit: int = 50) -> list:
    """获取用户最近交易流水"""
    db = get_db()
    cursor = db.transactions.find(
        {"username": username},
        {"_id": 0}
    ).sort("created_at", -1).limit(limit)
    return list(cursor)


def get_total_spent(username: str) -> float:
    """用户累计消费"""
    db = get_db()
    pipeline = [
        {"$match": {"username": username, "type": "deduct"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(db.transactions.aggregate(pipeline))
    return abs(result[0]["total"]) if result else 0.0


def get_total_topup(username: str) -> float:
    """用户累计充值"""
    db = get_db()
    pipeline = [
        {"$match": {"username": username, "type": "topup"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(db.transactions.aggregate(pipeline))
    return result[0]["total"] if result else 0.0


# ============================================================
# 管理员：所有用户余额概览
# ============================================================

def get_all_balances() -> list:
    """返回所有用户余额列表"""
    db = get_db()
    return list(db.balances.find({}, {"_id": 0}).sort("username", 1))
