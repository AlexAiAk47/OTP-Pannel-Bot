#!/usr/bin/env python3
"""
ALEX OTP PANEL — TELEGRAM BOT v3.4
LIVE STREAMING · Numbers appear while firebases load
NEW SMS BOX · Only new OTP shown
OLD BOX AUTO-DELETE · 2-MIN TTL · MANUAL REFRESH DELETE
FAST OTP DETECTION (v3.0 logic restored)
Developer: @Alex_Aditya
"""

import os
import re
import json
import time
import hashlib
import asyncio
import threading
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import requests
from pymongo import MongoClient

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
    Document
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, TelegramError

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8924019584:AAE8jyBLj1Fu_6JHXG8PSmyzPIPZB5sLdCw")
MONGO_URI = os.environ.get("MONGO_URI", "")
FB_HASH_SALT = os.environ.get("FB_HASH_SALT", "alex_fb_secure_salt_2025_v23")

ADMIN_IDS = set()
_raw = os.environ.get("ADMIN_IDS", "8644019747")
for x in _raw.split(","):
    try:
        ADMIN_IDS.add(int(x.strip()))
    except Exception:
        pass

MAX_SMS_PER_NUMBER = 10
FETCH_TIMEOUT = 6
FAST_FETCH_TIMEOUT = 6
MAX_DEVICES_PER_FB = 100
IST = timezone(timedelta(hours=5, minutes=30))

INBOX_REFRESH_SEC = 3
NEW_SMS_BOX_TTL = 120  # 2 minutes — auto delete new SMS box

BUILD_PARALLEL = 50
BUILD_REBUILD_INTERVAL = 300
BUILD_THREAD_TIMEOUT = 900

NUMBERS_PER_PAGE = 20

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger("AlexOTPBot")

# ============================================================
# MONGO
# ============================================================
mongo_client = None
db = None
fb_collection = None
settings_collection = None
users_collection = None
MONGO_OK = False


def init_mongo():
    global mongo_client, db, fb_collection, settings_collection, users_collection, MONGO_OK
    try:
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            socketTimeoutMS=8000,
            maxPoolSize=100,
        )
        db = mongo_client["otp_panel"]
        fb_collection = db["firebases"]
        settings_collection = db["settings"]
        users_collection = db["bot_users"]
        mongo_client.admin.command("ping")
        MONGO_OK = True
        logger.info("MongoDB connected")
    except Exception as e:
        MONGO_OK = False
        logger.error(f"MongoDB error: {e}")


def fb_hash(url: str) -> str:
    return hashlib.sha256((FB_HASH_SALT + url.rstrip("/")).encode()).hexdigest()[:16]


# ============================================================
# PREMIUM
# ============================================================
def get_user(user_id: int):
    if not MONGO_OK:
        return None
    return users_collection.find_one({"user_id": user_id})


def ensure_user(user_id: int, username: str = "", first_name: str = ""):
    if not MONGO_OK:
        return None
    users_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username or "",
                "first_name": first_name or "",
                "last_seen": int(time.time()),
            },
            "$setOnInsert": {"premium_until": 0, "joined_at": int(time.time())},
        },
        upsert=True,
    )
    return get_user(user_id)


def is_premium(user_id: int) -> bool:
    u = get_user(user_id)
    if not u:
        return False
    return int(u.get("premium_until", 0)) > int(time.time())


def premium_days_left(user_id: int) -> int:
    u = get_user(user_id)
    if not u:
        return 0
    left = int(u.get("premium_until", 0)) - int(time.time())
    return max(0, left // 86400)


def grant_premium(user_id: int, days: int):
    if not MONGO_OK:
        return False, 0
    u = get_user(user_id) or {}
    current = int(u.get("premium_until", 0))
    now = int(time.time())
    base = current if current > now else now
    new_until = base + days * 86400
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"premium_until": new_until, "granted_at": now}},
        upsert=True,
    )
    return True, new_until


def revoke_premium(user_id: int):
    if not MONGO_OK:
        return False
    users_collection.update_one({"user_id": user_id}, {"$set": {"premium_until": 0}})
    return True


def premium_status_text(user_id: int) -> str:
    if is_premium(user_id):
        left = premium_days_left(user_id)
        u = get_user(user_id) or {}
        until = int(u.get("premium_until", 0))
        dt = datetime.fromtimestamp(until, tz=IST).strftime("%d-%m-%Y")
        return f"✅ <b>PREMIUM ACTIVE</b> — {left} din baaki (until {dt})"
    return "❌ <b>FREE USER</b> — Inbox open karne ke liye premium chahiye"


# ============================================================
# HELPERS
# ============================================================
def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def short(s, n=60):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def extract_otp(text):
    if not text:
        return ""
    m = re.search(r"(?:otp|one[- ]time|password|code|pin|verification|passcode)[^\d]{0,30}(\d{4,8})",
                  text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4,8})[^\d]{0,30}(?:is your|otp|one[- ]time|code)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"#(\d{4,8})", text)
    if m:
        return m.group(1)
    return ""


def extract_real_numbers_from_message(message):
    if not message:
        return []
    text = re.sub(r'\d{3,4}[Xx*]{2,6}\d{2,4}', ' ', str(message))
    found, seen = [], set()
    for m in re.finditer(r'\+91[\s\-]?([6-9]\d{9})', text):
        num = m.group(1)
        if num not in seen:
            seen.add(num); found.append(num)
    for m in re.finditer(r'(?<!\d)([6-9]\d{9})(?!\d)', text):
        num = m.group(1)
        if num not in seen:
            seen.add(num); found.append(num)
    return found


def parse_date_string(s):
    if not s or not isinstance(s, str):
        return 0
    try:
        m = re.match(
            r"(\d{1,2})-(\d{1,2})-(\d{4})\s*\|\s*(\d{1,2}):(\d{2})\s*(am|pm)",
            s.strip(), re.IGNORECASE)
        if not m:
            return 0
        day, month, year, hour, minute, ap = m.groups()
        hour = int(hour)
        if ap.lower() == "pm" and hour != 12:
            hour += 12
        if ap.lower() == "am" and hour == 12:
            hour = 0
        dt = datetime(int(year), int(month), int(day), hour, int(minute), tzinfo=IST)
        return int(dt.timestamp())
    except Exception:
        return 0


MSG_FIELDS = {"message","body","text","msg","content","messagetext","messagebody","sms","smsbody","smscontent","smsbodytext","lastmessage","fullmessage","sms_message","msgbody"}
TIME_FIELDS = {"timestamp","time","date","receivedtime","datetime","createdat","senttime","smstime","devicetime","date_time","received_time","sent_time","sms_time","device_time","smsdate","receivedat","sentat","lastmessagetime","recvtime","logtime","event_time"}
SENT_KEYS = {"command","commands","sendsms","action","webhookevent","send_sms","sentsms","outgoing"}
INCOMING_TYPES = {"incoming","received","inbox","recv","in"}


def collect_all_messages(obj, found=None, depth=0, parent_ts=0, parent_key=""):
    if found is None:
        found = []
    if depth > 8:
        return found
    pk_lower = parent_key.lower()
    if any(k in pk_lower for k in SENT_KEYS):
        return found
    if isinstance(obj, dict):
        kl = {k.lower() for k in obj.keys()}
        if kl & MSG_FIELDS:
            def get_any(fs):
                for k in obj.keys():
                    if k.lower() in fs:
                        return obj[k]
                return None
            body = get_any(MSG_FIELDS)
            from_val = None
            for k in obj.keys():
                if k.lower() in ("sender", "from", "origin", "address", "src"):
                    v = obj[k]
                    if v and isinstance(v, str) and v.strip():
                        from_val = v.strip(); break
            to_val = None
            for k in obj.keys():
                if k.lower() in ("to", "destination", "receiver"):
                    to_val = obj[k]; break
            date_str = None
            for k in obj.keys():
                if k.lower() in ("datetime", "date_time", "date", "datetimestr"):
                    v = obj[k]
                    if isinstance(v, str) and ("|" in v or "am" in v.lower() or "pm" in v.lower()):
                        date_str = v; break
            msg_id = obj.get("id")
            ts = parse_date_string(date_str) if date_str else 0
            if not ts:
                ts_raw = get_any(TIME_FIELDS)
                if ts_raw:
                    try:
                        ts = int(ts_raw)
                        if ts > 1e12: ts //= 1000
                    except Exception:
                        ts = 0
            if not ts and msg_id:
                try:
                    mid = int(msg_id)
                    if mid > 1e12: ts = mid // 1000
                    elif mid > 1e9: ts = mid
                except Exception:
                    pass
            if not ts:
                ts = parent_ts
            msg_type = str(obj.get("type", "")).lower().strip()
            is_sended = obj.get("isSended") if "isSended" in obj else obj.get("isSent")
            keep = False
            if msg_type in INCOMING_TYPES:
                keep = True
            elif msg_type in ("sent", "outgoing"):
                keep = False
            elif is_sended is True:
                keep = False
            elif is_sended is False:
                keep = True
            elif from_val and not to_val:
                keep = True
            elif to_val and not from_val:
                keep = False
            elif not to_val and not from_val:
                keep = True
            if keep and body:
                found.append({
                    "from": str(from_val) if from_val else "Unknown",
                    "to": str(to_val) if to_val else "",
                    "message": str(body),
                    "timestamp": ts,
                    "date_string": date_str,
                    "id": msg_id,
                    "type": "received",
                })
        for k, v in obj.items():
            kl2 = k.lower()
            if any(x in kl2 for x in SENT_KEYS):
                continue
            child_ts = parent_ts
            try:
                tk = int(k)
                if tk > 1e12: child_ts = tk // 1000
                elif tk > 1e9: child_ts = tk
            except Exception:
                pass
            collect_all_messages(v, found, depth + 1, child_ts, k)
    elif isinstance(obj, list):
        for item in obj:
            collect_all_messages(item, found, depth + 1, parent_ts, parent_key)
    return found


