import os
import time
from copy import deepcopy
from pymongo import MongoClient, ASCENDING

_DEFAULT_DATA = {
    "accounts": [],
    "active_image_index": -1,
    "active_video_index": -1,
    "active_image_edit_index": -1,
    "active_ocr_index": -1,
}
_DEFAULT_PROFILE = {
    "points": 5,
    "total_extractions": 0,
    "is_blocked": False,
    "settings": {
        "output_format": "txt",
        "bubble_spacing": True,
        # تعطيل المؤثرات الصوتية هو الافتراضي المفعّل (تجاهل تام للـ SFX)
        "include_sfx": False,
        # الفاصل ثابت على اسم الصورة الأصلية
        "separator_mode": "original_name",
        # الترجمة الأدبية — ميزة جديدة معطلة افتراضيًا
        "translate_enabled": False,
        "translate_target": "ar",
    },
}
_DEFAULT_GUILD_PROFILE = {
    "guild_id": None,
    "points": 0,
    "guild_points_enabled": False,
    "total_extractions": 0,
}
_CLIENT = None
_DB = None


def _get_db():
    global _CLIENT, _DB
    if _DB is not None:
        return _DB
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI مفقود: بوت استخراج النصوص يستخدم MongoDB فقط ولا يحفظ الحسابات محليًا.")
    _CLIENT = MongoClient(uri, serverSelectionTimeoutMS=5000)
    _CLIENT.admin.command("ping")
    db_name = os.getenv("MONGODB_DB", "text_extractor_bot")
    _DB = _CLIENT[db_name]
    _DB.user_accounts.create_index([("user_id", ASCENDING)], unique=True)
    _DB.user_profiles.create_index([("user_id", ASCENDING)], unique=True)
    _DB.guild_profiles.create_index([("guild_id", ASCENDING)], unique=True)
    return _DB


def _normalize(data):
    result = deepcopy(_DEFAULT_DATA)
    if isinstance(data, dict):
        result.update({k: data.get(k, v) for k, v in result.items()})
    for acc in result.get("accounts", []):
        acc.setdefault("image_limit_until", 0)
        acc.setdefault("video_limit_until", 0)
        acc.setdefault("image_edit_limit_until", 0)
        acc.setdefault("ocr_limit_until", 0)
        acc.setdefault("image_count", 0)
        acc.setdefault("video_count", 0)
        acc.setdefault("image_edit_count", 0)
        acc.setdefault("ocr_count", 0)
    return result


def _normalize_profile(doc, user=None):
    result = deepcopy(_DEFAULT_PROFILE)
    if isinstance(doc, dict):
        settings = result["settings"]
        settings.update(doc.get("settings") or {})
        # Discard earlier per-user engine preferences. Only /zx controls these.
        for key in ("provider", "chatgpt_model", "_processing_config"):
            settings.pop(key, None)
        result.update({k: doc.get(k, v) for k, v in result.items() if k != "settings"})
        result["settings"] = settings
    if user is not None:
        result["user_id"] = str(user.id)
        result["username"] = getattr(user, "name", str(user.id))
        result["display_name"] = getattr(user, "display_name", result["username"])
    result.setdefault("created_at", int(time.time()))
    result["updated_at"] = int(time.time())
    return result


def _normalize_guild_profile(doc, guild_id=None):
    result = deepcopy(_DEFAULT_GUILD_PROFILE)
    if isinstance(doc, dict):
        result.update({k: doc.get(k, v) for k, v in result.items()})
    if guild_id is not None:
        result["guild_id"] = str(guild_id)
    result.setdefault("created_at", int(time.time()))
    result["updated_at"] = int(time.time())
    return result


def load_accounts_data(user_id):
    doc = _get_db().user_accounts.find_one({"user_id": str(user_id)}, {"_id": 0})
    return _normalize(doc or {})


def load_processing_config():
    return _get_db().bot_settings.find_one({"_id": "processing"}, {"_id": 0}) or {}


def save_processing_config(**changes):
    allowed = {"provider", "chatgpt_model", "gemini_cookie_header", "chatgpt_access_token"}
    if not changes or set(changes) - allowed:
        raise ValueError("إعداد معالجة غير صالح.")
    _get_db().bot_settings.update_one(
        {"_id": "processing"},
        {"$set": {**changes, "updated_at": int(time.time())}},
        upsert=True,
    )


def save_accounts_data(user_id, data):
    payload = _normalize(data)
    payload["user_id"] = str(user_id)
    payload["updated_at"] = int(time.time())
    _get_db().user_accounts.update_one({"user_id": str(user_id)}, {"$set": payload}, upsert=True)


def get_user_profile(user):
    doc = _get_db().user_profiles.find_one({"user_id": str(user.id)}, {"_id": 0})
    profile = _normalize_profile(doc, user)
    if doc is None:
        _get_db().user_profiles.update_one({"user_id": str(user.id)}, {"$set": profile}, upsert=True)
    else:
        _get_db().user_profiles.update_one(
            {"user_id": str(user.id)},
            {"$set": {
                "username": profile["username"],
                "display_name": profile["display_name"],
                "updated_at": profile["updated_at"],
            }},
            upsert=True,
        )
    return profile


