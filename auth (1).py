# ============================================================
# Kingphar 账号管理模块 v2.0
# 存储优先级：MongoDB（持久）→ users.json（降级）
# 管理员账号从环境变量注入，不写入任何存储
# ============================================================

import os
import json
import hashlib
from datetime import datetime
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

USERS_FILE = "users.json"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


# ── 读写：MongoDB 优先，JSON 文件降级 ──────────────────────

def _load_from_mongo() -> dict:
    try:
        from db import get_db, is_available
        if not is_available():
            return {}
        docs = get_db().accounts.find({}, {"_id": 0})
        return {d["username"]: d for d in docs if "username" in d}
    except Exception:
        return {}

def _load_from_file() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_to_mongo(username: str, data: dict):
    try:
        from db import get_db, is_available
        if not is_available():
            return False
        get_db().accounts.update_one(
            {"username": username},
            {"$set": {"username": username, **data}},
            upsert=True
        )
        return True
    except Exception:
        return False

def _delete_from_mongo(username: str):
    try:
        from db import get_db, is_available
        if is_available():
            get_db().accounts.delete_one({"username": username})
    except Exception:
        pass

def _save_to_file(users: dict):
    admin_user = os.getenv("ADMIN_USERNAME", "albar")
    to_save = {k: v for k, v in users.items() if k != admin_user}
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_users() -> dict:
    """
    加载所有用户。优先从 MongoDB，降级到 JSON 文件。
    同时把文件里的用户迁移到 MongoDB（一次性）。
    管理员账号从环境变量注入。
    """
    # 从 MongoDB 读
    mongo_users = _load_from_mongo()

    # 从文件读，迁移到 MongoDB
    file_users = _load_from_file()
    for uname, udata in file_users.items():
        if uname not in mongo_users:
            _save_to_mongo(uname, udata)
            mongo_users[uname] = udata

    users = mongo_users if mongo_users else file_users

    # 注入管理员（不存任何地方）
    admin_user  = os.getenv("ADMIN_USERNAME", "albar")
    admin_pass  = os.getenv("ADMIN_PASSWORD", "")
    if admin_pass:
        users[admin_user] = {
            "name":       os.getenv("ADMIN_NAME",  "Albar"),
            "email":      os.getenv("ADMIN_EMAIL", "albar@kingphar.cn"),
            "smtp_pass":  os.getenv("ADMIN_SMTP_PASSWORD", ""),
            "role":       "admin",
            "password":   hash_password(admin_pass),
            "created_at": "系统内置",
        }
    return users


def save_users(users: dict):
    """保存所有子账号到 MongoDB + JSON 文件双写"""
    admin_user = os.getenv("ADMIN_USERNAME", "albar")
    for uname, udata in users.items():
        if uname != admin_user:
            _save_to_mongo(uname, udata)
    _save_to_file(users)


# ── 认证 ──────────────────────────────────────────────────

def authenticate(username: str, password: str) -> dict | None:
    users = load_users()
    user  = users.get(username.strip().lower())
    if not user:
        return None
    if verify_password(password, user["password"]):
        return user
    return None


# ── 账号管理 ──────────────────────────────────────────────

def add_user(username: str, name: str, email: str,
             password: str, smtp_pass: str) -> tuple[bool, str]:
    username = username.strip().lower()
    if not all([username, name, email, password]):
        return False, "所有字段均为必填"

    users = load_users()
    admin_user = os.getenv("ADMIN_USERNAME", "albar")

    if username == admin_user:
        return False, "不能使用管理员用户名"
    if username in users:
        return False, f"用户名 '{username}' 已存在"
    if any(u.get("email") == email for u in users.values()):
        return False, f"邮箱 '{email}' 已被使用"

    data = {
        "name":       name.strip(),
        "email":      email.strip().lower(),
        "smtp_pass":  smtp_pass.strip(),
        "role":       "user",
        "password":   hash_password(password),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    users[username] = data
    save_users(users)
    return True, f"✅ 账号 '{username}' 创建成功"


def delete_user(username: str) -> tuple[bool, str]:
    username   = username.strip().lower()
    admin_user = os.getenv("ADMIN_USERNAME", "albar")
    if username == admin_user:
        return False, "不能删除管理员账号"

    users = load_users()
    if username not in users:
        return False, f"用户 '{username}' 不存在"

    del users[username]
    _delete_from_mongo(username)
    _save_to_file(users)
    return True, f"✅ 账号 '{username}' 已删除"


def update_user_smtp(username: str, smtp_pass: str) -> tuple[bool, str]:
    username   = username.strip().lower()
    admin_user = os.getenv("ADMIN_USERNAME", "albar")

    if username == admin_user:
        return False, "管理员SMTP密码请在Railway Variables中修改 ADMIN_SMTP_PASSWORD"

    users = load_users()
    if username not in users:
        return False, "用户不存在"

    users[username]["smtp_pass"] = smtp_pass.strip()
    save_users(users)
    return True, "✅ SMTP密码已更新"


def update_password(username: str, old_pass: str, new_pass: str) -> tuple[bool, str]:
    user = authenticate(username, old_pass)
    if not user:
        return False, "原密码错误"

    admin_user = os.getenv("ADMIN_USERNAME", "albar")
    if username == admin_user:
        return False, "管理员密码请在Railway Variables中修改 ADMIN_PASSWORD"

    users = load_users()
    users[username]["password"] = hash_password(new_pass)
    save_users(users)
    return True, "✅ 密码已修改"


def get_user(username: str) -> dict | None:
    """获取单个用户信息"""
    return load_users().get(username.strip().lower())


def list_users() -> list[dict]:
    users = load_users()
    result = []
    for username, info in users.items():
        result.append({
            "username":   username,
            "name":       info.get("name", ""),
            "email":      info.get("email", ""),
            "role":       info.get("role", "user"),
            "created_at": info.get("created_at", ""),
            "has_smtp":   bool(info.get("smtp_pass")),
        })
    return result