def is_online(info):
    if not isinstance(info, dict):
        return False
    if info.get("status") is True: return True
    if info.get("isOnline") is True: return True
    if info.get("online") is True: return True
    if info.get("connected") is True: return True
    if info.get("status") in ("online", "active", 1): return True
    if info.get("lastSeen"):
        try:
            ls = int(info.get("lastSeen"))
            if time.time() - ls < 600:
                return True
        except Exception:
            pass
    return False


def find_top_numbers(all_msgs):
    counter = Counter()
    sender_numbers = set()
    for m in all_msgs:
        for sn in extract_real_numbers_from_message(m.get("from", "")):
            sender_numbers.add(sn)
    for m in all_msgs:
        if m.get("type") != "received":
            continue
        for n in extract_real_numbers_from_message(m.get("message", "")):
            if n in sender_numbers: continue
            if n.startswith(("1800", "1900", "1991")): continue
            counter[n] += 1
    return counter.most_common(50)


# ============================================================
# FIREBASE FETCH
# ============================================================
_thread_local = threading.local()


def _get_session():
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=50, pool_maxsize=50, max_retries=0
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return _thread_local.session


def try_json(url, timeout=FETCH_TIMEOUT):
    try:
        r = _get_session().get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        t = r.text.strip()
        if not t or t == "null":
            return None
        return json.loads(t)
    except Exception:
        return None


def fetch_device_ids(fb_url):
    base = fb_url.rstrip("/")
    for path in ["/clients.json?shallow=true", "/devices.json?shallow=true"]:
        data = try_json(base + path, timeout=FAST_FETCH_TIMEOUT)
        if isinstance(data, dict):
            return list(data.keys())
    return []


def fetch_device_data(fb_url, dev_id):
    base = fb_url.rstrip("/")
    data = try_json(f"{base}/clients/{dev_id}.json", timeout=FAST_FETCH_TIMEOUT)
    if isinstance(data, dict):
        return data
    data = try_json(f"{base}/devices/{dev_id}.json", timeout=FAST_FETCH_TIMEOUT)
    return data if isinstance(data, dict) else None


def fetch_messages_multi_path(fb_url, dev_id):
    """⚡ Fetch last messages with NO artificial limit (so OTP miss na ho)."""
    base = fb_url.rstrip("/")
    # ⚡ Original v3.0 logic: limitToLast=20 (enough for most OTPs)
    url = f'{base}/messages/{dev_id}.json?orderBy="$key"&limitToLast=20'
    data = try_json(url, timeout=FAST_FETCH_TIMEOUT)
    if not data:
        return []
    if isinstance(data, dict):
        msgs = collect_all_messages(data)
        msgs.sort(key=lambda x: x.get("timestamp", 0) or 0, reverse=True)
        return msgs[:MAX_SMS_PER_NUMBER]
    return []


def fetch_devices_from_one_fb(fb_url, fb_name="", fb_id=""):
    out = []
    try:
        device_ids = fetch_device_ids(fb_url)
    except Exception:
        return []
    if not device_ids:
        return []
    if len(device_ids) > MAX_DEVICES_PER_FB:
        device_ids = device_ids[:MAX_DEVICES_PER_FB]

    for did in device_ids:
        try:
            raw = fetch_device_data(fb_url, did)
            if not raw:
                continue
            msgs = fetch_messages_multi_path(fb_url, did)
            inner_msgs = collect_all_messages(raw)
            existing = {f"{m.get('from','')}|{m.get('message','')[:80]}|{m.get('timestamp',0)}" for m in msgs}
            for m in inner_msgs:
                h = f"{m.get('from','')}|{m.get('message','')[:80]}|{m.get('timestamp',0)}"
                if h not in existing:
                    msgs.append(m); existing.add(h)
            msgs.sort(key=lambda x: x.get("timestamp", 0) or 0, reverse=True)
            msgs = msgs[:MAX_SMS_PER_NUMBER]

            online = is_online(raw)
            name = did[:16]
            for f in ["deviceName","device_name","name","model","deviceModel","brand"]:
                v = raw.get(f) if isinstance(raw, dict) else None
                if v and isinstance(v, str) and v.strip():
                    name = v.strip()[:25]; break

            top = find_top_numbers(msgs)
            if top:
                real_number, count = top[0]
                sims = [{"slot": 0, "number": real_number, "online": online, "confidence": count}]
            else:
                sims = [{"slot": 0, "number": "UNKNOWN-" + did[:6], "online": online, "confidence": 0}]

            total_otp = sum(1 for m in msgs if extract_otp(m.get("message", "")))
            out.append({
                "id": did,
                "name": name,
                "online": online,
                "sims": sims,
                "battery": raw.get("battery", 0) if isinstance(raw, dict) else 0,
                "messages": msgs,
                "total_msgs": len(msgs),
                "total_otp": total_otp,
                "firebase": fb_url,
                "fb_name": fb_name,
                "fb_id": fb_id,
            })
        except Exception:
            continue
    return out


# ============================================================
# LIVE CACHE
# ============================================================
LIVE_CACHE = {
    "numbers": {},
    "online": 0,
    "offline": 0,
    "total_otp": 0,
    "total_msg": 0,
    "loading": False,
    "last_update": 0,
    "progress": {"done": 0, "total": 0},
    "lock": threading.Lock(),
    "build_count": 0,
    "build_started_at": 0,
    "build_elapsed": 0,
    "last_stream_add": 0,
}


def _add_number_to_live_cache(entry):
    with LIVE_CACHE["lock"]:
        key = entry["number"]
        existing = LIVE_CACHE["numbers"].get(key)
        if not existing or entry.get("appear_count", 0) > existing.get("appear_count", 0):
            LIVE_CACHE["numbers"][key] = entry
        LIVE_CACHE["last_stream_add"] = time.time()
        nums = list(LIVE_CACHE["numbers"].values())
        LIVE_CACHE["online"] = sum(1 for n in nums if n.get("online"))
        LIVE_CACHE["offline"] = sum(1 for n in nums if not n.get("online"))
        LIVE_CACHE["total_otp"] = sum(n.get("otp_count", 0) for n in nums)
        LIVE_CACHE["total_msg"] = sum(n.get("msg_count", 0) for n in nums)


def build_all_numbers(force=False):
    if not MONGO_OK:
        return []
    with LIVE_CACHE["lock"]:
        if LIVE_CACHE["loading"] and not force:
            return list(LIVE_CACHE["numbers"].values())
        LIVE_CACHE["loading"] = True
        LIVE_CACHE["progress"] = {"done": 0, "total": 0}
        LIVE_CACHE["build_started_at"] = time.time()
        if force:
            LIVE_CACHE["numbers"] = {}
            LIVE_CACHE["online"] = 0
            LIVE_CACHE["offline"] = 0
            LIVE_CACHE["total_otp"] = 0
            LIVE_CACHE["total_msg"] = 0

    try:
        try:
            firebases = list(fb_collection.find({}, {"url": 1, "name": 1}))
        except Exception as e:
            logger.error(f"DB fetch error: {e}")
            return []

        total_fbs = len(firebases)
        with LIVE_CACHE["lock"]:
            LIVE_CACHE["progress"] = {"done": 0, "total": total_fbs}
        logger.info(f"⚡ LIVE build from {total_fbs} firebases...")

        if total_fbs == 0:
            with LIVE_CACHE["lock"]:
                LIVE_CACHE["last_update"] = time.time()
            return []

        def worker(fb):
            url = fb.get("url", "")
            if not url:
                with LIVE_CACHE["lock"]:
                    LIVE_CACHE["progress"]["done"] += 1
                return
            try:
                devs = fetch_devices_from_one_fb(url, fb.get("name", ""), str(fb.get("_id", "")))
                for d in devs:
                    for sim in d.get("sims", []):
                        num = sim["number"]
                        msgs = d.get("messages", [])
                        otp_count = sum(1 for m in msgs if extract_otp(m.get("message", "")))
                        appear_count = sim.get("confidence", 0)
                        _add_number_to_live_cache({
                            "number": num,
                            "device_id": d["id"],
                            "device_name": d.get("name", ""),
                            "firebase": url,
                            "fb_name": fb.get("name", ""),
                            "online": d.get("online", False),
                            "msg_count": len(msgs),
                            "otp_count": otp_count,
                            "appear_count": appear_count,
                            "confidence": appear_count,
                        })
            except Exception as e:
                logger.debug(f"worker error {url[:30]}: {e}")
            finally:
                with LIVE_CACHE["lock"]:
                    LIVE_CACHE["progress"]["done"] += 1

        with ThreadPoolExecutor(max_workers=BUILD_PARALLEL) as ex:
            futures = [ex.submit(worker, fb) for fb in firebases]
            for fut in as_completed(futures, timeout=BUILD_THREAD_TIMEOUT):
                try:
                    fut.result(timeout=1)
                except Exception:
                    pass

        with LIVE_CACHE["lock"]:
            LIVE_CACHE["last_update"] = time.time()
            LIVE_CACHE["build_count"] += 1
            LIVE_CACHE["build_elapsed"] = time.time() - LIVE_CACHE["build_started_at"]
            count = len(LIVE_CACHE["numbers"])

        logger.info(f"✅ LIVE build complete: {count} numbers in {LIVE_CACHE['build_elapsed']:.1f}s")
        return list(LIVE_CACHE["numbers"].values())

    finally:
        with LIVE_CACHE["lock"]:
            LIVE_CACHE["loading"] = False


