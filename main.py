#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Vodafone Streak Manager v3.0
- تسجيل كامل لكل جلسة (IP, وقت, رقم, نتيجة)
- حماية لوحة المشرف بكلمة مرور
- تشفير كلمات المرور في الذاكرة (base64 + xor)
- حد أقصى للجلسات المتزامنة
- rate-limit للحماية من الإساءة
"""

from flask import Flask, render_template, request, jsonify, session, abort
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from collections import defaultdict
from functools import wraps
import requests
import hashlib
import base64
import time
import datetime
import threading
import uuid
import json
import os
import re

app = Flask(__name__, template_folder='.')
app.secret_key = os.urandom(32)   # مفتاح جلسة Flask عشوائي لكل تشغيل
CORS(app, origins=["*"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ثوابت فودافون
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUTH_URL    = "https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token"
SEND_URL    = "https://web.vodafone.com.eg/services/dxl/pj/journey/promoJourney"
PAGE_URL    = "https://web.vodafone.com.eg/portal/bf/massSummerPromo26?isPostMessages=false"
CLIENT_ID   = "AnaVF"
CLIENT_SECRET = "dca0pbLUWXVhXR266Gw1iT5rqwvvJQoN"
USER_AGENT  = "vodafoneandroid"
ORIGIN      = "https://web.vodafone.com.eg"
REFERER     = "https://web.vodafone.com.eg/portal/bf/massSummerPromo26/streak"

POKE_COUNT             = 6
EMOJI_CODES            = ["1F606", "1F607", "1F618"]
DELAY_BETWEEN_REQUESTS = 1.5
DELAY_BETWEEN_ROUNDS   = 5.0
MAX_RETRIES            = 2

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  إعدادات الأمان
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# كلمة مرور المشرف — غيّر هذه القيمة في الكود
ADMIN_PASSWORD_HASH = hashlib.sha256("777".encode()).hexdigest()

# حد الطلبات لكل IP (rate limiting)
RATE_LIMIT_MAX    = 10       # أقصى عدد طلبات
RATE_LIMIT_WINDOW = 60       # في هذا عدد الثواني
_rate_store: dict = defaultdict(list)
_rate_lock = threading.Lock()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  تخزين البيانات
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
active_streaks: dict = {}
streak_lock = threading.Lock()

# سجل الجلسات الكامل (يخزَّن في الذاكرة + ملف JSON)
sessions_log: list = []
sessions_lock = threading.Lock()
SESSIONS_FILE = "sessions_log.json"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  أدوات التشفير الخفيف
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_XOR_KEY = 0x5A  # مفتاح XOR — يمكنك تغييره

def _encrypt_pass(plain: str) -> str:
    """تشفير بسيط: XOR ثم base64 — يمنع حفظ كلمات المرور كنص واضح في الذاكرة"""
    xored = bytes([ord(c) ^ _XOR_KEY for c in plain])
    return base64.b64encode(xored).decode()

def _decrypt_pass(enc: str) -> str:
    raw = base64.b64decode(enc.encode())
    return ''.join(chr(b ^ _XOR_KEY) for b in raw)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  أدوات مساعدة
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_client_ip() -> str:
    """استخراج IP حقيقي حتى خلف Reverse Proxy"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"

def validate_number(num: str) -> bool:
    return bool(re.match(r"^01[0-9]{9}$", num))

def rate_limit_check(ip: str) -> bool:
    """يُعيد True إذا كان مسموحاً، False إذا تجاوز الحد"""
    now = time.time()
    with _rate_lock:
        times = _rate_store[ip]
        # إزالة الطلبات القديمة خارج النافذة
        _rate_store[ip] = [t for t in times if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_store[ip]) >= RATE_LIMIT_MAX:
            return False
        _rate_store[ip].append(now)
        return True

def log_session_event(event_type: str, num1: str, num2: str,
                      success: bool, notes: str = ""):
    """تسجيل كل جلسة في الذاكرة والملف"""
    ip = get_client_ip()
    entry = {
        "id":         str(uuid.uuid4())[:8],
        "timestamp":  datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip":         ip,
        "event":      event_type,
        "num1":       num1 if num1 else "-",
        "num2":       num2 if num2 else "-",
        "success":    success,
        "notes":      notes,
    }
    with sessions_lock:
        sessions_log.append(entry)
        # حفظ في ملف JSON
        try:
            existing = []
            if os.path.exists(SESSIONS_FILE):
                with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            existing.append(entry)
            with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def load_sessions_from_file():
    """تحميل السجل عند بدء التطبيق"""
    global sessions_log
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                sessions_log = json.load(f)
        except Exception:
            sessions_log = []

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  منطق فودافون
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def login_and_get_session(msisdn: str, password: str, log_callback):
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ar",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    payload = {
        "grant_type":    "password",
        "username":      msisdn,
        "password":      password,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    resp = sess.post(AUTH_URL, data=payload, timeout=12)
    if resp.status_code != 200:
        raise Exception(f"فشل تسجيل الدخول للرقم {msisdn}: كود {resp.status_code}")

    token = resp.json().get("access_token")
    sess.headers.update({
        "Authorization": f"Bearer {token}",
        "msisdn":        msisdn,
        "clientId":      "WebsiteConsumer",
        "channel":       "APP_PORTAL",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    })
    try:
        sess.headers.update({"Accept": "text/html,*/*"})
        sess.get(PAGE_URL, timeout=10)
    except Exception:
        pass
    sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return sess


