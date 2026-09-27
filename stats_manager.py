import os
import json
import datetime
import threading
import time

import database as db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(BASE_DIR, "stats.json")
ERROR_LOG_FILE = os.path.join(BASE_DIR, "errors.log")
stats_lock = threading.RLock()
error_log_lock = threading.RLock()
_migration_done = False

last_global_run_time = 0


def _run_migration_once():
    global _migration_done
    if _migration_done:
        return
    _migration_done = True
    if os.path.exists(STATS_FILE) and os.path.getsize(STATS_FILE) > 0:
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("users") or data.get("cracked_history"):
                db.migrate_from_json(data)
                print("✅ [DB] Migrated legacy stats.json into MongoDB.")
        except Exception as e:
            print(f"⚠️ [DB] Legacy migration skipped: {e}")


def load_stats():
    """Returns aggregated stats dict compatible with legacy bot code."""
    with stats_lock:
        _run_migration_once()
        settings = db.get_settings()
        users = db.get_all_users()
        history = db.get_cracked_history()
        return {
            "total_views": settings.get("total_views", 0),
            "today_date": settings.get("today_date", ""),
            "today_views": settings.get("today_views", 0),
            "success_count": settings.get("success_count", 0),
            "fail_count": settings.get("fail_count", 0),
            "mode": settings.get("mode", "free"),
            "default_credits": settings.get("default_credits", 3),
            "users": users,
            "cracked_history": history,
            "today_users": settings.get("today_users", []),
            "cooldown_seconds": settings.get("cooldown_seconds", 60),
            "max_concurrent_tasks": settings.get("max_concurrent_tasks", 15),
            "maintenance_mode": settings.get("maintenance_mode", False),
        }


def save_stats(data):
    """Persists settings and user list changes to MongoDB."""
    with stats_lock:
        settings_updates = {}
        for key in [
            "total_views",
            "today_date",
            "today_views",
            "success_count",
            "fail_count",
            "mode",
            "default_credits",
            "today_users",
            "cooldown_seconds",
            "max_concurrent_tasks",
            "maintenance_mode",
        ]:
            if key in data:
                settings_updates[key] = data[key]
        if settings_updates:
            db.update_settings(settings_updates)

        for user in data.get("users", []):
            if not isinstance(user, dict):
                continue
            cid = user.get("chat_id")
            if cid is None:
                continue
            db.upsert_user(cid, updates=user)


def register_visit(chat_id, username=None, first_name=None):
    with stats_lock:
        settings = db.get_settings()
        today = datetime.date.today().isoformat()
        try:
            str_chat_id = int(chat_id)
        except (TypeError, ValueError):
            str_chat_id = chat_id

        today_users = list(settings.get("today_users", []))
        updates = {}

        if settings.get("today_date") != today:
            updates["today_date"] = today
            updates["today_views"] = 1
            updates["today_users"] = [str_chat_id]
            updates["total_views"] = settings.get("total_views", 0) + 1
        else:
            if str_chat_id not in today_users:
                today_users.append(str_chat_id)
                updates["today_views"] = settings.get("today_views", 0) + 1
                updates["total_views"] = settings.get("total_views", 0) + 1
                updates["today_users"] = today_users

        if updates:
            db.update_settings(updates)

        user_updates = {}
        if first_name:
            user_updates["first_name"] = first_name
        if username:
            user_updates["username"] = username
        db.upsert_user(str_chat_id, updates=user_updates or None)


def record_success(chat_id, user_info, name, mobile, uid, password, eid=None):
    with stats_lock:
        settings = db.get_settings()
        db.update_settings({"success_count": settings.get("success_count", 0) + 1})

        username = user_info.get("username", "N/A") if user_info else "N/A"
        first_name = user_info.get("first_name", "N/A") if user_info else "N/A"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        record = {
            "timestamp": now,
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "name": name,
            "mobile": mobile,
            "uid": uid,
            "password": password,
            "eid": eid or "N/A",
        }
        db.add_cracked_record(record)

        txt_file_path = os.path.join(BASE_DIR, "cracked_history.txt")
        try:
            settings = db.get_settings()
            with open(txt_file_path, "a", encoding="utf-8") as f:
                f.write("============================================================\n")
                f.write(f"{settings.get('success_count', 0)}. [Timestamp: {now}]\n")
                f.write(f"   👤 Telegram User: {first_name} (@{username}) [ID: {chat_id}]\n")
                f.write(f"   🆔 Holder Name: {name}\n")
                f.write(f"   📞 Mobile Number: {mobile}\n")
                f.write(f"   🆔 Enrollment ID (EID): {eid or 'N/A'}\n")
                f.write(f"   🔑 Cracked Aadhaar UID: {uid}\n")
                f.write(f"   🔓 PDF Password: {password}\n")
                f.write("============================================================\n\n")
        except Exception as e:
            print(f"⚠️ [STATS] Failed to write to cracked_history.txt: {e}")

        try:
            deduct_user_credit(chat_id)
        except Exception as e:
            print(f"⚠️ [STATS] Error during success credit deduction: {e}")