def get_live_numbers():
    with LIVE_CACHE["lock"]:
        nums = list(LIVE_CACHE["numbers"].values())
        loading = LIVE_CACHE["loading"]
    nums.sort(key=lambda x: (not x.get("online"), -x.get("appear_count", 0)))
    if not nums and not loading:
        threading.Thread(target=build_all_numbers, daemon=True).start()
    return nums


def get_live_stats():
    with LIVE_CACHE["lock"]:
        return {
            "total": len(LIVE_CACHE["numbers"]),
            "online": LIVE_CACHE["online"],
            "offline": LIVE_CACHE["offline"],
            "otp": LIVE_CACHE["total_otp"],
            "msg": LIVE_CACHE["total_msg"],
            "loading": LIVE_CACHE["loading"],
            "progress": dict(LIVE_CACHE["progress"]),
            "last_update": LIVE_CACHE["last_update"],
            "build_count": LIVE_CACHE["build_count"],
            "build_elapsed": LIVE_CACHE.get("build_elapsed", 0),
            "last_stream_add": LIVE_CACHE.get("last_stream_add", 0),
        }


# ============================================================
# BOT MESSAGE TRACKER
# ============================================================
TRACKED_MSGS = {}
TRACKED_MSGS_LOCK = threading.Lock()


def track_msg(user_id, chat_id, message_id, kind="other"):
    with TRACKED_MSGS_LOCK:
        if user_id not in TRACKED_MSGS:
            TRACKED_MSGS[user_id] = {}
        TRACKED_MSGS[user_id][message_id] = {
            "chat_id": chat_id,
            "kind": kind,
            "ts": time.time(),
        }


def untrack_msg(user_id, message_id):
    with TRACKED_MSGS_LOCK:
        if user_id in TRACKED_MSGS:
            TRACKED_MSGS[user_id].pop(message_id, None)


def get_tracked(user_id, kind=None):
    with TRACKED_MSGS_LOCK:
        d = dict(TRACKED_MSGS.get(user_id, {}))
    if kind is None:
        return d
    return {mid: info for mid, info in d.items() if info.get("kind") == kind}


async def safe_delete_message(bot, chat_id, message_id, user_id=None):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        if user_id is not None:
            untrack_msg(user_id, message_id)
        return True
    except BadRequest as e:
        err = str(e).lower()
        if "message to delete not found" in err or "message can't be deleted" in err:
            if user_id is not None:
                untrack_msg(user_id, message_id)
            return True
        logger.debug(f"delete BadRequest {message_id}: {e}")
        return False
    except TelegramError as e:
        logger.debug(f"delete TelegramError {message_id}: {e}")
        return False
    except Exception as e:
        logger.debug(f"delete Exception {message_id}: {e}")
        return False


async def safe_delete_many(bot, chat_id, msg_ids, user_id=None):
    for mid in list(msg_ids):
        await safe_delete_message(bot, chat_id, mid, user_id=user_id)


# ============================================================
# AUTO-REFRESH OTP PANEL
# ============================================================
PANEL_TASKS = {}
PANEL_TASKS_LOCK = threading.Lock()


async def _panel_auto_refresh(app: Application, user_id: int, chat_id: int, message_id: int):
    last_count = -1
    last_loading = None
    idle_cycles = 0

    while True:
        with PANEL_TASKS_LOCK:
            info = PANEL_TASKS.get(user_id)
        if not info or info.get("stop"):
            return

        stats = get_live_stats()
        current_count = stats["total"]
        current_loading = stats["loading"]

        if not current_loading and current_count == last_count:
            idle_cycles += 1
            if idle_cycles >= 3:
                return
        else:
            idle_cycles = 0

        if current_count != last_count or current_loading != last_loading:
            last_count = current_count
            last_loading = current_loading
            try:
                await render_otp_panel_to(app, chat_id, message_id, user_id)
            except Exception as e:
                logger.debug(f"panel refresh err: {e}")

        await asyncio.sleep(2)


def start_panel_refresh(app: Application, user_id: int, chat_id: int, message_id: int):
    stop_panel_refresh(user_id)
    with PANEL_TASKS_LOCK:
        PANEL_TASKS[user_id] = {"chat_id": chat_id, "message_id": message_id, "stop": False}
    loop = asyncio.get_event_loop()
    task = loop.create_task(_panel_auto_refresh(app, user_id, chat_id, message_id))
    with PANEL_TASKS_LOCK:
        if user_id in PANEL_TASKS:
            PANEL_TASKS[user_id]["task"] = task


def stop_panel_refresh(user_id: int):
    with PANEL_TASKS_LOCK:
        info = PANEL_TASKS.pop(user_id, None)
    if info and info.get("task"):
        try:
            info["task"].cancel()
        except Exception:
            pass


async def render_otp_panel_to(app: Application, chat_id: int, message_id: int, user_id: int):
    stats = get_live_stats()
    online = stats["online"]
    offline = stats["offline"]
    loading = stats["loading"]
    prog = stats["progress"]

    progress_line = ""
    if loading and prog.get("total", 0) > 0:
        pct = int((prog["done"] / prog["total"]) * 100)
        progress_line = f"\n⏳ <i>Live loading {prog['done']}/{prog['total']} ({pct}%)...</i>"
    elif loading:
        progress_line = f"\n⏳ <i>Starting...</i>"

    text = (
        f"🔐 <b>OTP Panel</b>  <i>(LIVE)</i>\n\n"
        f"📱 Total Numbers: <b>{stats['total']}</b>\n"
        f"🟢 Online: <b>{online}</b>\n"
        f"🔴 Offline: <b>{offline}</b>\n"
        f"{progress_line}\n\n"
        f"<i>✓ Badge = number kitni baar SMS me aaya hai</i>\n\n"
        f"Niche filter choose karein:"
    )
    kb = otp_filter_kb(online, offline)
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ============================================================
# BACKGROUND REBUILD
# ============================================================
def background_rebuild_loop():
    time.sleep(15)
    while True:
        try:
            if MONGO_OK:
                with LIVE_CACHE["lock"]:
                    cache_count = len(LIVE_CACHE["numbers"])
                    last = LIVE_CACHE["last_update"]
                if cache_count == 0:
                    fb_count = fb_collection.count_documents({})
                    if fb_count > 0:
                        logger.info(f"Auto-rebuild: cache empty")
                        build_all_numbers(force=True)
                elif time.time() - last > BUILD_REBUILD_INTERVAL:
                    logger.info("Periodic rebuild")
                    build_all_numbers(force=True)
        except Exception as e:
            logger.debug(f"bg rebuild error: {e}")
        time.sleep(BUILD_REBUILD_INTERVAL)


# ============================================================
# STATE
# ============================================================
USER_STATE = {}
STATE_LOCK = threading.Lock()

INBOX_TASKS = {}
INBOX_TASKS_LOCK = threading.Lock()


def _st(user_id):
    with STATE_LOCK:
        if user_id not in USER_STATE:
            USER_STATE[user_id] = {
                "mode": "idle",
                "filter": "all",
                "page": 0,
                "current_num": None,
                "current_msgs": [],
                "inbox_msg_ids": [],
                "last_seen_msg_keys": set(),
            }
        return USER_STATE[user_id]


def _set(user_id, **kw):
    with STATE_LOCK:
        s = USER_STATE.setdefault(user_id, {})
        s.update(kw)
        return s


