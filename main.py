#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import time
import datetime
import threading

app = Flask(__name__, template_folder='.')
CORS(app)

# ---------- ثوابت فودافون ----------
AUTH_URL = "https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token"
SEND_URL = "https://web.vodafone.com.eg/services/dxl/pj/journey/promoJourney"
PAGE_URL = "https://web.vodafone.com.eg/portal/bf/massSummerPromo26?isPostMessages=false"
CLIENT_ID = "AnaVF"
CLIENT_SECRET = "dca0pbLUWXVhXR266Gw1iT5rqwvvJQoN"
USER_AGENT = "vodafoneandroid"
ORIGIN = "https://web.vodafone.com.eg"
REFERER = "https://web.vodafone.com.eg/portal/bf/massSummerPromo26/streak"

POKE_COUNT = 6
EMOJI_CODES = ["1F606", "1F607", "1F618"]
DELAY_BETWEEN_REQUESTS = 1.5
DELAY_BETWEEN_ROUNDS = 5.0
MAX_RETRIES = 2

# تخزين البيانات في الذاكرة والقاعدة
active_streaks = {}
streak_lock = threading.Lock()

def login_and_get_session(msisdn, password, log_callback):
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ar",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "Content-Type": "application/x-www-form-urlencoded",
    })

    payload = {
        "grant_type": "password",
        "username": msisdn,
        "password": password,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    
    resp = session.post(AUTH_URL, data=payload, timeout=12)
    if resp.status_code != 200:
        raise Exception(f"فشل تسجيل الدخول للرقم {msisdn}: {resp.text[:100]}")

    data = resp.json()
    access_token = data.get("access_token")

    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "msisdn": msisdn,
        "clientId": "WebsiteConsumer",
        "channel": "APP_PORTAL",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    session.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    try:
        session.get(PAGE_URL, timeout=10)
    except Exception:
        pass
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    return session

def send_request_with_retry(session, payload, sender, receiver, request_type, log_callback, retries=MAX_RETRIES):
    for attempt in range(retries + 1):
        try:
            resp = session.post(SEND_URL, json=payload, timeout=12)
            if resp.status_code == 201:
                log_callback(f"   ✅ {request_type} ({sender} -> {receiver})")
                return True
            else:
                log_callback(f"   ⚠️ فشل {request_type} (محاولة {attempt+1}): حالة {resp.status_code}")
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
                else:
                    raise Exception(f"فشل {request_type} من {sender} بعد {retries+1} محاولات.")
        except Exception as e:
            if attempt < retries:
                log_callback(f"   ⚠️ خطأ شبكة (محاولة {attempt+1}): {str(e)[:60]}")
                time.sleep(3)
            else:
                raise Exception(f"خطأ اتصالات مع السيرفر: {str(e)[:60]}")
    return False

def execute_real_flow(num1, pass1, num2, pass2, log_callback):
    time_now = datetime.datetime.now().strftime("%I:%M:%S %p")
    log_callback(f"[{time_now}] ⏳ بدء تسجيل الدخول بالرقم الثاني ({num2})...")
    
    session2 = login_and_get_session(num2, pass2, log_callback)
    log_callback(f"[{time_now}] ✅ تم إعداد جلسة الرقم الثاني ({num2}) بنجاح.")
    
    log_callback(f"📤 [بدء الإرسال من {num2} إلى {num1}]")
    for i in range(POKE_COUNT):
        payload = {"@type": "sendMessage", "characteristics": [{"name": "messageType", "value": "POKE"}]}
        send_request_with_retry(session2, payload, num2, num1, f"POKE {i+1}/{POKE_COUNT}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    for code in EMOJI_CODES:
        payload = {
            "@type": "sendMessage",
            "characteristics": [
                {"name": "messageType", "value": "EMOJI"},
                {"name": "messageContent", "value": code}
            ]
        }
        send_request_with_retry(session2, payload, num2, num1, f"إيموجي {code}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    log_callback(f"⏳ انتظار {DELAY_BETWEEN_ROUNDS} ثواني قبل بدء الدورة العكسية...")
    time.sleep(DELAY_BETWEEN_ROUNDS)

    log_callback(f"[{time_now}] ⏳ بدء تسجيل الدخول بالرقم الأول ({num1})...")
    session1 = login_and_get_session(num1, pass1, log_callback)
    log_callback(f"[{time_now}] ✅ تم إعداد جلسة الرقم الأول ({num1}) بنجاح.")

    log_callback(f"📤 [بدء الإرسال العكسي من {num1} إلى {num2}]")
    for i in range(POKE_COUNT):
        payload = {"@type": "sendMessage", "characteristics": [{"name": "messageType", "value": "POKE"}]}
        send_request_with_retry(session1, payload, num1, num2, f"POKE {i+1}/{POKE_COUNT}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    for code in EMOJI_CODES:
        payload = {
            "@type": "sendMessage",
            "characteristics": [
                {"name": "messageType", "value": "EMOJI"},
                {"name": "messageContent", "value": code}
            ]
        }
        send_request_with_retry(session1, payload, num1, num2, f"إيموجي {code}", log_callback)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    log_callback(f"🎉 🎉 تم تنفيذ كامل العملية بنجاح وبشكل حقيقي!")

# خادم الجدولة الأوتوماتيكي في الخلفية
def scheduled_job_runner():
    with streak_lock:
        now = datetime.datetime.now()
        for key, streak in active_streaks.items():
            if streak.get('next_run') and now >= streak['next_run']:
                print(f"⏰ حان وقت التكرار التلقائي للستريك: {streak['num1']} <-> {streak['num2']}")
                
                logs = []
                def append_log(msg):
                    logs.append(msg)

                try:
                    execute_real_flow(streak['num1'], streak['pass1'], streak['num2'], streak['pass2'], append_log)
                    streak['success_count'] += 1
                    streak['logs'] = [f"--- تكرار تلقائي [{now.strftime('%I:%M %p')}] ---"] + logs + streak['logs']
                except Exception as e:
                    streak['logs'] = [f"--- خطأ تكرار تلقائي [{now.strftime('%I:%M %p')}]: {str(e)} ---"] + streak['logs']
                
                # إعداد موعد التشغيل القادم
                streak['last_run'] = now.strftime("%I:%M %p")
                streak['next_run'] = now + datetime.timedelta(hours=streak['hours'])

scheduler = BackgroundScheduler()
scheduler.add_job(func=scheduled_job_runner, trigger="interval", seconds=60)
scheduler.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/streaks', methods=['GET'])
def get_streaks():
    result = []
    with streak_lock:
        for key, s in active_streaks.items():
            result.append({
                "id": s["id"],
                "num1": s["num1"],
                "num2": s["num2"],
                "hours": s["hours"],
                "last_run": s["last_run"],
                "success_count": s["success_count"],
                "logs": s["logs"]
            })
    return jsonify(result)

@app.route('/api/run_streak', methods=['POST'])
def run_streak():
    data = request.json or {}
    num1 = data.get('num1', '').strip()
    pass1 = data.get('pass1', '').strip()
    num2 = data.get('num2', '').strip()
    pass2 = data.get('pass2', '').strip()
    hours = int(data.get('hours', 10))

    if not num1 or not pass1 or not num2 or not pass2:
        return jsonify({"success": False, "error": "جميع البيانات (الارقام وكلمات المرور) مطلوبة!"}), 400

    streak_key = f"{min(num1, num2)}_{max(num1, num2)}"
    logs = []

    def append_log(msg):
        logs.append(msg)

    try:
        execute_real_flow(num1, pass1, num2, pass2, append_log)
        time_now = datetime.datetime.now()

        with streak_lock:
            if streak_key in active_streaks:
                # تحديث الستريك الموجود بدون تكراره بالبطاقات
                active_streaks[streak_key]["pass1"] = pass1
                active_streaks[streak_key]["pass2"] = pass2
                active_streaks[streak_key]["hours"] = hours
                active_streaks[streak_key]["last_run"] = time_now.strftime("%I:%M %p")
                active_streaks[streak_key]["next_run"] = time_now + datetime.timedelta(hours=hours)
                active_streaks[streak_key]["success_count"] += 1
                active_streaks[streak_key]["logs"] = [f"--- تشغيل يدوي [{time_now.strftime('%I:%M %p')}] ---"] + logs + active_streaks[streak_key]["logs"]
            else:
                # إضافة جديد
                active_streaks[streak_key] = {
                    "id": int(time.time() * 1000),
                    "num1": num1,
                    "pass1": pass1,
                    "num2": num2,
                    "pass2": pass2,
                    "hours": hours,
                    "last_run": time_now.strftime("%I:%M %p"),
                    "next_run": time_now + datetime.timedelta(hours=hours),
                    "success_count": 1,
                    "logs": [f"--- بداية جديدة [{time_now.strftime('%I:%M %p')}] ---"] + logs
                }

        return jsonify({"success": True, "logs": logs})

    except Exception as e:
        error_msg = f"❌ خطأ أثناء التنفيذ: {str(e)}"
        logs.append(error_msg)
        return jsonify({"success": False, "error": str(e), "logs": logs}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