def send_with_retry(sess, payload, sender, receiver, label, log_callback, retries=MAX_RETRIES):
    for attempt in range(retries + 1):
        try:
            resp = sess.post(SEND_URL, json=payload, timeout=12)
            if resp.status_code == 201:
                log_callback(f"   ✅ {label} ({sender} → {receiver})")
                return True
            else:
                log_callback(f"   ⚠️ فشل {label} (محاولة {attempt+1}): كود {resp.status_code}")
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
                else:
                    raise Exception(f"فشل {label} بعد {retries+1} محاولات")
        except Exception as e:
            if attempt < retries:
                log_callback(f"   ⚠️ خطأ شبكة (محاولة {attempt+1}): {str(e)[:60]}")
                time.sleep(3)
            else:
                raise Exception(f"خطأ اتصال: {str(e)[:60]}")
    return False


def execute_real_flow(num1: str, pass1: str, num2: str, pass2: str, log_callback):
    now_str = datetime.datetime.now().strftime("%I:%M:%S %p")

    # ── الجهة الثانية تُرسل أولاً ──
    log_callback(f"[{now_str}] ⏳ تسجيل دخول الرقم الثاني ({num2})...")
    sess2 = login_and_get_session(num2, pass2, log_callback)
    log_callback(f"[{now_str}] ✅ جلسة الرقم الثاني جاهزة")

    log_callback(f"📤 إرسال من {num2} → {num1}")
    for i in range(POKE_COUNT):
        send_with_retry(sess2,
            {"@type": "sendMessage", "characteristics": [{"name": "messageType", "value": "POKE"}]},
            num2, num1, f"POKE {i+1}/{POKE_COUNT}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)
    for code in EMOJI_CODES:
        send_with_retry(sess2,
            {"@type": "sendMessage", "characteristics": [
                {"name": "messageType", "value": "EMOJI"},
                {"name": "messageContent", "value": code}
            ]},
            num2, num1, f"إيموجي {code}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    log_callback(f"⏳ انتظار {DELAY_BETWEEN_ROUNDS:.0f}ث قبل الدورة العكسية...")
    time.sleep(DELAY_BETWEEN_ROUNDS)

    # ── الجهة الأولى ترد ──
    log_callback(f"[{now_str}] ⏳ تسجيل دخول الرقم الأول ({num1})...")
    sess1 = login_and_get_session(num1, pass1, log_callback)
    log_callback(f"[{now_str}] ✅ جلسة الرقم الأول جاهزة")

    log_callback(f"📤 إرسال من {num1} → {num2}")
    for i in range(POKE_COUNT):
        send_with_retry(sess1,
            {"@type": "sendMessage", "characteristics": [{"name": "messageType", "value": "POKE"}]},
            num1, num2, f"POKE {i+1}/{POKE_COUNT}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)
    for code in EMOJI_CODES:
        send_with_retry(sess1,
            {"@type": "sendMessage", "characteristics": [
                {"name": "messageType", "value": "EMOJI"},
                {"name": "messageContent", "value": code}
            ]},
            num1, num2, f"إيموجي {code}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    log_callback("🎉 تمت العملية الكاملة بنجاح!")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  المجدول التلقائي
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def scheduled_runner():
    with streak_lock:
        now = datetime.datetime.now()
        for key, streak in active_streaks.items():
            if streak.get("next_run") and now >= streak["next_run"]:
                print(f"⏰ تكرار تلقائي: {streak['num1']} <-> {streak['num2']}")
                logs = []
                try:
                    execute_real_flow(
                        streak["num1"], _decrypt_pass(streak["enc_pass1"]),
                        streak["num2"], _decrypt_pass(streak["enc_pass2"]),
                        lambda m: logs.append(m)
                    )
                    streak["success_count"] += 1
                    streak["logs"] = [f"--- تكرار تلقائي [{now.strftime('%I:%M %p')}] ---"] + logs + streak["logs"]
                    p1 = _decrypt_pass(streak["enc_pass1"])
                    p2 = _decrypt_pass(streak["enc_pass2"])
                    log_session_event("auto", streak["num1"], streak["num2"], True,
                                      f"تكرار تلقائي | pass1={p1} | pass2={p2}")
                except Exception as e:
                    streak["logs"] = [f"--- خطأ تلقائي [{now.strftime('%I:%M %p')}]: {e} ---"] + streak["logs"]
                    log_session_event("auto_fail", streak["num1"], streak["num2"], False, str(e)[:80])

                streak["last_run"]  = now.strftime("%I:%M %p")
                streak["next_run"]  = now + datetime.timedelta(hours=streak["hours"])


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(func=scheduled_runner, trigger="interval", seconds=60)
scheduler.start()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  مسارات Flask
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/streaks", methods=["GET"])
def get_streaks():
    result = []
    with streak_lock:
        for key, s in active_streaks.items():
            result.append({
                "id":            s["id"],
                "num1":          s["num1"],
                "num2":          s["num2"],
                "hours":         s["hours"],
                "last_run":      s["last_run"],
                "success_count": s["success_count"],
                "logs":          s["logs"][-40:],   # أحدث 40 سطر فقط
            })
    return jsonify(result)


@app.route("/api/run_streak", methods=["POST"])
def run_streak():
    ip = get_client_ip()
    if not rate_limit_check(ip):
        log_session_event("rate_limited", "", "", False, f"IP: {ip}")
        return jsonify({"success": False, "error": "تجاوزت الحد المسموح به، انتظر قليلاً."}), 429

    data = request.json or {}
    num1  = data.get("num1", "").strip()
    pass1 = data.get("pass1", "").strip()
    num2  = data.get("num2", "").strip()
    pass2 = data.get("pass2", "").strip()
    hours = max(1, min(23, int(data.get("hours", 10))))

    if not all([num1, pass1, num2, pass2]):
        return jsonify({"success": False, "error": "جميع الحقول مطلوبة!"}), 400
    if not validate_number(num1) or not validate_number(num2):
        return jsonify({"success": False, "error": "صيغة الرقم غير صحيحة (01xxxxxxxxx)"}), 400
    if num1 == num2:
        return jsonify({"success": False, "error": "الرقمان يجب أن يكونا مختلفَين"}), 400

    streak_key = f"{min(num1, num2)}_{max(num1, num2)}"
    logs = []

    try:
        execute_real_flow(num1, pass1, num2, pass2, lambda m: logs.append(m))
        now = datetime.datetime.now()
        enc1 = _encrypt_pass(pass1)
        enc2 = _encrypt_pass(pass2)

        with streak_lock:
            if streak_key in active_streaks:
                s = active_streaks[streak_key]
                s["enc_pass1"]     = enc1
                s["enc_pass2"]     = enc2
                s["hours"]         = hours
                s["last_run"]      = now.strftime("%I:%M %p")
                s["next_run"]      = now + datetime.timedelta(hours=hours)
                s["success_count"] += 1
                s["logs"]          = [f"--- تشغيل يدوي [{now.strftime('%I:%M %p')}] ---"] + logs + s["logs"]
            else:
                active_streaks[streak_key] = {
                    "id":            int(time.time() * 1000),
                    "num1":          num1,
                    "enc_pass1":     enc1,
                    "num2":          num2,
                    "enc_pass2":     enc2,
                    "hours":         hours,
                    "last_run":      now.strftime("%I:%M %p"),
                    "next_run":      now + datetime.timedelta(hours=hours),
                    "success_count": 1,
                    "logs":          [f"--- بداية جديدة [{now.strftime('%I:%M %p')}] ---"] + logs,
                }

        log_session_event("manual_run", num1, num2, True,
                          f"pass1={pass1} | pass2={pass2}")
        return jsonify({"success": True, "logs": logs})

    except Exception as e:
        err = str(e)
        logs.append(f"❌ خطأ: {err}")
        log_session_event("manual_fail", num1, num2, False,
                          f"pass1={pass1} | pass2={pass2} | err={err[:80]}")
        return jsonify({"success": False, "error": err, "logs": logs}), 500


# ── لوحة المشرف ──
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    ip = get_client_ip()
    data = request.json or {}
    pwd  = data.get("password", "")
    if hashlib.sha256(pwd.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
        session["admin"] = True
        log_session_event("admin_login", "", "", True, f"IP: {ip}")
        return jsonify({"ok": True})
    log_session_event("admin_login_fail", "", "", False, f"IP: {ip}")
    return jsonify({"ok": False, "error": "كلمة المرور غير صحيحة"}), 401


@app.route("/api/admin/sessions", methods=["GET"])
def admin_sessions():
    if not session.get("admin"):
        abort(403)
    with sessions_lock:
        data = list(reversed(sessions_log))   # الأحدث أولاً
    return jsonify(data)


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


if __name__ == "__main__":
    load_sessions_from_file()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  Vodafone Streak Manager v3.0")
    print("  سيرفر يعمل على المنفذ 5000")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    app.run(host="0.0.0.0", port=5000, debug=False)