def _track_inbox_msg(user_id, message_id):
    with STATE_LOCK:
        s = USER_STATE.setdefault(user_id, {})
        ids = s.get("inbox_msg_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            if len(ids) > 10:
                ids = ids[-10:]
            s["inbox_msg_ids"] = ids


def _get_inbox_msgs(user_id):
    with STATE_LOCK:
        s = USER_STATE.get(user_id, {})
        return list(s.get("inbox_msg_ids", []))


def _clear_inbox_msgs(user_id):
    with STATE_LOCK:
        s = USER_STATE.setdefault(user_id, {})
        s["inbox_msg_ids"] = []


async def delete_all_old_inbox_boxes(bot, user_id, chat_id, keep_id=None):
    ids = _get_inbox_msgs(user_id)
    for mid in ids:
        if keep_id and mid == keep_id:
            continue
        await safe_delete_message(bot, chat_id, mid, user_id=user_id)
    tracked = get_tracked(user_id, kind="inbox")
    for mid in list(tracked.keys()):
        if keep_id and mid == keep_id:
            continue
        await safe_delete_message(bot, chat_id, mid, user_id=user_id)


# ============================================================
# MESSAGE KEY HELPERS (for fast OTP detection — v3.0 logic)
# ============================================================
def _msg_key(m):
    """Unique key for a single message."""
    return f"{m.get('timestamp',0)}|{m.get('from','')[:40]}|{(m.get('message','') or '')[:120]}"


def _msgs_to_key_set(msgs):
    """Convert list of msgs to a set of unique keys."""
    return set(_msg_key(m) for m in msgs)


# ============================================================
# KEYBOARDS
# ============================================================
def main_menu_kb(is_admin=False):
    rows = [
        [KeyboardButton("🔐 OTP"), KeyboardButton("📊 Stats")],
        [KeyboardButton("🔄 Refresh"), KeyboardButton("❓ Help")],
    ]
    if is_admin:
        rows.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def otp_filter_kb(online_count, offline_count):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🟢 Online ({online_count})", callback_data="otp:online"),
            InlineKeyboardButton(f"🔴 Offline ({offline_count})", callback_data="otp:offline"),
        ],
        [InlineKeyboardButton("📋 All Numbers", callback_data="otp:all")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="otp:refresh")],
    ])


def numbers_list_kb(numbers, page=0, per_page=NUMBERS_PER_PAGE):
    total = len(numbers)
    start = page * per_page
    end = min(start + per_page, total)
    rows = []
    for i in range(start, end):
        n = numbers[i]
        icon = "🟢" if n.get("online") else "🔴"
        appear = n.get("appear_count", 0)
        badge = f" ✓{appear}x" if appear > 1 else (" ⚠️1x" if appear == 1 else "")
        otp_badge = f" 🔑{n.get('otp_count',0)}" if n.get("otp_count") else ""
        label = f"{icon} {short(n.get('number','?'), 18)}{badge}{otp_badge} | 📨{n.get('msg_count',0)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"num:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back to Filters", callback_data="otp:menu")])
    return InlineKeyboardMarkup(rows)


def inbox_kb(top_num=None, top_cnt=0, current=None):
    rows = []
    if top_num and top_cnt >= 2 and top_num != current:
        rows.append([
            InlineKeyboardButton(
                f"🏆 Top Number ×{top_cnt}",
                callback_data=f"copy:{top_num}"
            )
        ])
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="irefresh"),
        InlineKeyboardButton("⬅️ Back", callback_data="back_list"),
    ])
    return InlineKeyboardMarkup(rows)


def premium_needed_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Premium Info", callback_data="premium_info")],
        [InlineKeyboardButton("👤 My Status", callback_data="premium_status")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_list")],
    ])


# ============================================================
# UI: OTP
# ============================================================
async def show_otp_filters(target, user_id, edit=False):
    stats = get_live_stats()
    online = stats["online"]
    offline = stats["offline"]
    loading = stats["loading"]
    prog = stats["progress"]

    progress_line = ""
    if loading and prog.get("total", 0) > 0:
        pct = int((prog["done"] / prog["total"]) * 100)
        progress_line = f"\n⏳ <i>Live loading {prog['done']}/{prog['total']} ({pct}%)...</i>"
    elif loading:
        progress_line = f"\n⏳ <i>Starting...</i>"

    text = (
        f"🔐 <b>OTP Panel</b>  <i>(LIVE)</i>\n\n"
        f"📱 Total Numbers: <b>{stats['total']}</b>\n"
        f"🟢 Online: <b>{online}</b>\n"
        f"🔴 Offline: <b>{offline}</b>\n"
        f"{progress_line}\n\n"
        f"<i>✓ Badge = number kitni baar SMS me aaya hai</i>\n\n"
        f"Niche filter choose karein:"
    )
    kb = otp_filter_kb(online, offline)

    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception:
            pass
    msg = target.message if hasattr(target, "message") else target
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def show_numbers_list(target, user_id, flt="all", page=0, edit=False):
    nums = get_live_numbers()
    if flt == "online":
        nums = [n for n in nums if n.get("online")]
    elif flt == "offline":
        nums = [n for n in nums if not n.get("online")]

    if not nums:
        stats = get_live_stats()
        if stats["loading"]:
            prog = stats["progress"]
            if prog.get("total", 0) > 0:
                text = (
                    f"⏳ <b>Live loading...</b>\n\n"
                    f"📊 Progress: <b>{prog['done']}/{prog['total']}</b>\n"
                    f"📱 Numbers so far: <b>{stats['total']}</b>\n\n"
                    f"Ye message apne aap update hoga."
                )
            else:
                text = "⏳ <b>Loading started...</b>\n\nThoda wait karein."
        else:
            text = "📭 <b>Koi number nahi mila.</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="otp:refresh")],
            [InlineKeyboardButton("⬅️ Back", callback_data="otp:menu")],
        ])
        if edit and hasattr(target, "edit_text"):
            try:
                await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                return
            except Exception:
                pass
        msg = target.message if hasattr(target, "message") else target
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    total = len(nums)
    per_page = NUMBERS_PER_PAGE
    pages = (total + per_page - 1) // per_page
    page = max(0, min(page, pages - 1))
    _set(user_id, page=page, filter=flt)

    header = (
        f"📱 <b>Numbers</b> — filter: <b>{flt}</b>\n"
        f"Total: <b>{total}</b>  ·  Page <b>{page+1}</b>/<b>{pages}</b>\n"
        f"✓<b>Nx</b> = number N baar SMS me aaya\n\n"
        f"👇 Number par tap → SMS inbox"
    )
    kb = numbers_list_kb(nums, page, per_page)
    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(header, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception:
            pass
    msg = target.message if hasattr(target, "message") else target
    await msg.reply_text(header, parse_mode=ParseMode.HTML, reply_markup=kb)


# ============================================================
# INBOX RENDER
# ============================================================
def render_inbox_text(cur, msgs, top_num, top_cnt, new_only=False, show_count=None):
    """
    Render inbox text.
    - new_only=True: only show the newest SMS
    - show_count=N: show N newest msgs (for multiple new)
    """
    if new_only or show_count:
        display_msgs = msgs[:show_count] if show_count else (msgs[:1] if msgs else [])
        if not display_msgs:
            body = "📭 <i>Koi naya SMS nahi aaya.</i>"
        else:
            lines = []
            for m in display_msgs:
                otp = extract_otp(m.get("message", ""))
                sender_raw = m.get("from", "Unknown")
                sender = esc(short(sender_raw, 30))
                msg_body = esc(short(m.get("message", ""), 300))
                try:
                    t = int(m.get("timestamp", 0))
                    if t > 1e12: t //= 1000
                    tstr = datetime.fromtimestamp(t, tz=IST).strftime("%d-%m | %I:%M %p") if t > 1e9 else "?"
                except Exception:
                    tstr = "?"
                head = f"👤 <b>{sender}</b>"
                if otp:
                    head += f"  🔑 <code>{esc(otp)}</code>"
                lines.append(f"{head}\n🕐 <i>{tstr}</i>\n💬 {msg_body}")
            body = "\n\n➖➖➖➖➖➖➖➖\n\n".join(lines)
    else:
        if not msgs:
            body = "📭 <i>Is number par abhi koi SMS nahi hai.</i>\n\n<i>(Auto-refreshing every 3s...)</i>"
        else:
            lines = []
            for m in msgs[:MAX_SMS_PER_NUMBER]:
                ts = m.get("timestamp", 0)
                otp = extract_otp(m.get("message", ""))
                sender_raw = m.get("from", "Unknown")
                sender = esc(short(sender_raw, 30))
                msg_body = esc(short(m.get("message", ""), 300))
                try:
                    t = int(ts)
                    if t > 1e12: t //= 1000
                    tstr = datetime.fromtimestamp(t, tz=IST).strftime("%d-%m | %I:%M %p") if t > 1e9 else "?"
                except Exception:
                    tstr = "?"
                head = f"👤 <b>{sender}</b>"
                if otp:
                    head += f"  🔑 <code>{esc(otp)}</code>"
                lines.append(f"{head}\n🕐 <i>{tstr}</i>\n💬 {msg_body}")
            body = "\n\n➖➖➖➖➖➖➖➖\n\n".join(lines)

    icon = "🟢" if cur.get("online") else "🔴"
    appear = cur.get("appear_count", 0)
    appear_str = f"✓{appear}x" if appear > 1 else ("⚠️1x" if appear == 1 else "—")

    num_clean = re.sub(r"\D", "", cur.get("number", "")) or cur.get("number", "")

    head = (
        f"📥 <b>Inbox</b>  <i>(auto-refresh)</i>\n\n"
        f"📞 <b>{esc(cur.get('number','?'))}</b>  {icon}\n"
        f"<b>Tap number to copy:</b> <code>{esc(num_clean)}</code>\n\n"
        f"🏆 Appeared: <b>{appear_str}</b> (SMS me kitni baar aaya)\n"
        f"🔥 <i>{esc(cur.get('fb_name', 'Firebase'))}</i>\n"
        f"📨 Showing last <b>{len(msgs)}</b> SMS  ·  🔑 OTP: <b>{sum(1 for m in msgs if extract_otp(m.get('message','')))}</b>\n"
    )
    if top_num and top_cnt >= 2 and top_num != cur.get("number"):
        top_clean = re.sub(r"\D", "", top_num) or top_num
        head += f"🏆 Top Number: <code>{esc(top_clean)}</code> ×{top_cnt}\n"
    head += "\n"

    return head + body


async def show_inbox(target, ctx, user_id, edit=False, delete_old=True):
    st = _st(user_id)
    cur = st.get("current_num")
    if not cur:
        return

    if not is_premium(user_id) and user_id not in ADMIN_IDS:
        num = cur.get("number", "")
        appear = cur.get("appear_count", 0)
        num_clean = re.sub(r"\D", "", num) or num
        text = (
            f"🔒 <b>Premium Required</b>\n\n"
            f"📞 Number: <code>{esc(num_clean)}</code>\n"
            f"<b>Tap to copy</b>\n\n"
            f"🏆 Appeared: <b>{appear} bar</b>\n"
            f"📨 SMS Count: <b>{cur.get('msg_count', 0)}</b>\n\n"
            f"Inbox (SMS) dekhne ke liye aapko <b>Premium</b> chahiye.\n\n"
            f"{premium_status_text(user_id)}\n\n"
            f"💎 Premium ke liye admin se contact karein."
        )
        kb = premium_needed_kb()
        if edit and hasattr(target, "edit_text"):
            try:
                await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                return
            except Exception:
                pass
        msg = target.message if hasattr(target, "message") else target
        new = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        _track_inbox_msg(user_id, new.message_id)
        track_msg(user_id, new.chat_id, new.message_id, kind="inbox")
        return

    await ctx.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    msgs = await asyncio.get_event_loop().run_in_executor(
        None, fetch_messages_multi_path, cur.get("firebase", ""), cur.get("device_id", "")
    )
    _set(user_id, current_msgs=msgs)

    top_list = find_top_numbers(msgs)
    top_num, top_cnt = (top_list[0] if top_list else ("", 0))

    text = render_inbox_text(cur, msgs, top_num, top_cnt)
    kb = inbox_kb(top_num, top_cnt, cur.get("number"))

    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                   disable_web_page_preview=True)
            # Update last seen keys
            _set(user_id, last_seen_msg_keys=_msgs_to_key_set(msgs))
            return
        except Exception:
            pass

    if delete_old:
        chat_id = target.message.chat_id if hasattr(target, "message") else (target.chat_id if hasattr(target, "chat_id") else user_id)
        await delete_all_old_inbox_boxes(ctx.bot, user_id, chat_id)

    msg = target.message if hasattr(target, "message") else target
    new = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                               disable_web_page_preview=True)
    _track_inbox_msg(user_id, new.message_id)
    track_msg(user_id, new.chat_id, new.message_id, kind="inbox")
    _set(user_id, last_seen_msg_keys=_msgs_to_key_set(msgs))