def record_failure():
    with stats_lock:
        settings = db.get_settings()
        db.update_settings({"fail_count": settings.get("fail_count", 0) + 1})


def get_stats_summary(active_user_states):
    data = load_stats()

    steps_breakdown = {}
    for _, state_dict in active_user_states.items():
        step = state_dict.get("step", "IDLE")
        if step != "IDLE":
            steps_breakdown[step] = steps_breakdown.get(step, 0) + 1

    active_count = sum(steps_breakdown.values())
    if active_count > 0:
        active_details = ""
        for step, count in steps_breakdown.items():
            active_details += f"  ├─ <code>{step:<18}</code>: <b>{count} user(s)</b>\n"
    else:
        active_details = "  └─ <i>No users are running active tasks.</i>\n"

    mode = data.get("mode", "free").upper()
    def_credits = data.get("default_credits", 3)
    cooldown = data.get("cooldown_seconds", 60)
    max_concurrent = data.get("max_concurrent_tasks", 15)
    maintenance = data.get("maintenance_mode", False)

    total_runs = data["success_count"] + data["fail_count"]
    success_rate = 100 if total_runs == 0 else int((data["success_count"] / total_runs) * 100)

    total_users = db.count_users(include_banned=False)
    banned_users = db.count_users(include_banned=True) - total_users

    try:
        db.ping_database()
        db_status = "🟢 Online"
    except Exception:
        db_status = "🔴 Offline"

    maintenance_str = "🔴 ON" if maintenance else "🟢 OFF"

    return (
        "<b>👑 OWNER CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>SYSTEM CONFIGURATION</b>\n"
        f"  ├─ Bot Mode:         <b>{mode}</b>\n"
        f"  ├─ Maintenance:      <b>{maintenance_str}</b>\n"
        f"  ├─ Default Credits:  <code>{def_credits}</code>\n"
        f"  ├─ Global Cooldown:  <code>{cooldown}s</code>\n"
        f"  ├─ Max Concurrency:  <code>{max_concurrent}</code>\n"
        f"  └─ MongoDB:          <b>{db_status}</b>\n\n"
        "📊 <b>LIVE METRICS</b>\n"
        f"  ├─ Total Views:      <code>{data['total_views']}</code>\n"
        f"  ├─ Today Views:      <code>{data['today_views']}</code>\n"
        f"  ├─ Active Users:     <code>{total_users}</code>\n"
        f"  ├─ Banned Users:     <code>{banned_users}</code>\n"
        f"  └─ Success Rate:     <b>{success_rate}%</b> "
        f"(<code>{data['success_count']} ✅</code> / <code>{data['fail_count']} ❌</code>)\n\n"
        f"👥 <b>ACTIVE TASKS:</b> <code>{active_count}</code>\n"
        f"{active_details}"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


def get_cracked_data_file_path():
    data = load_stats()
    report_path = os.path.join(BASE_DIR, "cracked_history_report.txt")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("============================================================\n")
            f.write("🔒 CRACKED AADHAAR DATABASE REPORT\n")
            f.write(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total Successes: {data['success_count']}\n")
            f.write("============================================================\n\n")
            history = data.get("cracked_history", [])
            if not history:
                f.write("No cracked Aadhaar records found in database history yet.\n")
            else:
                for idx, record in enumerate(history, 1):
                    f.write("============================================================\n")
                    f.write(f"{idx}. [Timestamp: {record.get('timestamp', 'N/A')}]\n")
                    f.write(f"   👤 Telegram User: {record.get('first_name', 'N/A')} (@{record.get('username', 'N/A')}) [ID: {record.get('chat_id', 'N/A')}]\n")
                    f.write(f"   🆔 Holder Name: {record.get('name', 'N/A')}\n")
                    f.write(f"   📞 Mobile Number: {record.get('mobile', 'N/A')}\n")
                    f.write(f"   🆔 Enrollment ID (EID): {record.get('eid', 'N/A')}\n")
                    f.write(f"   🔑 Cracked Aadhaar UID: {record.get('uid', 'N/A')}\n")
                    f.write(f"   🔓 PDF Password: {record.get('password', 'N/A')}\n")
                    f.write("============================================================\n\n")
        return report_path
    except Exception as e:
        print(f"⚠️ [STATS] Failed to generate text database report: {e}")
        return None


def log_error(chat_id, user_info, error_msg):
    with error_log_lock:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = user_info.get("username", "N/A") if user_info else "N/A"
        first_name = user_info.get("first_name", "N/A") if user_info else "N/A"
        entry = {
            "timestamp": now,
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "error": error_msg,
        }
        try:
            db.add_error_log(entry)
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{now}] User: {first_name} (@{username}) [ID: {chat_id}] | Error: {error_msg}\n")
        except Exception as e:
            print(f"⚠️ [STATS] Failed to write error log: {e}")


def get_error_log_file_path():
    if os.path.exists(ERROR_LOG_FILE) and os.path.getsize(ERROR_LOG_FILE) > 0:
        return ERROR_LOG_FILE
    return None


def get_bot_mode():
    return db.get_settings().get("mode", "free")


def set_bot_mode(mode):
    db.update_settings({"mode": mode})


def get_default_credits():
    return db.get_settings().get("default_credits", 3)


def set_default_credits(count):
    db.update_settings({"default_credits": int(count)})


def get_user_credits(chat_id):
    user = db.get_user(chat_id)
    if user:
        return user.get("credits", get_default_credits())
    return get_default_credits()


def add_user_credits(chat_id, amount):
    settings = db.get_settings()
    user = db.get_user(chat_id)
    if user:
        new_balance = max(0, user.get("credits", settings.get("default_credits", 3)) + amount)
        db.upsert_user(chat_id, updates={"credits": new_balance})
        return new_balance

    new_balance = max(0, settings.get("default_credits", 3) + amount)
    db.upsert_user(chat_id, updates={"credits": new_balance})
    return new_balance


def deduct_user_credit(chat_id):
    if get_bot_mode() != "paid":
        return
    user = db.get_user(chat_id)
    if not user:
        return
    settings = db.get_settings()
    current = user.get("credits", settings.get("default_credits", 3))
    db.upsert_user(chat_id, updates={"credits": max(0, current - 1)})


def check_global_cooldown():
    global last_global_run_time
    now = time.time()
    elapsed = now - last_global_run_time
    cooldown_limit = get_cooldown_seconds()
    if elapsed >= cooldown_limit:
        return True, 0
    return False, max(1, cooldown_limit - int(elapsed))


def update_global_run_time():
    global last_global_run_time
    last_global_run_time = time.time()


def get_cooldown_seconds():
    return db.get_settings().get("cooldown_seconds", 60)


def set_cooldown_seconds(val):
    db.update_settings({"cooldown_seconds": int(val)})


def get_max_concurrent_tasks():
    return db.get_settings().get("max_concurrent_tasks", 15)


def set_max_concurrent_tasks(val):
    db.update_settings({"max_concurrent_tasks": int(val)})


def is_user_registered(chat_id):
    return db.get_user(chat_id) is not None


def find_cracked_record(mobile):
    return db.find_cracked_by_mobile(mobile)


def is_maintenance_mode():
    return bool(db.get_settings().get("maintenance_mode", False))


def set_maintenance_mode(enabled):
    db.update_settings({"maintenance_mode": bool(enabled)})


def is_user_banned(chat_id):
    return db.is_user_banned(chat_id)


def ban_user(chat_id):
    db.set_user_banned(chat_id, True)


def unban_user(chat_id):
    db.set_user_banned(chat_id, False)


def set_user_referred_by(chat_id, referrer_id):
    db.upsert_user(chat_id, updates={"referred_by": referrer_id})


def mark_user_verified(chat_id):
    db.upsert_user(chat_id, updates={"verified": True})


def get_users_paginated(page=1, page_size=5):
    skip = max(0, (page - 1) * page_size)
    users = db.get_all_users(limit=page_size, skip=skip)
    total = db.count_users(include_banned=True)
    return users, total
