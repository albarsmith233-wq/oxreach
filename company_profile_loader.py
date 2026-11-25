"""
company_profile_loader.py
统一公司档案加载：user_configs (MongoDB) → company_config.py (Kingphar默认)
所有模块用 get_company_profile(username) 替代 from company_config import COMPANY_PROFILE
"""
import logging
log = logging.getLogger(__name__)

def get_db():
    from app import get_db as _get_db
    return _get_db()

def get_company_profile(username: str) -> dict:
    """
    从 MongoDB user_configs 构建完整公司档案。
    字段兼容旧 COMPANY_PROFILE dict，引擎/邮件/分析模块可直接使用。
    """
    profile = {}
    try:
        cfg = get_db().user_configs.find_one({"username": username}, {"_id": 0})
        if cfg and cfg.get("company_name"):
            # 产品线：存储为换行分隔字符串，输出为 list
            products_raw = cfg.get("products", "")
            products = [p.strip() for p in products_raw.split("\n") if p.strip()] if products_raw else []

            # 认证：同上
            certs_raw = cfg.get("certifications", "")
            certs = [c.strip() for c in certs_raw.split("\n") if c.strip()] if certs_raw else []

            # 优势：同上
            adv_raw = cfg.get("company_advantages", "")
            advantages = [a.strip() for a in adv_raw.split("\n") if a.strip()] if adv_raw else []

            profile = {
                "name":               cfg.get("company_name", ""),
                "short_name":         cfg.get("company_short_name", cfg.get("company_name", "")),
                "website":            cfg.get("company_website", ""),
                "location":           cfg.get("company_location", ""),
                "contact_email":      cfg.get("sender_email", ""),
                "whatsapp":           cfg.get("company_whatsapp", ""),
                "products":           products,
                "certifications":     certs,
                "advantages":         advantages,
                "target_customer_types": [t.strip() for t in cfg.get("company_target_customer", "").split(",") if t.strip()],
                "key_selling_points": cfg.get("company_selling_points", ""),
                "industry":           cfg.get("company_industry", ""),
                "cta":                cfg.get("company_cta", ""),
                # 兼容旧字段名（scheduler_engine 用）
                "company_name":       cfg.get("company_name", ""),
                "product":            cfg.get("product", ""),
                "target_customer":    cfg.get("company_target_customer", ""),
            }
            return profile
    except Exception as e:
        log.warning("get_company_profile(%s) DB读取失败: %s", username, e)

    # Fallback: Kingphar 默认
    try:
        from company_config import COMPANY_PROFILE
        return COMPANY_PROFILE
    except Exception:
        return {
            "name": "our company",
            "company_name": "our company",
            "products": [],
            "certifications": [],
            "advantages": [],
            "key_selling_points": "",
        }