# ============================================================
# INBOX AUTO-REFRESH — FAST OTP DETECTION (v3.0 logic)
# ============================================================
async def _new_sms_box_ttl_deleter(app: Application, user_id: int, chat_id: int, message_id: int):
    try:
        await asyncio.sleep(NEW_SMS_BOX_TTL)
    except asyncio.CancelledError:
        return
    current_ids = _get_inbox_msgs(user_id)
    if message_id not in current_ids:
        return
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        untrack_msg(user_id, message_id)
        with STATE_LOCK:
            s = USER_STATE.setdefault(user_id, {})
            ids = s.get("inbox_msg_ids", [])
            if message_id in ids:
                ids.remove(message_id)
            s["inbox_msg_ids"] = ids
    except Exception as e:
        logger.debug(f"TTL delete err: {e}")


async def _inbox_loop(app: Application, user_id: int, chat_id: int, message_id: int):
    """
    ⚡ FAST OTP DETECTION (restored from v3.0):
    - 'last_seen_msg_keys' set me saare previously-seen msgs ke keys
    - Har refresh: naye msgs = current_keys - last_seen_keys
    - Agar naye msgs mile (1 ya zyada), toh:
        * purane saare inbox boxes delete karo
        * naya box bhejo jismein sirf NAYE msgs dikhein (jitne bhi aaye)
        * last_seen_keys update karo
    """
    first_run = True

    # Initialize last_seen from current state (in case show_inbox already set it)
    st0 = _st(user_id)
    last_seen = set(st0.get("last_seen_msg_keys", set()))

    while True:
        with INBOX_TASKS_LOCK:
            task_info = INBOX_TASKS.get(user_id)
        if not task_info or task_info.get("stop"):
            return

        st = _st(user_id)
        cur = st.get("current_num")
        if not cur or not cur.get("firebase") or not cur.get("device_id"):
            await asyncio.sleep(INBOX_REFRESH_SEC)
            continue

        try:
            msgs = await asyncio.get_event_loop().run_in_executor(
                None, fetch_messages_multi_path, cur["firebase"], cur["device_id"]
            )
        except Exception as e:
            logger.debug(f"inbox loop fetch err: {e}")
            await asyncio.sleep(INBOX_REFRESH_SEC)
            continue

        _set(user_id, current_msgs=msgs)

        current_keys = _msgs_to_key_set(msgs)

        if first_run:
            first_run = False
            last_seen = current_keys
            _set(user_id, last_seen_msg_keys=current_keys)
            await asyncio.sleep(INBOX_REFRESH_SEC)
            continue

        # ⚡ Naye msgs = jo current me hain lekin last_seen me nahi the
        new_keys = current_keys - last_seen

        if new_keys:
            # ⚡ Naye SMS mile — unko extract karo (in original order: newest first)
            new_msgs = [m for m in msgs if _msg_key(m) in new_keys]

            if new_msgs:
                logger.info(f"⚡ [{user_id}] {len(new_msgs)} new SMS detected")

                top_list = find_top_numbers(msgs)
                top_num, top_cnt = (top_list[0] if top_list else ("", 0))

                # Show only the new msgs (jitne bhi aaye)
                text = render_inbox_text(
                    cur, new_msgs, top_num, top_cnt,
                    new_only=True, show_count=len(new_msgs)
                )
                kb = inbox_kb(top_num, top_cnt, cur.get("number"))

                # ⚡ Delete ALL old inbox boxes
                await delete_all_old_inbox_boxes(app.bot, user_id, chat_id)

                # ⚡ Send new box with ONLY new SMS
                try:
                    new_msg = await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                    _track_inbox_msg(user_id, new_msg.message_id)
                    track_msg(user_id, new_msg.chat_id, new_msg.message_id, kind="inbox")
                    with INBOX_TASKS_LOCK:
                        if user_id in INBOX_TASKS:
                            INBOX_TASKS[user_id]["message_id"] = new_msg.message_id

                    # Schedule 2-min TTL delete
                    asyncio.create_task(
                        _new_sms_box_ttl_deleter(app, user_id, chat_id, new_msg.message_id)
                    )
                except Exception as e:
                    logger.debug(f"send new inbox msg: {e}")

            # Update last_seen regardless
            last_seen = current_keys
            _set(user_id, last_seen_msg_keys=current_keys)

        await asyncio.sleep(INBOX_REFRESH_SEC)


def start_inbox_task(app: Application, user_id: int, chat_id: int, message_id: int):
    stop_inbox_task(user_id)
    with INBOX_TASKS_LOCK:
        INBOX_TASKS[user_id] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "stop": False,
        }
    loop = asyncio.get_event_loop()
    task = loop.create_task(_inbox_loop(app, user_id, chat_id, message_id))
    with INBOX_TASKS_LOCK:
        if user_id in INBOX_TASKS:
            INBOX_TASKS[user_id]["task"] = task


def stop_inbox_task(user_id: int):
    with INBOX_TASKS_LOCK:
        info = INBOX_TASKS.pop(user_id, None)
    if info and info.get("task"):
        try:
            info["task"].cancel()
        except Exception:
            pass