def update_user_settings(user, **settings):
    profile = get_user_profile(user)
    profile["settings"].update(settings)
    _get_db().user_profiles.update_one(
        {"user_id": str(user.id)},
        {"$set": {"settings": profile["settings"], "updated_at": int(time.time())}},
        upsert=True,
    )
    return profile


def consume_point(user_id):
    db = _get_db()
    doc = db.user_profiles.find_one({"user_id": str(user_id)}, {"_id": 0}) or {}
    profile = _normalize_profile(doc)
    if profile.get("is_blocked"):
        return False, profile
    if int(profile.get("points", 0)) <= 0:
        return False, profile
    updated = db.user_profiles.find_one_and_update(
        {"user_id": str(user_id), "points": {"$gt": 0}, "is_blocked": {"$ne": True}},
        {"$inc": {"points": -1, "total_extractions": 1}, "$set": {"updated_at": int(time.time())}},
        return_document=True,
        projection={"_id": 0},
    )
    return bool(updated), _normalize_profile(updated or profile)


def admin_adjust_user(user_id, *, points_delta=0, set_points=None, blocked=None):
    update = {"$set": {"updated_at": int(time.time())}, "$setOnInsert": {"created_at": int(time.time())}}
    if set_points is not None:
        update["$set"]["points"] = max(0, int(set_points))
    if blocked is not None:
        update["$set"]["is_blocked"] = bool(blocked)
    if points_delta:
        update["$inc"] = {"points": int(points_delta)}
    _get_db().user_profiles.update_one({"user_id": str(user_id)}, update, upsert=True)
    return _normalize_profile(_get_db().user_profiles.find_one({"user_id": str(user_id)}, {"_id": 0}) or {})


def list_user_profiles(limit=25):
    return list(_get_db().user_profiles.find({}, {"_id": 0}).sort("updated_at", -1).limit(int(limit)))


# ============================================================
# دوال نقاط السيرفر
# ============================================================
def get_guild_profile(guild_id):
    db = _get_db()
    doc = db.guild_profiles.find_one({"guild_id": str(guild_id)}, {"_id": 0})
    profile = _normalize_guild_profile(doc, guild_id)
    if doc is None:
        db.guild_profiles.update_one({"guild_id": str(guild_id)}, {"$set": profile}, upsert=True)
    else:
        db.guild_profiles.update_one(
            {"guild_id": str(guild_id)},
            {"$set": {"updated_at": profile["updated_at"]}},
            upsert=True,
        )
    return profile


def server_has_points(guild_id):
    """يتحقق إذا كان السيرفر لديه نقاط > 0 — وجود الرصيد وحده يكفي لتفعيل خصم السيرفر."""
    profile = get_guild_profile(guild_id)
    return int(profile.get("points", 0)) > 0


def consume_server_point(guild_id):
    """يخصم نقطة من رصيد السيرفر — يكفي أن يكون الرصيد > 0 (لا يشترط تفعيلًا إضافيًا)."""
    db = _get_db()
    doc = db.guild_profiles.find_one({"guild_id": str(guild_id)}, {"_id": 0}) or {}
    profile = _normalize_guild_profile(doc, guild_id)
    if int(profile.get("points", 0)) <= 0:
        return False, profile
    updated = db.guild_profiles.find_one_and_update(
        {"guild_id": str(guild_id), "points": {"$gt": 0}},
        {"$inc": {"points": -1, "total_extractions": 1}, "$set": {"updated_at": int(time.time())}},
        return_document=True,
        projection={"_id": 0},
    )
    return bool(updated), _normalize_guild_profile(updated or profile, guild_id)


def admin_adjust_server(guild_id, *, points_delta=0, set_points=None, guild_points_enabled=None):
    update = {"$set": {"updated_at": int(time.time())}, "$setOnInsert": {"created_at": int(time.time())}}
    if set_points is not None:
        update["$set"]["points"] = max(0, int(set_points))
        if int(set_points) > 0:
            update["$set"]["guild_points_enabled"] = True
    if guild_points_enabled is not None:
        update["$set"]["guild_points_enabled"] = bool(guild_points_enabled)
    if points_delta:
        update["$inc"] = {"points": int(points_delta)}
        if int(points_delta) > 0:
            # إضافة نقاط للسيرفر تعني تلقائيًا أن الخصم يصبح من رصيد السيرفر
            update["$set"]["guild_points_enabled"] = True
    _get_db().guild_profiles.update_one({"guild_id": str(guild_id)}, update, upsert=True)
    return _normalize_guild_profile(
        _get_db().guild_profiles.find_one({"guild_id": str(guild_id)}, {"_id": 0}) or {},
        guild_id,
    )


def list_guild_profiles(limit=25):
    return list(_get_db().guild_profiles.find({}, {"_id": 0}).sort("updated_at", -1).limit(int(limit)))


def close_mongo():
    global _CLIENT, _DB
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None
    _DB = None
