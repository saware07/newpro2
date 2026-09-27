import os
import datetime
import threading
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

_db_lock = threading.RLock()
_client = None
_db = None

DEFAULT_SETTINGS = {
    "total_views": 0,
    "today_date": "",
    "today_views": 0,
    "success_count": 0,
    "fail_count": 0,
    "mode": "free",
    "default_credits": 3,
    "cooldown_seconds": 60,
    "max_concurrent_tasks": 15,
    "maintenance_mode": False,
    "today_users": [],
}


def get_db():
    global _client, _db
    with _db_lock:
        if _db is not None:
            return _db

        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI is not configured in .env")

        db_name = os.getenv("MONGODB_DB_NAME", "aadhar_bot")
        _client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        _db = _client[db_name]
        _ensure_indexes(_db)
        _ensure_settings(_db)
        return _db


def _ensure_indexes(db):
    db.users.create_index("chat_id", unique=True)
    db.users.create_index("banned")
    db.cracked_history.create_index("mobile")
    db.pdf_logs.create_index("user_id")
    db.cracked_history.create_index([("timestamp", DESCENDING)])
    db.error_logs.create_index([("timestamp", DESCENDING)])


def _ensure_settings(db):
    if db.bot_settings.find_one({"_id": "global"}) is None:
        doc = {"_id": "global", **DEFAULT_SETTINGS}
        db.bot_settings.insert_one(doc)


def get_settings():
    db = get_db()
    doc = db.bot_settings.find_one({"_id": "global"}) or {}
    settings = {**DEFAULT_SETTINGS}
    for key in DEFAULT_SETTINGS:
        if key in doc:
            settings[key] = doc[key]
    return settings


def update_settings(updates):
    db = get_db()
    db.bot_settings.update_one({"_id": "global"}, {"$set": updates}, upsert=True)


def get_user(chat_id):
    db = get_db()
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = chat_id
    return db.users.find_one({"chat_id": cid})


def upsert_user(chat_id, updates=None, create_defaults=None):
    db = get_db()
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = chat_id

    existing = db.users.find_one({"chat_id": cid})
    if existing:
        if updates:
            db.users.update_one({"chat_id": cid}, {"$set": updates})
        return db.users.find_one({"chat_id": cid})

    settings = get_settings()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc = {
        "chat_id": cid,
        "first_name": "N/A",
        "username": "N/A",
        "joined": now_str,
        "credits": settings.get("default_credits", 3),
        "referred_by": None,
        "banned": False,
        "verified": False,
    }
    if create_defaults:
        doc.update(create_defaults)
    if updates:
        doc.update(updates)
    db.users.insert_one(doc)
    return doc


def get_all_users(limit=None, skip=0, include_banned=True):
    db = get_db()
    query = {} if include_banned else {"banned": {"$ne": True}}
    cursor = db.users.find(query).sort("joined", DESCENDING)
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)


def count_users(include_banned=True):
    db = get_db()
    query = {} if include_banned else {"banned": {"$ne": True}}
    return db.users.count_documents(query)


def set_user_banned(chat_id, banned=True):
    upsert_user(chat_id)
    db = get_db()
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = chat_id
    db.users.update_one({"chat_id": cid}, {"$set": {"banned": bool(banned)}})
    return True


def is_user_banned(chat_id):
    user = get_user(chat_id)
    return bool(user and user.get("banned"))


def add_cracked_record(record):
    db = get_db()
    db.cracked_history.insert_one(record)


def get_cracked_history(limit=None):
    db = get_db()
    cursor = db.cracked_history.find().sort("_id", ASCENDING)
    if limit:
        cursor = cursor.skip(max(0, db.cracked_history.count_documents({}) - limit))
    return list(cursor)


def find_cracked_by_mobile(mobile):
    import re

    clean_mobile = re.sub(r"\D", "", str(mobile))
    db = get_db()
    for record in db.cracked_history.find():
        rec_mobile = re.sub(r"\D", "", str(record.get("mobile", "")))
        if rec_mobile == clean_mobile:
            return record
    return None


def find_user_cracked_record(chat_id, target_input):
    """Strict per-user chat history lookup for instant PDF and details re-delivery."""
    try:
        db = get_db()
        clean_target = ''.join(filter(str.isdigit, str(target_input)))
        if len(clean_target) >= 10:
            clean_target = clean_target[-10:]  # Normalize to last 10 digits
            
        try:
            cid_str = str(chat_id)
            cid_int = int(chat_id)
        except:
            cid_str = str(chat_id)
            cid_int = chat_id
            
        for record in db.cracked_history.find({"chat_id": {"$in": [cid_str, cid_int]}}):
            rec_mob = ''.join(filter(str.isdigit, str(record.get("mobile", ""))))
            rec_eid = ''.join(filter(str.isdigit, str(record.get("eid", ""))))
            rec_uid = ''.join(filter(str.isdigit, str(record.get("uid", ""))))
            
            if clean_target in rec_mob or clean_target in rec_eid or clean_target in rec_uid or rec_mob.endswith(clean_target):
                return record
    except Exception as e:
        print(f"⚠️ [DB USER HISTORY ERROR]: {e}")
    return None


# Alias for compatibility with any module expecting find_user_pdf_log
find_user_pdf_log = find_user_cracked_record


def add_error_log(entry):
    db = get_db()
    db.error_logs.insert_one(entry)


def get_recent_error_logs(limit=10):
    db = get_db()
    return list(db.error_logs.find().sort("timestamp", DESCENDING).limit(limit))


def ping_database():
    db = get_db()
    db.command("ping")
    return True


def migrate_from_json(stats_data):
    """One-time migration helper from legacy stats.json layout."""
    if not stats_data:
        return

    settings_keys = set(DEFAULT_SETTINGS.keys())
    settings_updates = {k: stats_data[k] for k in settings_keys if k in stats_data}
    if settings_updates:
        update_settings(settings_updates)

    db = get_db()
    for user in stats_data.get("users", []):
        if not isinstance(user, dict):
            continue
        cid = user.get("chat_id")
        if cid is None:
            continue
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            pass
        if db.users.find_one({"chat_id": cid}):
            continue
        doc = {
            "chat_id": cid,
            "first_name": user.get("first_name", "N/A"),
            "username": user.get("username", "N/A"),
            "joined": user.get("joined", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "credits": user.get("credits", stats_data.get("default_credits", 3)),
            "referred_by": user.get("referred_by"),
            "banned": user.get("banned", False),
            "verified": user.get("verified", False),
        }
        db.users.insert_one(doc)

    for record in stats_data.get("cracked_history", []):
        if db.cracked_history.find_one({"timestamp": record.get("timestamp"), "mobile": record.get("mobile")}):
            continue
        db.cracked_history.insert_one(record)