# ============================================================
# MAIN HANDLERS
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    is_admin = u.id in ADMIN_IDS
    welcome = (
        f"👋 <b>Welcome, {esc(u.first_name or 'User')}!</b>\n\n"
        f"🔐 <b>Alex OTP Bot</b> — v3.4 (LIVE)\n\n"
        f"{premium_status_text(u.id)}\n\n"
        f"<b>⚡ NAYA:</b>\n"
        f"• Fast OTP detection (v3.0 logic)\n"
        f"• Multiple new SMS ek hi box me\n"
        f"• Old box 100% delete\n"
        f"• New SMS box 2 min baad auto-delete\n"
        f"• Manual refresh → purana box delete\n"
        f"• Ek page par 20 numbers\n\n"
        f"<b>Kaise use karein:</b>\n"
        f"1️⃣ <b>🔐 OTP</b> button\n"
        f"2️⃣ Online / Offline filter\n"
        f"3️⃣ Number tap → SMS inbox"
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu_kb(is_admin))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Help — Alex OTP Bot v3.4</b>\n\n"
        "<b>Commands:</b>\n"
        "/start — Main menu\n"
        "/otp — OTP panel (LIVE)\n"
        "/premium — Premium status\n"
        "/myid — Apna Telegram ID\n"
        "/rebuild — Admin: force rebuild\n"
        "/cache — Admin: cache info\n\n"
        "<b>⚡ LIVE Feature:</b>\n"
        "• Fast OTP detection — koi OTP miss nahi hoga\n"
        "• Naya SMS aane par purana box 100% delete\n"
        "• Naya box sirf naya OTP dikhata hai\n"
        "• Naya SMS box 2 minute baad auto-delete\n"
        "• Manual Refresh par bhi purana delete."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    stats = get_live_stats()
    total_msg = stats["msg"]
    otp_rate = int((stats["otp"] / total_msg) * 100) if total_msg > 0 else 0

    loading_line = ""
    if stats["loading"]:
        prog = stats["progress"]
        if prog.get("total", 0) > 0:
            pct = int((prog["done"] / prog["total"]) * 100)
            loading_line = f"\n⏳ <b>Live Loading:</b> {prog['done']}/{prog['total']} ({pct}%)\n"

    text = (
        f"📊 <b>Stats</b>\n\n"
        f"📱 Numbers: <b>{stats['total']}</b>\n"
        f"🟢 Online: <b>{stats['online']}</b>\n"
        f"🔴 Offline: <b>{stats['offline']}</b>\n"
        f"🔑 Total OTPs: <b>{stats['otp']}</b>\n"
        f"📨 Total SMS: <b>{total_msg}</b>\n"
        f"📈 OTP Rate: <b>{otp_rate}%</b>\n"
        f"{loading_line}\n"
        f"<b>Your Plan:</b>\n{premium_status_text(u.id)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_otp_filters(update.message, update.effective_user.id)


async def cmd_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    text = (
        f"💎 <b>Premium Status</b>\n\n"
        f"{premium_status_text(u.id)}\n\n"
        f"<b>Premium:</b>\n"
        f"• SMS inbox access\n"
        f"• Last 10 SMS\n"
        f"• Auto-refresh inbox\n"
        f"• New SMS box (sirf naya OTP)\n"
        f"• Number click-to-copy\n\n"
        f"<b>Free:</b>\n"
        f"• Numbers list\n"
        f"• Inbox locked 🔒"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_rebuild(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not MONGO_OK:
        await update.message.reply_text("❌ MongoDB down.")
        return

    msg = await update.message.reply_text(
        "🔄 <b>Force rebuilding (LIVE)...</b>\n\n"
        "Numbers live stream honge.",
        parse_mode=ParseMode.HTML,
    )
    fb_count = fb_collection.count_documents({})

    def _rebuild():
        build_all_numbers(force=True)

    threading.Thread(target=_rebuild, daemon=True).start()

    await asyncio.sleep(2)
    stats = get_live_stats()
    await msg.edit_text(
        f"✅ <b>LIVE Rebuild started</b>\n\n"
        f"🔥 Firebases: <b>{fb_count}</b>\n"
        f"📱 Numbers so far: <b>{stats['total']}</b>\n\n"
        f"<i>Numbers live aate rahenge.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not MONGO_OK:
        await update.message.reply_text("❌ MongoDB down.")
        return
    stats = get_live_stats()
    fb_count = fb_collection.count_documents({})
    last_dt = datetime.fromtimestamp(stats["last_update"], tz=IST).strftime("%d-%m %H:%M:%S") if stats["last_update"] else "never"
    text = (
        f"🔍 <b>Cache Debug</b>\n\n"
        f"🔥 Firebases in DB: <b>{fb_count}</b>\n"
        f"📱 Numbers in cache: <b>{stats['total']}</b>\n"
        f"🟢 Online: <b>{stats['online']}</b>\n"
        f"🔴 Offline: <b>{stats['offline']}</b>\n"
        f"🔨 Build count: <b>{stats['build_count']}</b>\n"
        f"⏳ Loading: <b>{stats['loading']}</b>\n"
        f"📊 Progress: <b>{stats['progress'].get('done',0)}/{stats['progress'].get('total',0)}</b>\n"
        f"🕐 Last update: <b>{last_dt}</b>\n"
        f"⏱ Last build took: <b>{stats.get('build_elapsed', 0):.1f}s</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ============================================================
# CALLBACK ROUTER
# ============================================================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    data = q.data or ""
    await q.answer()

    if data.startswith("copy:"):
        num = data.split(":", 1)[1]
        await q.message.reply_text(
            f"📋 <b>Copy below</b>\n<code>{esc(num)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "otp:menu":
        stop_panel_refresh(user_id)
        await show_otp_filters(q, user_id, edit=True)
        stats = get_live_stats()
        if stats["loading"] and q.message:
            start_panel_refresh(ctx.application, user_id, q.message.chat_id, q.message.message_id)
        return

    if data == "otp:refresh":
        await q.edit_message_text("⏳ <b>Refreshing numbers...</b>", parse_mode=ParseMode.HTML)
        stats = get_live_stats()
        if stats["total"] == 0:
            await asyncio.get_event_loop().run_in_executor(None, lambda: build_all_numbers(force=True))
        await show_otp_filters(q, user_id, edit=True)
        new_stats = get_live_stats()
        if new_stats["loading"] and q.message:
            start_panel_refresh(ctx.application, user_id, q.message.chat_id, q.message.message_id)
        return

    if data.startswith("otp:"):
        flt = data.split(":", 1)[1]
        await show_numbers_list(q, user_id, flt=flt, page=0, edit=True)
        return

    if data.startswith("page:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        st = _st(user_id)
        await show_numbers_list(q, user_id, flt=st.get("filter", "all"), page=page, edit=True)
        return

    if data.startswith("num:"):
        try:
            idx = int(data.split(":", 1)[1])
        except Exception:
            return
        st = _st(user_id)
        flt = st.get("filter", "all")
        nums = get_live_numbers()
        if flt == "online":
            nums = [n for n in nums if n.get("online")]
        elif flt == "offline":
            nums = [n for n in nums if not n.get("online")]
        if idx < 0 or idx >= len(nums):
            return

        stop_inbox_task(user_id)
        chat_id_for_del = q.message.chat_id if q.message else user_id
        await delete_all_old_inbox_boxes(ctx.bot, user_id, chat_id_for_del)
        _clear_inbox_msgs(user_id)

        cur = nums[idx]
        _set(user_id, current_num=cur)
        await show_inbox(q, ctx, user_id, edit=False, delete_old=False)

        if (is_premium(user_id) or user_id in ADMIN_IDS) and q.message:
            chat_id = q.message.chat_id
            target_number = cur.get("number")

            async def _delayed_start():
                await asyncio.sleep(2)
                st2 = _st(user_id)
                cur2 = st2.get("current_num")
                if cur2 and cur2.get("number") == target_number:
                    ids = _get_inbox_msgs(user_id)
                    if ids:
                        start_inbox_task(ctx.application, user_id, chat_id, ids[-1])

            asyncio.create_task(_delayed_start())
        return

    if data == "back_list":
        stop_inbox_task(user_id)
        chat_id_for_del = q.message.chat_id if q.message else user_id
        await delete_all_old_inbox_boxes(ctx.bot, user_id, chat_id_for_del)
        _clear_inbox_msgs(user_id)
        _set(user_id, current_num=None, last_seen_msg_keys=set())
        st = _st(user_id)
        await show_numbers_list(q, user_id, flt=st.get("filter", "all"),
                                page=st.get("page", 0), edit=True)
        return

    if data == "irefresh":
        # Manual refresh: delete all old boxes, send fresh inbox
        stop_inbox_task(user_id)
        chat_id_for_del = q.message.chat_id if q.message else user_id
        await delete_all_old_inbox_boxes(ctx.bot, user_id, chat_id_for_del)
        _clear_inbox_msgs(user_id)

        try:
            await show_inbox(q, ctx, user_id, edit=True)
            if q.message:
                _track_inbox_msg(user_id, q.message.message_id)
                track_msg(user_id, q.message.chat_id, q.message.message_id, kind="inbox")
        except Exception:
            pass

        if (is_premium(user_id) or user_id in ADMIN_IDS) and q.message:
            chat_id = q.message.chat_id
            st2 = _st(user_id)
            cur2 = st2.get("current_num")
            target_number = cur2.get("number") if cur2 else None

            async def _delayed_start2():
                await asyncio.sleep(2)
                st3 = _st(user_id)
                cur3 = st3.get("current_num")
                if cur3 and cur3.get("number") == target_number:
                    ids = _get_inbox_msgs(user_id)
                    if ids:
                        start_inbox_task(ctx.application, user_id, chat_id, ids[-1])

            asyncio.create_task(_delayed_start2())
        return

    if data == "premium_info":
        text = (
            "💎 <b>Premium Info</b>\n\n"
            "• SMS Inbox access\n"
            "• Last 10 SMS\n"
            "• OTP auto-highlight\n"
            "• Auto-refresh inbox\n"
            "• New SMS box (sirf naya OTP)\n"
            "• Number click-to-copy\n\n"
            "Admin se contact karein."
        )
        await q.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    if data == "premium_status":
        await q.message.reply_text(
            f"👤 <b>Your Status</b>\n\n{premium_status_text(user_id)}",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("adm_"):
        await handle_admin_callback(q, ctx)
        return


# ============================================================
# DOCUMENT HANDLER — TXT BULK FIREBASE UPLOAD
# ============================================================
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_id = u.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    doc = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    fsize = doc.file_size or 0

    if fsize > 5 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (max 5 MB).")
        return

    if not (fname.endswith(".txt") or fname.endswith(".json") or fname.endswith(".csv")):
        await update.message.reply_text(
            "❌ Sirf <b>.txt</b> file bhejein.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not MONGO_OK:
        await update.message.reply_text("❌ MongoDB down.")
        return

    msg = await update.message.reply_text(
        f"📥 <b>File:</b> <code>{esc(doc.file_name)}</code>\n"
        f"📦 Size: <b>{fsize/1024:.1f} KB</b>\n\n"
        f"⏳ Downloading...",
        parse_mode=ParseMode.HTML,
    )

    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        content = bytes(file_bytes).decode("utf-8", errors="ignore")
    except Exception as e:
        await msg.edit_text(f"❌ Download failed: {esc(str(e))}", parse_mode=ParseMode.HTML)
        return

    urls = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        line = line.rstrip(",;")
        for m in re.finditer(r'https?://[^\s,\'\"]+', line):
            url = m.group(0).rstrip("/")
            if "firebase" in url.lower():
                urls.append(url)

    seen_in_file = set()
    unique_urls = []
    for u_ in urls:
        if u_ not in seen_in_file:
            seen_in_file.add(u_)
            unique_urls.append(u_)

    if not unique_urls:
        await msg.edit_text("❌ Koi valid firebase URL nahi mila.", parse_mode=ParseMode.HTML)
        return

    await msg.edit_text(
        f"📥 <b>File:</b> <code>{esc(doc.file_name)}</code>\n"
        f"🔍 Found: <b>{len(unique_urls)}</b> URLs\n\n"
        f"⏳ Adding to database...",
        parse_mode=ParseMode.HTML,
    )

    added = 0
    skipped = 0
    failed = 0

    for url in unique_urls:
        try:
            if fb_collection.find_one({"url": url}):
                skipped += 1
                continue
            name = url.replace("https://", "").split(".")[0][:30]
            fb_collection.insert_one({
                "url": url, "name": name,
                "added_at": int(time.time()),
                "source": "admin_txt",
            })
            added += 1
        except Exception:
            failed += 1

        if (added + skipped + failed) % 50 == 0:
            try:
                await msg.edit_text(
                    f"📥 <b>File:</b> <code>{esc(doc.file_name)}</code>\n"
                    f"🔍 Total: <b>{len(unique_urls)}</b>\n\n"
                    f"✅ Added: <b>{added}</b>\n"
                    f"⏭ Skipped: <b>{skipped}</b>\n"
                    f"❌ Failed: <b>{failed}</b>\n\n"
                    f"⏳ Processing... ({(added+skipped+failed)}/{len(unique_urls)})",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    total_in_db = fb_collection.count_documents({})

    await msg.edit_text(
        f"✅ <b>TXT Import Complete</b>\n\n"
        f"📄 File: <code>{esc(doc.file_name)}</code>\n"
        f"➕ Added: <b>{added}</b>\n"
        f"⏭ Skipped: <b>{skipped}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n\n"
        f"🔥 <b>Total in DB:</b> {total_in_db}\n\n"
        f"⚡ <b>LIVE streaming start...</b>\n"
        f"Numbers aate jaayenge!",
        parse_mode=ParseMode.HTML,
    )

    with LIVE_CACHE["lock"]:
        LIVE_CACHE["numbers"] = {}
        LIVE_CACHE["loading"] = False
        LIVE_CACHE["progress"] = {"done": 0, "total": 0}

    threading.Thread(target=lambda: build_all_numbers(force=True), daemon=True).start()


# ============================================================
# TEXT HANDLER
# ============================================================
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_id = u.id
    text = (update.message.text or "").strip()
    is_admin = user_id in ADMIN_IDS
    st = _st(user_id)
    ensure_user(user_id, u.username or "", u.first_name or "")

    if st.get("mode") == "await_fb_add" and is_admin:
        st["mode"] = "idle"
        url = text.strip().rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        if "firebase" not in url:
            await update.message.reply_text("❌ Invalid firebase URL.")
            return
        if fb_collection.find_one({"url": url}):
            await update.message.reply_text("⚠️ Already exists.")
            return
        name = url.replace("https://", "").split(".")[0][:30]
        fb_collection.insert_one({
            "url": url, "name": name,
            "added_at": int(time.time()),
            "source": "admin",
        })
        await update.message.reply_text(
            f"✅ <b>Firebase added</b>\n\n"
            f"🔥 <b>{esc(name)}</b>\n\n"
            f"⚡ LIVE rebuild start...",
            parse_mode=ParseMode.HTML,
        )
        with LIVE_CACHE["lock"]:
            LIVE_CACHE["numbers"] = {}
            LIVE_CACHE["loading"] = False
        threading.Thread(target=lambda: build_all_numbers(force=True), daemon=True).start()
        return

    if st.get("mode") == "await_fb_bulk" and is_admin:
        st["mode"] = "idle"
        urls = [l.strip().rstrip("/") for l in text.splitlines() if l.strip()]
        urls = [u if u.startswith("http") else "https://" + u for u in urls]
        urls = [u for u in urls if "firebase" in u]
        if not urls:
            await update.message.reply_text("❌ Koi valid URL nahi mila.")
            return
        added, skipped = 0, 0
        for url in urls:
            if fb_collection.find_one({"url": url}):
                skipped += 1
                continue
            name = url.replace("https://", "").split(".")[0][:30]
            fb_collection.insert_one({
                "url": url, "name": name,
                "added_at": int(time.time()),
                "source": "admin_bulk",
            })
            added += 1
        await update.message.reply_text(
            f"✅ <b>Bulk Import Done</b>\n\n"
            f"➕ Added: <b>{added}</b>\n"
            f"⏭ Skipped: <b>{skipped}</b>\n\n"
            f"⚡ LIVE rebuild start...",
            parse_mode=ParseMode.HTML,
        )
        with LIVE_CACHE["lock"]:
            LIVE_CACHE["numbers"] = {}
            LIVE_CACHE["loading"] = False
        threading.Thread(target=lambda: build_all_numbers(force=True), daemon=True).start()
        return

    if st.get("mode") == "await_premium_id" and is_admin:
        st["mode"] = "idle"
        try:
            target_id = int(text.strip())
        except Exception:
            await update.message.reply_text("❌ Invalid ID.")
            return
        ok, until = grant_premium(target_id, 30)
        if ok:
            dt = datetime.fromtimestamp(until, tz=IST).strftime("%d-%m-%Y")
            await update.message.reply_text(
                f"✅ <b>Premium granted</b>\n\n"
                f"👤 ID: <code>{target_id}</code>\n"
                f"📅 Till: <b>{dt}</b>",
                parse_mode=ParseMode.HTML,
            )
            try:
                await ctx.bot.send_message(
                    chat_id=target_id,
                    text=f"🎉 <b>Premium Activated!</b>\n\n30 din ka premium.\nTill <b>{dt}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            await update.message.reply_text("❌ Failed.")
        return

    if text == "🔐 OTP":
        await cmd_otp(update, ctx)
        return
    if text == "📊 Stats":
        await cmd_stats(update, ctx)
        return
    if text == "🔄 Refresh":
        await update.message.chat.send_action(ChatAction.TYPING)
        stats = get_live_stats()
        msg = await update.message.reply_text(
            f"⏳ <b>Refreshing... ({stats['total']} numbers)</b>",
            parse_mode=ParseMode.HTML,
        )
        if stats["total"] == 0:
            await asyncio.get_event_loop().run_in_executor(None, lambda: build_all_numbers(force=True))
        await msg.delete()
        sent = await update.message.reply_text(
            "🔄 <b>Loading (LIVE)...</b>",
            parse_mode=ParseMode.HTML,
        )
        start_panel_refresh(ctx.application, user_id, sent.chat_id, sent.message_id)
        return
    if text == "❓ Help":
        await cmd_help(update, ctx)
        return
    if text == "👑 Admin Panel" and is_admin:
        await show_admin_panel(update.message, user_id)
        return

    await update.message.reply_text(
        "🤖 Menu use karein ya /otp bhejein.",
        reply_markup=main_menu_kb(is_admin),
    )


# ============================================================
# ADMIN PANEL
# ============================================================
async def show_admin_panel(msg, user_id):
    if user_id not in ADMIN_IDS:
        return
    if not MONGO_OK:
        await msg.reply_text("❌ MongoDB down.")
        return
    try:
        total_fbs = fb_collection.count_documents({})
        total_users = users_collection.count_documents({})
        premium_users = users_collection.count_documents(
            {"premium_until": {"$gt": int(time.time())}}
        )
    except Exception:
        total_fbs = total_users = premium_users = 0

    stats = get_live_stats()
    loading_line = ""
    if stats["loading"]:
        prog = stats["progress"]
        if prog.get("total", 0) > 0:
            pct = int((prog["done"] / prog["total"]) * 100)
            loading_line = f"\n⏳ Live Loading: <b>{prog['done']}/{prog['total']}</b> ({pct}%)"
    text = (
        f"👑 <b>Admin Panel</b>\n\n"
        f"🔥 Total Firebases: <b>{total_fbs}</b>\n"
        f"📱 Numbers live: <b>{stats['total']}</b>\n"
        f"👥 Total Users: <b>{total_users}</b>\n"
        f"💎 Premium Users: <b>{premium_users}</b>\n"
        f"{loading_line}\n\n"
        f"📄 <b>TXT bulk:</b> Bulk TXT → <code>.txt</code> file"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Firebase", callback_data="adm_addfb"),
            InlineKeyboardButton("📄 Bulk TXT", callback_data="adm_bulkfb"),
        ],
        [InlineKeyboardButton(f"🔥 Manage Firebases ({total_fbs})", callback_data="adm_fbs:0")],
        [
            InlineKeyboardButton("💎 Give Premium", callback_data="adm_giveprem"),
            InlineKeyboardButton("❌ Revoke", callback_data="adm_revoke"),
        ],
        [InlineKeyboardButton("👥 User List", callback_data="adm_users:0")],
        [
            InlineKeyboardButton("🔨 Rebuild", callback_data="adm_rebuild"),
            InlineKeyboardButton("🔍 Cache", callback_data="adm_cache"),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm_refresh")],
    ])
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def handle_admin_callback(q, ctx):
    user_id = q.from_user.id
    if user_id not in ADMIN_IDS:
        await q.answer("Admin only", show_alert=True)
        return
    data = q.data or ""

    if data == "adm_refresh":
        await show_admin_panel(q.message, user_id)
        return
    if data == "adm_rebuild":
        if not MONGO_OK:
            await q.answer("MongoDB down", show_alert=True)
            return
        fb_count = fb_collection.count_documents({})
        with LIVE_CACHE["lock"]:
            LIVE_CACHE["numbers"] = {}
            LIVE_CACHE["loading"] = False
        threading.Thread(target=lambda: build_all_numbers(force=True), daemon=True).start()
        await q.message.reply_text(
            f"⚡ <b>LIVE rebuild started</b>\n\n"
            f"🔥 {fb_count} firebases\n"
            f"📱 Numbers live aayenge...",
            parse_mode=ParseMode.HTML,
        )
        return
    if data == "adm_cache":
        await cmd_cache(q.message, ctx)
        return
    if data == "adm_addfb":
        _set(user_id, mode="await_fb_add")
        await q.message.reply_text(
            "➕ <b>Add Firebase</b>\n\nURL bhejein:",
            parse_mode=ParseMode.HTML,
        )
        return
    if data == "adm_bulkfb":
        _set(user_id, mode="await_fb_bulk")
        await q.message.reply_text(
            "📄 <b>Bulk Import</b>\n\n"
            "TXT file bhejo ya text message me URLs paste karo.\n\n"
            "❌ Cancel: /cancel",
            parse_mode=ParseMode.HTML,
        )
        return
    if data == "adm_giveprem":
        _set(user_id, mode="await_premium_id")
        await q.message.reply_text("💎 User ID bhejein:", parse_mode=ParseMode.HTML)
        return
    if data == "adm_revoke":
        await q.message.reply_text("Use: <code>/revoke USER_ID</code>", parse_mode=ParseMode.HTML)
        return
    if data.startswith("adm_fbs:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        fbs = list(fb_collection.find({}).sort("added_at", -1).skip(page * 5).limit(5))
        total = fb_collection.count_documents({})
        if not fbs:
            await q.edit_message_text("📭 No firebases.",
                                      reply_markup=InlineKeyboardMarkup([
                                          [InlineKeyboardButton("⬅️ Back", callback_data="adm_back")]
                                      ]))
            return
        lines = []
        for f in fbs:
            lines.append(f"🔥 <b>{esc(short(f.get('name','?'), 26))}</b>\n   🔒 <code>{esc(fb_hash(f.get('url','')))}</code>")
        body = "\n\n".join(lines)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_fbs:{page-1}"))
        if (page + 1) * 5 < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_fbs:{page+1}"))
        kb_rows = []
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("🗑 Delete All", callback_data="adm_fbdelall")])
        kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await q.edit_message_text(
            f"🔥 <b>Firebases</b> — Page {page+1} · Total {total}\n\n{body}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return
    if data == "adm_fbdelall":
        res = fb_collection.delete_many({})
        with LIVE_CACHE["lock"]:
            LIVE_CACHE["numbers"] = {}
            LIVE_CACHE["online"] = 0
            LIVE_CACHE["offline"] = 0
        await q.edit_message_text(
            f"✅ Deleted {res.deleted_count} firebases",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="adm_back")]
            ]),
        )
        return
    if data.startswith("adm_users:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        users = list(users_collection.find({}).sort("last_seen", -1).skip(page * 5).limit(5))
        total = users_collection.count_documents({})
        if not users:
            await q.edit_message_text("📭 No users.",
                                      reply_markup=InlineKeyboardMarkup([
                                          [InlineKeyboardButton("⬅️ Back", callback_data="adm_back")]
                                      ]))
            return
        now = int(time.time())
        lines = []
        for u in users:
            uid = u.get("user_id")
            uname = u.get("username") or ""
            fname = u.get("first_name") or ""
            pu = int(u.get("premium_until", 0))
            is_prem = pu > now
            days = max(0, (pu - now) // 86400) if is_prem else 0
            badge = f"💎 {days}d" if is_prem else "🆓 Free"
            lines.append(
                f"👤 <b>{esc(fname)}</b> {badge}\n"
                f"   🆔 <code>{uid}</code>\n"
                f"   @{esc(uname) if uname else '—'}"
            )
        body = "\n\n".join(lines)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_users:{page-1}"))
        if (page + 1) * 5 < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_users:{page+1}"))
        kb_rows = []
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await q.edit_message_text(
            f"👥 <b>Users</b> — Page {page+1} · Total {total}\n\n{body}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return
    if data == "adm_back":
        await show_admin_panel(q.message, user_id)
        return


# ============================================================
# ADMIN COMMANDS
# ============================================================
async def cmd_givepremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /givepremium USER_ID [days=30]")
        return
    try:
        target = int(args[0])
        days = int(args[1]) if len(args) > 1 else 30
    except Exception:
        await update.message.reply_text("❌ Invalid args.")
        return
    ok, until = grant_premium(target, days)
    if ok:
        dt = datetime.fromtimestamp(until, tz=IST).strftime("%d-%m-%Y")
        await update.message.reply_text(
            f"✅ <code>{target}</code> → {days}d till {dt}",
            parse_mode=ParseMode.HTML,
        )
        try:
            await ctx.bot.send_message(
                chat_id=target,
                text=f"🎉 <b>Premium Activated!</b>\n\n{days} din.\nTill <b>{dt}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    else:
        await update.message.reply_text("❌ Failed.")


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /revoke USER_ID")
        return
    try:
        target = int(args[0])
    except Exception:
        await update.message.reply_text("❌ Invalid ID.")
        return
    revoke_premium(target)
    await update.message.reply_text(f"✅ Revoked <code>{target}</code>.",
                                    parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _set(update.effective_user.id, mode="idle")
    await update.message.reply_text("❎ Cancelled.")


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 <b>Your ID:</b>\n<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN missing")
        return
    if not MONGO_URI:
        print("⚠️ MONGO_URI missing")

    init_mongo()

    threading.Thread(target=background_rebuild_loop, daemon=True).start()

    if MONGO_OK:
        threading.Thread(target=lambda: build_all_numbers(force=True), daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("otp", cmd_otp))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("givepremium", cmd_givepremium))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("rebuild", cmd_rebuild))
    app.add_handler(CommandHandler("cache", cmd_cache))

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def post_init(application: Application):
        await application.bot.set_my_commands([
            BotCommand("start", "🏠 Main menu"),
            BotCommand("otp", "🔐 OTP Panel (LIVE)"),
            BotCommand("premium", "💎 Premium status"),
            BotCommand("stats", "📊 Stats"),
            BotCommand("rebuild", "🔨 Rebuild (admin)"),
            BotCommand("cache", "🔍 Cache debug (admin)"),
            BotCommand("myid", "🆔 My Telegram ID"),
            BotCommand("help", "❓ Help"),
        ])

    app.post_init = post_init
    print("🤖 Alex OTP Bot v3.4 LIVE starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
