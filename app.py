import requests
import time
import re
import warnings
import os
import threading
import json
import hashlib
import secrets
import base64
import smtplib
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, session, redirect
from flask_cors import CORS

warnings.filterwarnings('ignore', category=DeprecationWarning)

# ─── PATHS ───────────────────────────────────────────────────────────────────
_DATA_ROOT    = "/data" if os.path.isdir("/data") else "."
USERS_FILE    = os.path.join(_DATA_ROOT, "users.json")
USER_DATA_DIR = os.path.join(_DATA_ROOT, "user_data")

BATCH_SIZE  = 2
TARGET_HOUR = 8

# ─── OAUTH APP CREDENTIALS ───────────────────────────────────────────────────
LI_CLIENT_ID     = os.environ.get("LINKEDIN_CLIENT_ID", "")
LI_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
LI_REDIRECT_URI  = os.environ.get("LINKEDIN_REDIRECT_URI", "")

FB_APP_ID        = os.environ.get("FACEBOOK_APP_ID", "")
FB_APP_SECRET    = os.environ.get("FACEBOOK_APP_SECRET", "")
FB_REDIRECT_URI  = os.environ.get("FACEBOOK_REDIRECT_URI", "")

IG_APP_ID        = os.environ.get("INSTAGRAM_APP_ID", "")
IG_APP_SECRET    = os.environ.get("INSTAGRAM_APP_SECRET", "")
IG_REDIRECT_URI  = os.environ.get("INSTAGRAM_REDIRECT_URI", "")

# ─── GOOGLE OAUTH (Sign In / Sign Up) ─────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "")

# ─── EMAIL CONFIG ─────────────────────────────────────────────────────────────
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER)
APP_BASE_URL  = os.environ.get("APP_BASE_URL", "http://localhost:5000")

# ─── AI PROMPTS ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT_POST = """You are an expert LinkedIn ghostwriter.
Write a highly engaging, professional LinkedIn post based on the user's subject.
IMPORTANT: Your response must be between 2800 and 3000 characters total. Count carefully.
1. Hook on the first line wrapped in **asterisks**.
2. Short, punchy sentences with good spacing between paragraphs.
3. Include real insights, tips, or a story to fill the length naturally.
4. Call-To-Action (CTA) question at the end.
5. 3 to 5 relevant hashtags.
Do not exceed 3000 characters. Do not go below 2800 characters."""

# ─── APP SETUP ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

# ─── HTML LOADER ──────────────────────────────────────────────────────────────
def load_html(name):
    path = os.path.join(os.path.dirname(__file__), name)
    if os.path.exists(path):
        return open(path, encoding="utf-8").read()
    return f"<h1>{name} not found</h1>"

# ─── ENCRYPTION ───────────────────────────────────────────────────────────────
SENSITIVE_FIELDS = {
    "groq_api_key", "serpapi_key",
    "linkedin_1_access_token", "linkedin_2_access_token", "linkedin_3_access_token",
    "facebook_access_token", "instagram_access_token",
}

def _get_fernet():
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None

def _encrypt(value):
    f = _get_fernet()
    if not f or not isinstance(value, str):
        return value
    return "ENC:" + f.encrypt(value.encode()).decode()

def _decrypt(value):
    if not isinstance(value, str) or not value.startswith("ENC:"):
        return value
    f = _get_fernet()
    if not f:
        return value
    try:
        return f.decrypt(value[4:].encode()).decode()
    except Exception:
        return value

def _encrypt_cfg(cfg):
    out = {}
    for k, v in cfg.items():
        if k in SENSITIVE_FIELDS and isinstance(v, str) and v and not v.startswith("ENC:"):
            out[k] = _encrypt(v)
        else:
            out[k] = v
    return out

def _decrypt_cfg(cfg):
    out = {}
    for k, v in cfg.items():
        if k in SENSITIVE_FIELDS and isinstance(v, str) and v.startswith("ENC:"):
            out[k] = _decrypt(v)
        else:
            out[k] = v
    return out

# ─── USER HELPERS ─────────────────────────────────────────────────────────────
def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return h, salt

def verify_password(password, stored_hash, salt):
    h, _ = hash_password(password, salt)
    return h == stored_hash

def user_has_password(u):
    """
    Returns whether the user has a real, usable password set.
    - If 'has_password' key exists, use it directly.
    - Otherwise (legacy accounts): Google-authenticated accounts default to False
      (they only have a random unusable hash), all other legacy accounts default to True.
    """
    if "has_password" in u:
        return bool(u["has_password"])
    return u.get("auth_provider") != "google"

def find_user_by_email(email):
    """Return (username, user_data) for a given email, or (None, None)."""
    users = load_users()
    email_lower = email.strip().lower()
    for uname, udata in users.items():
        if udata.get("email", "").lower() == email_lower:
            return uname, udata
    return None, None

def user_dir(username):
    d = os.path.join(USER_DATA_DIR, username)
    os.makedirs(d, exist_ok=True)
    return d

def user_config_path(username):
    return os.path.join(user_dir(username), "config.json")

def user_subjects_path(username):
    return os.path.join(user_dir(username), "subjects.txt")

def user_state_path(username):
    return os.path.join(user_dir(username), "state.json")

def user_review_path(username):
    return os.path.join(user_dir(username), "review_queue.json")

def load_config(username):
    p = user_config_path(username)
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return _decrypt_cfg(raw)

def save_config(username, cfg):
    encrypted = _encrypt_cfg(cfg)
    with open(user_config_path(username), "w", encoding="utf-8") as f:
        json.dump(encrypted, f, indent=2)

def load_state(username):
    p = user_state_path(username)
    default = {"status": "waiting", "logs": [], "today_count": 0,
                "total_run": 0, "last_run": None, "running": False}
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        s = json.load(f)
    s["running"] = False
    return s

def save_state(username, state):
    with open(user_state_path(username), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def load_review_queue(username):
    p = user_review_path(username)
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_review_queue(username, queue):
    with open(user_review_path(username), "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)

_states = {}
_state_lock = threading.Lock()

def get_state(username):
    with _state_lock:
        if username not in _states:
            _states[username] = load_state(username)
        return _states[username]

def add_log(username, msg, level="info"):
    state  = get_state(username)
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", 0)
    user_now = datetime.utcnow() + timedelta(hours=offset)
    entry = {"time": user_now.strftime("%H:%M:%S"), "msg": msg, "level": level}
    state["logs"].append(entry)
    if len(state["logs"]) > 100:
        state["logs"] = state["logs"][-100:]
    print(f"[{username}][{entry['time']}][{level.upper()}] {msg}")

# ─── AUTH DECORATOR ───────────────────────────────────────────────────────────
def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get("username")
        if not username:
            return jsonify({"ok": False, "message": "Not authenticated"}), 401
        return f(*args, username=username, **kwargs)
    return decorated

# ─── EMAIL HELPERS ────────────────────────────────────────────────────────────
def send_email(to_email, subject, html_body):
    """Send email via SMTP (use Gmail SMTP on Railway)."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL] SMTP not configured — SMTP_USER or SMTP_PASSWORD missing")
        return False

    print(f"[EMAIL] Attempting to send to {to_email} via {SMTP_HOST}:{SMTP_PORT}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM or SMTP_USER
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))
    raw = msg.as_string()

    import ssl

    # Method 1: STARTTLS port 587
    try:
        print(f"[EMAIL] Trying STARTTLS port 587...")
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM or SMTP_USER, to_email, raw)
        print(f"[EMAIL] Sent via Gmail STARTTLS to {to_email}")
        return True
    except Exception as e1:
        print(f"[EMAIL] Gmail STARTTLS failed: {e1}")

    # Method 2: SSL port 465
    try:
        print(f"[EMAIL] Trying Gmail SSL port 465...")
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM or SMTP_USER, to_email, raw)
        print(f"[EMAIL] Sent via Gmail SSL:465 to {to_email}")
        return True
    except Exception as e2:
        print(f"[EMAIL] Gmail SSL:465 failed: {e2}")

    print(f"[EMAIL] All methods failed for {to_email}")
    return False

def send_review_email(username, review_id, subject_text, post_preview):
    """Notify the user that a post is pending review."""
    users = load_users()
    if username not in users:
        return
    email = users[username].get("email", "")
    if not email:
        return
    review_url = f"{APP_BASE_URL}/dashboard#review"
    preview_short = post_preview[:300].replace("\n", "<br>") + ("..." if len(post_preview) > 300 else "")
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:600px;margin:0 auto;background:#05030f;color:#ede8ff;border-radius:16px;padding:32px;border:1px solid rgba(176,133,255,0.3);">
      <div style="font-size:24px;font-weight:800;margin-bottom:6px;">📋 Post Ready for Review</div>
      <div style="font-size:13px;color:rgba(200,185,255,0.6);margin-bottom:24px;">LinkedIn Agent · Auto-Scheduler</div>
      <div style="background:rgba(255,255,255,0.04);border:1px solid rgba(176,133,255,0.2);border-radius:12px;padding:18px;margin-bottom:20px;">
        <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#b085ff;margin-bottom:8px;">Subject</div>
        <div style="font-size:14px;font-weight:700;">{subject_text}</div>
      </div>
      <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(160,120,255,0.15);border-radius:12px;padding:18px;margin-bottom:24px;">
        <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#b085ff;margin-bottom:8px;">Post Preview</div>
        <div style="font-size:13px;line-height:1.7;color:rgba(235,230,255,0.85);">{preview_short}</div>
      </div>
      <div style="background:rgba(255,204,92,0.08);border:1px solid rgba(255,204,92,0.25);border-radius:10px;padding:14px;margin-bottom:24px;font-size:13px;color:#ffcc5c;">
        ⏰ <strong>Auto-publishes in 1 hour</strong> if no action is taken.
      </div>
      <a href="{review_url}" style="display:inline-block;background:linear-gradient(135deg,#b085ff,#ff80b5);color:#fff;text-decoration:none;padding:14px 28px;border-radius:12px;font-weight:700;font-size:15px;margin-bottom:12px;">
        Review &amp; Edit Post →
      </a>
      <div style="font-size:11px;color:rgba(200,185,255,0.4);margin-top:16px;">
        Review ID: {review_id} · Sent to {email}
      </div>
    </div>
    """
    send_email(email, f"📋 Post ready for review: {subject_text[:50]}", html)

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def to_unicode_bold(text):
    normal  = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b_chars = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return text.translate(str.maketrans(normal, b_chars))

def format_linkedin_bold(text):
    return re.sub(r'\*\*(.*?)\*\*', lambda m: to_unicode_bold(m.group(1)), text)

def get_next_run_time_for_user(username):
    """Return UTC datetime of the next scheduled slot for the user."""
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", 0)
    now_utc  = datetime.utcnow()
    user_now = now_utc + timedelta(hours=offset)

    raw_slots = cfg.get("time_slots", [{"h": TARGET_HOUR, "m": 0}])
    if not isinstance(raw_slots, list) or not raw_slots:
        raw_slots = [{"h": TARGET_HOUR, "m": 0}]

    slots = []
    for s in raw_slots:
        if isinstance(s, dict):
            slots.append((int(s.get("h", 8)), int(s.get("m", 0))))
        elif isinstance(s, (int, float)):
            slots.append((int(s), 0))

    # Find the next upcoming slot in user-local time
    candidates = []
    for (sh, sm) in slots:
        t = user_now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if user_now >= t:
            t += timedelta(days=1)
        candidates.append(t)

    next_local = min(candidates) if candidates else (
        user_now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=1)
    )
    # Convert back to UTC
    return next_local - timedelta(hours=offset)


def get_all_next_slots(username):
    """Return list of UTC datetimes for ALL upcoming slots today/tomorrow."""
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", 0)
    now_utc  = datetime.utcnow()
    user_now = now_utc + timedelta(hours=offset)

    raw_slots = cfg.get("time_slots", [{"h": TARGET_HOUR, "m": 0}])
    if not isinstance(raw_slots, list) or not raw_slots:
        raw_slots = [{"h": TARGET_HOUR, "m": 0}]

    result = []
    for s in raw_slots:
        if isinstance(s, dict):
            sh, sm = int(s.get("h", 8)), int(s.get("m", 0))
        elif isinstance(s, (int, float)):
            sh, sm = int(s), 0
        else:
            continue
        t = user_now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if user_now >= t:
            t += timedelta(days=1)
        result.append({
            "local_label": f"{sh:02d}:{sm:02d}",
            "utc_iso": (t - timedelta(hours=offset)).isoformat() + "Z",
            "seconds_left": max(0, int((t - user_now).total_seconds()))
        })
    result.sort(key=lambda x: x["seconds_left"])
    return result


def parse_subject_hour(subject):
    m = re.search(r'@(\d{1,2})(?::00)?\s*(am|pm)?', subject, re.IGNORECASE)
    if not m:
        return None
    hour   = int(m.group(1))
    period = (m.group(2) or "").lower()
    if period == "pm" and hour != 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    return max(0, min(23, hour))


def get_subjects_due_now(username):
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", 0)
    now_utc  = datetime.utcnow()
    user_now = now_utc + timedelta(hours=offset)
    cur_hour = user_now.hour

    subjects_file = user_subjects_path(username)
    if not os.path.exists(subjects_file):
        return []

    with open(subjects_file, "r", encoding="utf-8") as f:
        subjects = [l.strip() for l in f if l.strip()]

    due = []
    for subj in subjects:
        th = parse_subject_hour(subj)
        if th is not None and th == cur_hour:
            due.append(subj)
        elif th is None and cur_hour == TARGET_HOUR:
            due.append(subj)
    return due

def validate_image_url(url, timeout=8):
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            return True
        if r.status_code in (405, 403, 0):
            r2 = requests.get(url, timeout=timeout, stream=True,
                              headers={"User-Agent": "Mozilla/5.0"})
            if r2.status_code == 200 and "image" in r2.headers.get("content-type", ""):
                r2.close()
                return True
        return False
    except Exception:
        return False

# ─── AI FUNCTIONS ─────────────────────────────────────────────────────────────
def generate_post(username, subject):
    cfg      = load_config(username)
    groq_key = cfg.get("groq_api_key", "")
    clean    = subject.replace("(create image)", "").strip()
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_POST},
                {"role": "user",   "content": f"Write a post about: {clean}"}
            ]}, timeout=30
        ).json()
        if "error" in response:
            add_log(username, f"Groq Error: {response['error']['message']}", "error")
            return None
        return format_linkedin_bold(response["choices"][0]["message"]["content"])
    except Exception as e:
        add_log(username, f"Groq failure: {e}", "error")
        return None


def get_image_url(username, subject):
    cfg      = load_config(username)
    serp_key = cfg.get("serpapi_key", "").strip()
    if not serp_key:
        add_log(username, "SerpAPI key not set — post sent without image.", "warn")
        return None
    clean = subject.replace("(create image)", "").strip()
    try:
        response = requests.get("https://serpapi.com/search.json", params={
            "engine": "google_images", "q": f"{clean} infographic", "api_key": serp_key
        }, timeout=20).json()
        results = response.get("images_results", [])
        if not results:
            add_log(username, "SerpAPI returned no images.", "warn")
            return None
        for i, img in enumerate(results[:5]):
            url = img.get("original", "")
            if not url:
                continue
            add_log(username, f"Checking image {i+1}/5...", "info")
            if validate_image_url(url):
                add_log(username, f"Image {i+1} reachable ✓", "ok")
                return url
            add_log(username, f"Image {i+1} unreachable — trying next", "warn")
        add_log(username, "All candidate images unreachable — post sent without image.", "warn")
        return None
    except Exception as e:
        add_log(username, f"SerpAPI Error: {e}", "error")
        return None

# ════════════════════════════════════════════════════════════════════════════════
#  DIRECT PUBLISHING — LinkedIn (multi-slot), Facebook, Instagram
# ════════════════════════════════════════════════════════════════════════════════

def publish_to_linkedin_slot(username, slot, text, image_url=None):
    cfg      = load_config(username)
    token    = cfg.get(f"linkedin_{slot}_access_token", "")
    urn      = cfg.get(f"linkedin_{slot}_urn", "")
    name     = cfg.get(f"linkedin_{slot}_name", f"Slot {slot}")
    acct_type = cfg.get(f"linkedin_{slot}_account_type", "personal")

    if not token or not urn:
        add_log(username, f"  → [LinkedIn #{slot}] Not connected — skipping.", "warn")
        return False

    # NOTE: LinkedIn posting requires the "w_member_social" scope (Share on LinkedIn /
    # Community Management API). The app currently only has OpenID Connect access
    # (sign-in + profile only), so publishing is disabled until that access is approved.
    add_log(username, f"  → [LinkedIn #{slot} — {name}] Posting unavailable — app is connected via OpenID Connect only (sign-in/profile access). LinkedIn posting requires Community Management API approval.", "warn")
    return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    if image_url:
        reg = requests.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers=headers,
            json={"registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": urn,
                "serviceRelationships": [{
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent"
                }]
            }}, timeout=15
        ).json()
        upload_url = reg.get("value", {}).get("uploadMechanism", {}).get(
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}).get("uploadUrl")
        asset = reg.get("value", {}).get("asset")
        if upload_url and asset:
            img_data = requests.get(image_url, timeout=15,
                                    headers={"User-Agent": "Mozilla/5.0"}).content
            requests.put(upload_url, data=img_data,
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
            payload = {
                "author": urn, "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "IMAGE",
                    "media": [{"status": "READY", "media": asset}]
                }},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            }
        else:
            payload = {
                "author": urn, "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text}, "shareMediaCategory": "NONE"
                }},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            }
    else:
        safe_text = text[:3000].rsplit(" ", 1)[0] if len(text) > 3000 else text
        payload = {
            "author": urn, "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": safe_text}, "shareMediaCategory": "NONE"
            }},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
        }

    try:
        res = requests.post("https://api.linkedin.com/v2/ugcPosts",
                            headers=headers, json=payload, timeout=20)
        if res.status_code in (200, 201):
            pid   = res.headers.get("x-restli-id", "n/a")
            add_log(username, f"  → [LinkedIn #{slot} — {name}] Published post ✓ ID: {pid}", "ok")
            return True
        else:
            add_log(username, f"  → [LinkedIn #{slot}] Failed {res.status_code}: {res.text[:200]}", "error")
            return False
    except Exception as e:
        add_log(username, f"  → [LinkedIn #{slot}] Exception: {e}", "error")
        return False


def publish_to_facebook(username, text, image_url=None):
    cfg      = load_config(username)
    token    = cfg.get("facebook_access_token", "")
    page_id  = cfg.get("facebook_page_id", "")
    if not token or not page_id:
        add_log(username, "  → [Facebook] Not connected — skipping.", "warn")
        return False
    try:
        if image_url:
            res = requests.post(
                f"https://graph.facebook.com/v19.0/{page_id}/photos",
                params={"access_token": token},
                data={"url": image_url, "caption": text},
                timeout=20
            )
        else:
            res = requests.post(
                f"https://graph.facebook.com/v19.0/{page_id}/feed",
                params={"access_token": token},
                data={"message": text},
                timeout=20
            )
        result = res.json()
        if "id" in result:
            add_log(username, f"  → [Facebook] Published ✓ ID: {result['id']}", "ok")
            return True
        else:
            add_log(username, f"  → [Facebook] Failed: {result.get('error', {}).get('message', str(result))}", "error")
            return False
    except Exception as e:
        add_log(username, f"  → [Facebook] Exception: {e}", "error")
        return False


def publish_to_instagram(username, text, image_url=None):
    cfg     = load_config(username)
    token   = cfg.get("instagram_access_token", "")
    ig_id   = cfg.get("instagram_account_id", "")
    if not token or not ig_id:
        add_log(username, "  → [Instagram] Not connected — skipping.", "warn")
        return False
    if not image_url:
        add_log(username, "  → [Instagram] Skipped — Instagram requires an image.", "warn")
        return False
    try:
        container = requests.post(
            f"https://graph.facebook.com/v19.0/{ig_id}/media",
            params={"access_token": token},
            data={"image_url": image_url, "caption": text},
            timeout=20
        ).json()
        container_id = container.get("id")
        if not container_id:
            add_log(username, f"  → [Instagram] Container failed: {container.get('error', {}).get('message', str(container))}", "error")
            return False
        pub = requests.post(
            f"https://graph.facebook.com/v19.0/{ig_id}/media_publish",
            params={"access_token": token},
            data={"creation_id": container_id},
            timeout=20
        ).json()
        if "id" in pub:
            add_log(username, f"  → [Instagram] Published ✓ ID: {pub['id']}", "ok")
            return True
        else:
            add_log(username, f"  → [Instagram] Publish failed: {pub.get('error', {}).get('message', str(pub))}", "error")
            return False
    except Exception as e:
        add_log(username, f"  → [Instagram] Exception: {e}", "error")
        return False


def publish_to_channels(username, text, image_url=None, target_channels=None):
    """
    Publish to channels. target_channels is a list like ["linkedin_1","facebook_1","instagram_1"].
    If None, publish to all enabled channels (legacy behaviour).
    """
    cfg        = load_config(username)
    ch_enabled = cfg.get("channel_enabled", {})
    success    = 0

    def channel_allowed(key):
        # If target_channels specified, only post to those
        if target_channels is not None:
            return key in target_channels
        # Otherwise respect global toggle
        return ch_enabled.get(key, True)

    for slot in [1, 2, 3]:
        token = cfg.get(f"linkedin_{slot}_access_token", "")
        if token:
            key = f"linkedin_{slot}"
            if not channel_allowed(key):
                add_log(username, f"  → [LinkedIn #{slot}] Skipped (not in target channels)", "info")
                continue
            ok = publish_to_linkedin_slot(username, slot, text, image_url)
            if ok:
                success += 1

    if cfg.get("facebook_access_token"):
        key = "facebook_1"
        if not channel_allowed(key):
            add_log(username, "  → [Facebook] Skipped (not in target channels)", "info")
        else:
            ok = publish_to_facebook(username, text, image_url)
            if ok:
                success += 1

    if cfg.get("instagram_access_token"):
        key = "instagram_1"
        if not channel_allowed(key):
            add_log(username, "  → [Instagram] Skipped (not in target channels)", "info")
        elif not image_url:
            add_log(username, "  → [Instagram] Skipped — Instagram requires an image", "warn")
        else:
            ok = publish_to_instagram(username, text, image_url)
            if ok:
                success += 1

    any_connected = any([
        cfg.get("linkedin_1_access_token"),
        cfg.get("linkedin_2_access_token"),
        cfg.get("linkedin_3_access_token"),
        cfg.get("facebook_access_token"),
        cfg.get("instagram_access_token"),
    ])
    if success == 0 and not any_connected:
        add_log(username, "No channels connected — go to Setup to connect accounts.", "error")

    return success

# Keep backward-compat alias
def publish_to_all(username, text, image_url=None):
    return publish_to_channels(username, text, image_url, target_channels=None)

# ─── REVIEW QUEUE ─────────────────────────────────────────────────────────────
def add_to_review_queue(username, subject, post_text, image_url, target_channels):
    """Add a generated post to the review queue and notify by email."""
    review_id  = secrets.token_hex(8)
    auto_publish_at = (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"
    item = {
        "id":               review_id,
        "subject":          subject,
        "post_text":        post_text,
        "image_url":        image_url,
        "target_channels":  target_channels,  # list of channel keys
        "created_at":       datetime.utcnow().isoformat() + "Z",
        "auto_publish_at":  auto_publish_at,
        "status":           "pending",  # pending | published | discarded
    }
    queue = load_review_queue(username)
    queue.append(item)
    save_review_queue(username, queue)
    add_log(username, f"Post added to review queue (ID: {review_id}) — auto-publishes in 1h", "ok")
    # Send email notification in background
    threading.Thread(
        target=send_review_email,
        args=(username, review_id, subject, post_text),
        daemon=True
    ).start()
    return review_id


def auto_publish_reviewer():
    """Background loop — auto-publishes pending review items after 1 hour."""
    print("[REVIEWER] Auto-publish reviewer started")
    while True:
        try:
            now = datetime.utcnow()
            for uname in load_users():
                queue = load_review_queue(uname)
                changed = False
                for item in queue:
                    if item["status"] != "pending":
                        continue
                    auto_at_str = item.get("auto_publish_at", "")
                    if not auto_at_str:
                        continue
                    try:
                        auto_at = datetime.fromisoformat(auto_at_str.rstrip("Z"))
                    except Exception:
                        continue
                    if now >= auto_at:
                        add_log(uname, f"Auto-publishing review item {item['id']}: {item['subject'][:40]}", "info")
                        sent = publish_to_channels(
                            uname,
                            item["post_text"],
                            item.get("image_url"),
                            item.get("target_channels")
                        )
                        item["status"] = "published"
                        item["published_at"] = now.isoformat() + "Z"
                        item["published_channels"] = sent
                        changed = True
                        state = get_state(uname)
                        state["today_count"] = state.get("today_count", 0) + 1
                        state["total_run"]   = state.get("total_run", 0) + sent
                        add_log(uname, f"Auto-published post {item['id']} to {sent} channel(s) ✓", "ok")
                if changed:
                    save_review_queue(uname, queue)
        except Exception as e:
            print(f"[REVIEWER] Error: {e}")
        time.sleep(30)

# ─── BATCH ENGINE ─────────────────────────────────────────────────────────────
def run_batch(username, triggered_by="scheduler"):
    state = get_state(username)
    if state["running"]:
        add_log(username, "Batch already running.", "warn")
        return

    state["running"]     = True
    state["status"]      = "running"
    state["today_count"] = 0
    add_log(username, f"Batch started (trigger: {triggered_by})", "info")

    subjects_file = user_subjects_path(username)
    if not os.path.exists(subjects_file):
        add_log(username, "Queue is empty! Add subjects from dashboard.", "warn")
        state["running"] = False; state["status"] = "waiting"; return

    with open(subjects_file, "r", encoding="utf-8") as f:
        all_subjects = [l.strip() for l in f if l.strip()]

    if not all_subjects:
        add_log(username, "Queue is empty!", "warn")
        state["running"] = False; state["status"] = "waiting"; return

    batch = all_subjects[:1]
    with open(subjects_file, "w", encoding="utf-8") as f:
        f.write("\n".join(all_subjects[1:]))

    add_log(username, f"Processing 1 subject. {len(all_subjects)-1} remaining in queue.", "info")

    for j, subject in enumerate(batch):
        manual_image_url = None
        base_subject     = subject

        # Parse per-subject target channels  e.g. "| CHANNELS: linkedin_1,facebook_1"
        target_channels = None
        if "| CHANNELS:" in subject:
            parts          = subject.split("| CHANNELS:")
            base_subject   = parts[0].strip()
            channels_str   = parts[1].strip()
            target_channels = [c.strip() for c in channels_str.split(",") if c.strip()]

        if "| IMG:" in base_subject:
            parts            = base_subject.split("| IMG:")
            base_subject     = parts[0].strip()
            manual_image_url = parts[1].strip()

        add_log(username, f"[{j+1}/{len(batch)}] {base_subject[:50]}", "info")

        post_text = generate_post(username, base_subject)
        if not post_text:
            continue
        add_log(username, f"[{j+1}/{len(batch)}] Post generated ✓", "ok")

        image_url = None
        if manual_image_url:
            if validate_image_url(manual_image_url):
                image_url = manual_image_url
                add_log(username, f"[{j+1}/{len(batch)}] Using uploaded image ✓", "ok")
            else:
                add_log(username, f"[{j+1}/{len(batch)}] Uploaded image unreachable — sending without image", "warn")
        elif "(create image)" in base_subject.lower():
            image_url = get_image_url(username, base_subject)
            if image_url:
                add_log(username, f"[{j+1}/{len(batch)}] Image fetched ✓", "ok")

        # ── SEND TO REVIEW QUEUE instead of publishing directly ──
        review_id = add_to_review_queue(username, base_subject, post_text, image_url, target_channels)
        add_log(username, f"[{j+1}/{len(batch)}] Sent to review queue (ID: {review_id}) ✓", "ok")
        time.sleep(10)

    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["running"]  = False
    state["status"]   = "waiting"
    add_log(username, f"Batch complete! Posts in review queue — check dashboard.", "ok")
    save_state(username, state)

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def scheduler_loop():
    print("[SCHEDULER] Started — per-user time slots")
    fired_today = {}
    while True:
        now_utc = datetime.utcnow()
        for uname in load_users():
            cfg      = load_config(uname)
            offset   = cfg.get("utc_offset_hours", 0)
            user_now = now_utc + timedelta(hours=offset)
            cur_hour = user_now.hour
            cur_min  = user_now.minute
            cur_sec  = user_now.second
            today    = user_now.date()

            if uname not in fired_today or fired_today[uname].get("date") != today:
                fired_today[uname] = {"date": today, "hours": set()}

            raw_slots = cfg.get("time_slots", [{"h": TARGET_HOUR, "m": 0}])
            if not isinstance(raw_slots, list) or not raw_slots:
                raw_slots = [{"h": TARGET_HOUR, "m": 0}]
            slots = []
            for s in raw_slots:
                if isinstance(s, dict):
                    slots.append((int(s.get("h", 8)), int(s.get("m", 0))))
                elif isinstance(s, (int, float)):
                    slots.append((int(s), 0))

            for (sh, sm) in slots:
                fire_key = (sh, sm)
                if (cur_hour == sh and cur_min == sm and cur_sec < 5 and
                        fire_key not in fired_today[uname]["hours"]):
                    fired_today[uname]["hours"].add(fire_key)
                    add_log(uname, f"Scheduler fired at local {sh:02d}:{sm:02d} (UTC{offset:+.1f}h)", "info")
                    threading.Thread(target=run_batch, args=(uname, "scheduler"), daemon=True).start()

        time.sleep(1)

def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            render  = os.environ.get("RENDER_EXTERNAL_URL")
            port    = os.environ.get("PORT", 5000)
            url = f"https://{railway}" if railway else (render if render else f"http://localhost:{port}")
            requests.get(f"{url}/ping", timeout=10)
        except Exception:
            pass
        time.sleep(600)

# ════════════════════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/")
def page_login():     return load_html("index.html")
@app.route("/setup")
def page_setup():     return load_html("setup.html")
@app.route("/profile")
def page_profile():   return load_html("profile.html")
@app.route("/dashboard")
def page_dashboard(): return load_html("dashboard.html")

@app.route("/privacy")
def privacy():
    return load_html("privacy.html")

@app.route("/data-deletion")
def data_deletion():
    return load_html("data-deletion.html")

@app.route("/instagram/webhook", methods=["GET", "POST"])
def instagram_webhook():
    if request.method == "GET":
        verify_token = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "myverifytoken123")
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        return "Forbidden", 403
    return "OK", 200


@app.route("/api/test_email")
@require_auth
def test_email(username):
    """Send a test email to verify SMTP config."""
    users = load_users()
    to_email = users.get(username, {}).get("email", "")
    if not to_email:
        return jsonify({"ok": False, "message": "No email on account"})
    html = f"""
    <div style="font-family:sans-serif;padding:24px;background:#05030f;color:#ede8ff;border-radius:12px;">
      <h2 style="color:#b085ff;">✓ LinkedIn Agent — Email Test</h2>
      <p style="color:rgba(200,185,255,0.7);margin-top:12px;">
        SMTP is working correctly.<br>
        Host: {SMTP_HOST}:{SMTP_PORT}<br>
        From: {SMTP_FROM or SMTP_USER}
      </p>
    </div>
    """
    ok = send_email(to_email, "✓ LinkedIn Agent — SMTP Test", html)
    return jsonify({
        "ok": ok,
        "message": f"Email {'sent' if ok else 'FAILED'} to {to_email}",
        "smtp_host": SMTP_HOST,
        "smtp_port": SMTP_PORT,
        "smtp_user": SMTP_USER,
        "to": to_email
    })


@app.route("/ping")
def ping():
    utc_now = datetime.utcnow()
    return jsonify({"status": "alive", "utc_time": utc_now.strftime("%H:%M:%S"),
                    "utc_iso": utc_now.isoformat() + "Z"})

# ════════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES  — now use EMAIL instead of username
# ════════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════════
#  GOOGLE OAUTH — Sign In / Sign Up
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/auth/google")
def google_auth():
    if not GOOGLE_CLIENT_ID:
        return jsonify({"ok": False, "message": "GOOGLE_CLIENT_ID not set in environment"}), 400
    state_token = secrets.token_hex(16)
    session["google_state"] = state_token
    # Pass along utc_offset_hours via state so we can set timezone on first login
    utc_offset = request.args.get("utc_offset_hours", "")
    state_payload = f"{state_token}:{utc_offset}"
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state_payload,
        "prompt":        "select_account",
        "access_type":   "online",
    }
    oauth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return jsonify({"ok": True, "auth_url": oauth_url})


@app.route("/api/auth/google/callback")
def google_callback():
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    error = request.args.get("error", "")

    if error:
        return _google_result_page(False, f"Google sign-in was cancelled or denied: {error}")

    if not code:
        return _google_result_page(False, "Missing authorization code from Google.")

    # Parse utc_offset from state if present
    utc_offset = None
    try:
        parts = state.split(":", 1)
        if len(parts) > 1 and parts[1]:
            utc_offset = float(parts[1])
    except Exception:
        pass

    try:
        # Exchange code for access token
        token_res = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri":  GOOGLE_REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
            timeout=15
        ).json()

        access_token = token_res.get("access_token")
        if not access_token:
            return _google_result_page(False, f"Token exchange failed: {token_res.get('error_description', str(token_res))}")

        # Get user info
        userinfo = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        ).json()

        email          = (userinfo.get("email") or "").strip().lower()
        email_verified = userinfo.get("email_verified", False)
        name           = userinfo.get("name", "")

        if not email:
            return _google_result_page(False, "Could not retrieve email from Google account.")
        if not email_verified:
            return _google_result_page(False, "Your Google email is not verified. Please verify it with Google first.")

        # Find or create user
        username, user_data = find_user_by_email(email)
        users = load_users()
        is_new = False

        if not username:
            # Create new account — Google-authenticated, no password
            is_new   = True
            username = re.sub(r'[^a-z0-9_]', '_', email.split("@")[0].lower())[:20]
            base_uname = username
            counter = 1
            while username in users:
                username = f"{base_uname}_{counter}"
                counter += 1

            # Generate a random unusable password hash (Google-only account)
            random_pass = secrets.token_hex(32)
            h, salt = hash_password(random_pass)
            users[username] = {
                "email":         email,
                "password_hash": h,
                "salt":          salt,
                "name":          name,
                "auth_provider": "google",
                "has_password":  False,
                "created_at":    datetime.now().isoformat()
            }
            save_users(users)
            os.makedirs(user_dir(username), exist_ok=True)

            if utc_offset is not None:
                cfg = load_config(username)
                cfg["utc_offset_hours"] = utc_offset
                save_config(username, cfg)
        else:
            # Existing account — mark that Google is linked (without breaking existing password login)
            if not users[username].get("auth_provider"):
                users[username]["auth_provider"] = "google"
                save_users(users)

        # Log the user in
        session.clear()
        session["username"] = username
        session.permanent   = True

        cfg = load_config(username)
        has_config = bool(cfg.get("groq_api_key"))
        redirect_to = "/dashboard" if has_config else "/setup"
        return _google_result_page(True, "Signed in with Google!", redirect_to=redirect_to)

    except Exception as e:
        return _google_result_page(False, f"Google sign-in error: {e}")


def _google_result_page(success, message, redirect_to="/"):
    """Returns an HTML page that stores the session marker and redirects."""
    if success:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#05030f;color:#3dffc0;font-family:'Segoe UI',sans-serif;text-align:center;}}.box{{padding:40px;border:1px solid rgba(61,255,192,0.3);border-radius:20px;background:rgba(61,255,192,0.05);}}.ico{{font-size:48px;margin-bottom:16px;}}h2{{font-size:20px;margin-bottom:8px;}}p{{font-size:13px;color:rgba(200,185,255,0.6);}}</style></head>
<body><div class="box"><div class="ico">✓</div><h2>{message}</h2><p>Redirecting...</p></div>
<script>
localStorage.setItem('li_session', JSON.stringify({{expiresAt: Date.now() + 8*3600*1000}}));
setTimeout(function(){{window.location.href='{redirect_to}';}}, 600);
</script></body></html>"""
    else:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#05030f;color:#ff6060;font-family:'Segoe UI',sans-serif;text-align:center;}}.box{{padding:40px;border:1px solid rgba(255,96,96,0.3);border-radius:20px;background:rgba(255,96,96,0.05);max-width:420px;}}.ico{{font-size:48px;margin-bottom:16px;}}h2{{font-size:18px;margin-bottom:8px;}}p{{font-size:12px;color:rgba(255,150,150,0.8);margin-top:8px;}}a{{display:inline-block;margin-top:20px;padding:10px 24px;background:rgba(255,96,96,0.15);border:1px solid rgba(255,96,96,0.3);color:#ff6060;border-radius:10px;text-decoration:none;font-size:13px;}}</style></head>
<body><div class="box"><div class="ico">✕</div><h2>Google Sign-In Failed</h2><p>{message}</p><a href="/">← Back to Login</a></div></body></html>"""


@app.route("/api/register", methods=["POST"])
def register():
    data     = request.get_json()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"ok": False, "message": "Email and password required."}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({"ok": False, "message": "Invalid email address."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."}), 400

    # Check email uniqueness
    existing_user, _ = find_user_by_email(email)
    if existing_user:
        return jsonify({"ok": False, "message": "Email already registered."}), 409

    # Generate a safe internal username from email
    username = re.sub(r'[^a-z0-9_]', '_', email.split("@")[0].lower())[:20]
    # Ensure uniqueness
    users = load_users()
    base_uname = username
    counter = 1
    while username in users:
        username = f"{base_uname}_{counter}"
        counter += 1

    h, salt = hash_password(password)
    users[username] = {
        "email":         email,
        "password_hash": h,
        "salt":          salt,
        "has_password":  True,
        "created_at":    datetime.now().isoformat()
    }
    save_users(users)
    os.makedirs(user_dir(username), exist_ok=True)

    utc_offset = data.get("utc_offset_hours")
    if utc_offset is not None:
        cfg = load_config(username)
        cfg["utc_offset_hours"] = float(utc_offset)
        save_config(username, cfg)

    session.clear()
    session["username"] = username
    session.permanent   = True

    cfg = load_config(username)
    has_config = bool(cfg.get("groq_api_key"))
    return jsonify({"ok": True, "username": username, "email": email,
                    "has_config": has_config, "message": "Account created!"})

@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    username, user_data = find_user_by_email(email)
    if not username:
        return jsonify({"ok": False, "message": "Invalid email or password."}), 401
    if not verify_password(password, user_data["password_hash"], user_data["salt"]):
        return jsonify({"ok": False, "message": "Invalid email or password."}), 401

    utc_offset = data.get("utc_offset_hours")
    session.clear()
    session["username"] = username
    session.permanent   = True

    if utc_offset is not None:
        cfg = load_config(username)
        cfg["utc_offset_hours"] = float(utc_offset)
        save_config(username, cfg)

    cfg = load_config(username)
    has_config = bool(cfg.get("groq_api_key"))
    return jsonify({"ok": True, "username": username, "email": email, "has_config": has_config})

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def me():
    username = session.get("username")
    if not username:
        return jsonify({"ok": False, "authenticated": False})
    users = load_users()
    u = users.get(username, {})
    email = u.get("email", "")
    has_password = user_has_password(u)
    cfg = load_config(username)
    has_config = bool(cfg.get("groq_api_key"))
    return jsonify({"ok": True, "authenticated": True, "username": username,
                    "email": email, "has_config": has_config,
                    "has_password": has_password,
                    "auth_provider": u.get("auth_provider", "local")})

# ════════════════════════════════════════════════════════════════════════════════
#  PROFILE / ACCOUNT MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/profile/update_email", methods=["POST"])
@require_auth
def update_email(username):
    data      = request.get_json()
    new_email = (data.get("email") or "").strip().lower()
    password  = data.get("password") or ""

    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', new_email):
        return jsonify({"ok": False, "message": "Invalid email address."}), 400

    users = load_users()
    u     = users.get(username, {})
    has_password = user_has_password(u)

    if has_password:
        if not password:
            return jsonify({"ok": False, "message": "Current password is required."}), 400
        if not verify_password(password, u["password_hash"], u["salt"]):
            return jsonify({"ok": False, "message": "Current password is incorrect."}), 401
    # Google-only accounts (no password set) skip password verification —
    # they're already authenticated via session + Google OAuth

    # Check not already taken by another user
    existing, _ = find_user_by_email(new_email)
    if existing and existing != username:
        return jsonify({"ok": False, "message": "Email already in use."}), 409

    users[username]["email"] = new_email
    save_users(users)
    return jsonify({"ok": True, "message": "Email updated successfully.", "email": new_email})


@app.route("/api/profile/update_password", methods=["POST"])
@require_auth
def update_password(username):
    data         = request.get_json()
    current_pass = data.get("current_password") or ""
    new_pass     = data.get("new_password") or ""

    if len(new_pass) < 6:
        return jsonify({"ok": False, "message": "New password must be at least 6 characters."}), 400

    users = load_users()
    u     = users.get(username, {})
    has_password = user_has_password(u)

    if has_password:
        if not current_pass:
            return jsonify({"ok": False, "message": "Current password is required."}), 400
        if not verify_password(current_pass, u["password_hash"], u["salt"]):
            return jsonify({"ok": False, "message": "Current password is incorrect."}), 401
    # Google-only accounts: no current password needed — setting a password for the first time

    h, salt = hash_password(new_pass)
    users[username]["password_hash"] = h
    users[username]["salt"]          = salt
    users[username]["has_password"]  = True
    save_users(users)
    msg = "Password set successfully! You can now also log in with email + password." if not has_password else "Password updated successfully."
    return jsonify({"ok": True, "message": msg, "has_password": True})


@app.route("/api/profile/delete", methods=["POST"])
@require_auth
def delete_account(username):
    data     = request.get_json()
    password = data.get("password") or ""
    confirm  = data.get("confirm") or ""

    if confirm != "DELETE":
        return jsonify({"ok": False, "message": "Type DELETE to confirm."}), 400

    users = load_users()
    u     = users.get(username, {})
    has_password = user_has_password(u)

    if has_password:
        if not password:
            return jsonify({"ok": False, "message": "Password is required."}), 400
        if not verify_password(password, u["password_hash"], u["salt"]):
            return jsonify({"ok": False, "message": "Password is incorrect."}), 401
    # Google-only accounts: session + DELETE confirmation is sufficient

    # Remove user data directory
    import shutil
    udir = os.path.join(USER_DATA_DIR, username)
    if os.path.exists(udir):
        shutil.rmtree(udir)

    del users[username]
    save_users(users)

    # Clean in-memory state
    with _state_lock:
        _states.pop(username, None)

    session.clear()
    return jsonify({"ok": True, "message": "Account deleted."})

# ════════════════════════════════════════════════════════════════════════════════
#  CONFIG ROUTES
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/config", methods=["GET"])
@require_auth
def get_config(username):
    cfg = load_config(username)
    masked = {}
    token_keys = {
        "linkedin_1_access_token", "linkedin_2_access_token", "linkedin_3_access_token",
        "facebook_access_token", "instagram_access_token",
    }
    for k, v in cfg.items():
        if k in token_keys:
            masked[k] = "connected" if v else ""
        elif isinstance(v, str) and v and len(v) > 8 and k in SENSITIVE_FIELDS:
            masked[k] = "*" * (len(v) - 4) + v[-4:]
        else:
            masked[k] = v
    return jsonify({"ok": True, "config": masked})

@app.route("/api/config", methods=["POST"])
@require_auth
def save_config_route(username):
    data = request.get_json()
    cfg  = load_config(username)
    for field in ["groq_api_key", "serpapi_key"]:
        val = (data.get(field) or "").strip()
        if val and "*" not in val:
            cfg[field] = val
    if "time_slots" in data:
        raw = data["time_slots"]
        if isinstance(raw, list):
            cleaned = []
            seen    = set()
            for s in raw[:3]:
                if isinstance(s, dict):
                    h = max(0, min(23, int(s.get("h", 8))))
                    m = max(0, min(59, int(s.get("m", 0))))
                elif isinstance(s, (int, float)):
                    h, m = max(0, min(23, int(s))), 0
                else:
                    continue
                key = (h, m)
                if key not in seen:
                    seen.add(key)
                    cleaned.append({"h": h, "m": m})
            cleaned.sort(key=lambda x: x["h"]*60 + x["m"])
            cfg["time_slots"] = cleaned
    save_config(username, cfg)
    add_log(username, "Config updated ✓", "ok")
    return jsonify({"ok": True, "message": "Configuration saved!"})

# ════════════════════════════════════════════════════════════════════════════════
#  CHANNELS STATUS
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/channels")
def get_channels():
    username = session.get("username")
    if not username:
        return jsonify({"ok": False, "authenticated": False}), 401
    cfg = load_config(username)

    def token_expired(exp_key):
        exp = cfg.get(exp_key)
        if not exp:
            return False
        try:
            return datetime.utcnow() > datetime.fromisoformat(exp)
        except Exception:
            return False

    linkedin = {}
    for slot in [1, 2, 3]:
        t_key  = f"linkedin_{slot}_access_token"
        e_key  = f"linkedin_{slot}_token_expires"
        n_key  = f"linkedin_{slot}_name"
        a_key  = f"linkedin_{slot}_account_type"
        linkedin[slot] = {
            "connected": bool(cfg.get(t_key)),
            "expired":   token_expired(e_key),
            "name":      cfg.get(n_key, ""),
            "expires":   cfg.get(e_key, ""),
            "account_type": cfg.get(a_key, "personal"),
            "env_set":   bool(LI_CLIENT_ID and LI_CLIENT_SECRET),
        }

    facebook = {
        1: {
            "connected": bool(cfg.get("facebook_access_token")),
            "page_name": cfg.get("facebook_page_name", ""),
            "page_id":   cfg.get("facebook_page_id", ""),
            "env_set":   bool(FB_APP_ID and FB_APP_SECRET),
        }
    }

    instagram = {
        1: {
            "connected":      bool(cfg.get("instagram_access_token")),
            "username":       cfg.get("instagram_username", ""),
            "via_facebook":   cfg.get("instagram_via_facebook", False),
            "env_set":        bool(FB_APP_ID and FB_APP_SECRET),
            "direct_env_set": bool(IG_APP_ID and IG_APP_SECRET),
        }
    }

    return jsonify({
        "ok":        True,
        "linkedin":  linkedin,
        "facebook":  facebook,
        "instagram": instagram,
    })

# ════════════════════════════════════════════════════════════════════════════════
#  OAUTH CALLBACK HELPERS  (unchanged from original)
# ════════════════════════════════════════════════════════════════════════════════

def _callback_page(success, message="", msg_key="", platform="linkedin", slot=1):
    if success:
        event = msg_key or f"channel_connected:{platform}:{slot}"
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#05030f;color:#3dffc0;font-family:'Segoe UI',sans-serif;text-align:center;}}.box{{padding:40px;border:1px solid rgba(61,255,192,0.3);border-radius:20px;background:rgba(61,255,192,0.05);}}.ico{{font-size:48px;margin-bottom:16px;}}h2{{font-size:20px;margin-bottom:8px;}}p{{font-size:13px;color:rgba(200,185,255,0.6);}}</style></head>
<body><div class="box"><div class="ico">✓</div><h2>{message or 'Connected!'}</h2><p>This window will close automatically...</p></div>
<script>try{{window.opener&&window.opener.postMessage('{event}','*');}}catch(e){{}}setTimeout(function(){{window.close();}},1200);</script></body></html>"""
    else:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#05030f;color:#ff6060;font-family:'Segoe UI',sans-serif;text-align:center;}}.box{{padding:40px;border:1px solid rgba(255,96,96,0.3);border-radius:20px;background:rgba(255,96,96,0.05);max-width:420px;}}.ico{{font-size:48px;margin-bottom:16px;}}h2{{font-size:18px;margin-bottom:8px;}}p{{font-size:12px;color:rgba(200,185,255,0.5);margin-top:16px;}}button{{margin-top:20px;padding:10px 24px;background:rgba(255,96,96,0.15);border:1px solid rgba(255,96,96,0.3);color:#ff6060;border-radius:10px;cursor:pointer;font-size:13px;}}</style></head>
<body><div class="box"><div class="ico">✕</div><h2>Connection Failed</h2><div style="font-size:12px;color:rgba(255,150,150,0.8);margin-top:8px;">{message}</div><p>You can close this window and try again.</p><button onclick="window.close()">Close</button></div></body></html>""", 400


# ════════════════════════════════════════════════════════════════════════════════
#  ALL OAUTH ROUTES — unchanged, just copied verbatim
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/linkedin")
@require_auth
def linkedin_auth(username):
    if not LI_CLIENT_ID:
        return jsonify({"ok": False, "message": "LINKEDIN_CLIENT_ID not set in environment"}), 400
    slot = request.args.get("slot", "1")
    try:
        slot = int(slot)
    except ValueError:
        slot = 1
    slot = max(1, min(3, slot))
    state_token   = secrets.token_hex(16)
    state_payload = f"{username}:{slot}:{state_token}"
    session[f"li_state_{slot}"] = state_token
    session["li_active_slot"]   = slot
    params = {
        "response_type": "code", "client_id": LI_CLIENT_ID,
        "redirect_uri": LI_REDIRECT_URI, "state": state_payload,
        "scope": "openid profile email", "prompt": "login", "login_hint": "",
    }
    oauth_url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params)
    return jsonify({"ok": True, "auth_url": oauth_url})


def _li_account_picker_page(username, slot, access_token, expires_in, personal_name, personal_id, orgs, oauth_url=''):
    token_key = f"li_pending_{username}_{slot}"
    session[token_key] = {"access_token": access_token, "expires_in": expires_in, "personal_id": personal_id, "personal_name": personal_name}
    token_key2 = f"li_oauth_{username}_{slot}"
    session[token_key2] = oauth_url
    safe_name = personal_name.replace("'", "\\'")
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Choose LinkedIn Account</title>
<style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{min-height:100vh;background:#05030f;color:#ede8ff;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;padding:24px;}}.wrap{{width:100%;max-width:440px;}}.hdr{{text-align:center;margin-bottom:24px;}}.hdr h2{{font-size:20px;font-weight:800;letter-spacing:-0.5px;margin-bottom:6px;}}.hdr p{{font-size:12px;color:rgba(200,185,255,0.55);}}.slot-badge{{display:inline-block;background:rgba(176,133,255,0.15);border:1px solid rgba(176,133,255,0.3);color:#b085ff;border-radius:20px;padding:4px 14px;font-size:11px;margin-bottom:12px;}}.section-label{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:rgba(200,185,255,0.4);margin-bottom:10px;}}.acct-card{{display:flex;align-items:center;gap:14px;padding:14px 16px;border:1.5px solid rgba(160,120,255,0.2);border-radius:14px;background:rgba(255,255,255,0.03);margin-bottom:8px;cursor:pointer;transition:all 0.2s;text-decoration:none;color:inherit;}}.acct-card:hover{{border-color:rgba(176,133,255,0.5);background:rgba(176,133,255,0.07);}}.acct-ico{{font-size:26px;flex-shrink:0;}}.acct-info{{flex:1;min-width:0;}}.acct-info strong{{display:block;font-size:14px;font-weight:700;}}.acct-info small{{display:block;font-size:11px;color:rgba(200,185,255,0.5);margin-top:2px;}}.acct-arrow{{color:rgba(176,133,255,0.5);font-size:16px;flex-shrink:0;}}.loading{{display:none;text-align:center;padding:20px;font-size:13px;color:rgba(200,185,255,0.5);}}.spin{{display:inline-block;animation:sp 0.8s linear infinite;}}@keyframes sp{{to{{transform:rotate(360deg);}}}}.signout-row{{display:flex;align-items:center;justify-content:center;margin-top:20px;}}.signout-btn{{display:flex;align-items:center;gap:8px;padding:11px 22px;border-radius:11px;border:1.5px solid rgba(255,96,96,0.25);background:rgba(255,96,96,0.07);color:rgba(255,130,130,0.9);font-size:13px;font-weight:600;cursor:pointer;font-family:'Segoe UI',sans-serif;}}</style></head>
<body><div class="wrap">
<div class="hdr"><div class="slot-badge">LinkedIn Channel {slot}</div><h2>Connect your account</h2><p>Connecting your personal LinkedIn profile</p></div>
<div class="section-label">Personal Profile</div>
<label class="acct-card" onclick="pick('personal','{personal_id}','{safe_name}')"><span class="acct-ico">👤</span><span class="acct-info"><strong>{personal_name}</strong><small>Personal LinkedIn Profile</small></span><span class="acct-arrow">→</span></label>
<div class="loading" id="loadingBox"><span class="spin">⟳</span> Connecting...</div>
<div class="signout-row"><button class="signout-btn" onclick="doSignOut()">↩ Sign out &amp; use a different account</button></div></div>
<script>
async function pick(acct_type,acct_id,acct_name){{document.querySelectorAll('.acct-card').forEach(c=>c.style.pointerEvents='none');document.getElementById('loadingBox').style.display='block';try{{const res=await fetch('/api/auth/linkedin/pick',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{username:'{username}',slot:{slot},acct_type:acct_type,acct_id:acct_id,acct_name:acct_name}})}});const data=await res.json();if(data.ok){{window.opener&&window.opener.postMessage('channel_connected:linkedin:{slot}','*');window.close();}}else{{alert('Error: '+(data.message||'Unknown error'));document.querySelectorAll('.acct-card').forEach(c=>c.style.pointerEvents='');document.getElementById('loadingBox').style.display='none';}}}}catch(e){{alert('Network error: '+e.message);document.querySelectorAll('.acct-card').forEach(c=>c.style.pointerEvents='');document.getElementById('loadingBox').style.display='none';}}}}
function doSignOut(){{window.location.href='/api/auth/linkedin/signout/{username}/{slot}';}}
</script></body></html>"""


@app.route("/api/auth/linkedin/signout/<username>/<int:slot>")
def linkedin_signout(username, slot):
    token_key = f"li_oauth_{username}_{slot}"
    oauth_url = session.get(token_key, "")
    if not oauth_url:
        return redirect("/setup")
    safe_oauth = oauth_url.replace("\\", "\\\\").replace("'", "\\'")
    return f"""<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Signing out...</title>
<style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{min-height:100vh;background:#05030f;color:#ede8ff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;text-align:center;padding:28px;gap:16px;}}h2{{font-size:19px;font-weight:800;}}p{{font-size:13px;color:rgba(200,185,255,.55);max-width:300px;line-height:1.7;}}.spin{{font-size:36px;animation:sp 1s linear infinite;display:inline-block;}}@keyframes sp{{to{{transform:rotate(360deg);}}}}</style></head>
<body><div class='spin'>⟳</div><h2>Signing out of LinkedIn...</h2><p>Please wait...</p>
<script>sessionStorage.setItem('li_reauth_url','{safe_oauth}');setTimeout(function(){{window.location.href='https://www.linkedin.com/m/logout';}},800);</script></body></html>"""


@app.route("/api/auth/linkedin/reauth")
def linkedin_reauth():
    return """<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Sign in</title>
<style>*{box-sizing:border-box;margin:0;padding:0;}body{min-height:100vh;background:#05030f;color:#ede8ff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;text-align:center;padding:28px;gap:16px;}h2{font-size:19px;font-weight:800;}p{font-size:13px;color:rgba(200,185,255,.55);max-width:300px;line-height:1.7;}.btn{padding:14px 28px;border-radius:12px;border:none;background:linear-gradient(135deg,#0077b5,#0099cc);color:#fff;font-size:15px;font-weight:700;cursor:pointer;}</style></head>
<body><h2>✓ Signed out of LinkedIn</h2><p>Now sign in with the account you want to connect.</p><button class='btn' id='btn'>Sign In with LinkedIn →</button>
<script>var u=sessionStorage.getItem('li_reauth_url');document.getElementById('btn').onclick=function(){if(u){sessionStorage.removeItem('li_reauth_url');window.location.href=u;}else{window.close();}};</script></body></html>"""


@app.route("/api/auth/linkedin/pick", methods=["POST"])
def linkedin_pick():
    data      = request.get_json()
    username  = data.get("username", "")
    slot      = int(data.get("slot", 1))
    acct_type = data.get("acct_type", "personal")
    acct_id   = data.get("acct_id", "")
    acct_name = data.get("acct_name", "")
    users = load_users()
    if username not in users:
        return jsonify({"ok": False, "message": "Unknown user"}), 400
    token_key = f"li_pending_{username}_{slot}"
    pending   = session.get(token_key)
    if not pending:
        return jsonify({"ok": False, "message": "Session expired — please reconnect"}), 400
    access_token = pending["access_token"]
    expires_in   = pending["expires_in"]
    cfg = load_config(username)
    urn = f"urn:li:organization:{acct_id}" if acct_type == "organization" else f"urn:li:person:{acct_id}"
    cfg[f"linkedin_{slot}_access_token"]  = access_token
    cfg[f"linkedin_{slot}_urn"]           = urn
    cfg[f"linkedin_{slot}_name"]          = acct_name
    cfg[f"linkedin_{slot}_account_type"]  = acct_type
    cfg[f"linkedin_{slot}_token_expires"] = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
    save_config(username, cfg)
    session.pop(token_key, None)
    add_log(username, f"LinkedIn slot {slot} → {acct_type} '{acct_name}' connected ✓", "ok")
    return jsonify({"ok": True})


@app.route("/api/auth/start")
def auth_start():
    oauth_url = request.args.get("oauth", "")
    if not oauth_url:
        return "Missing oauth param", 400
    return redirect(urllib.parse.unquote(oauth_url))


@app.route("/api/auth/linkedin/callback")
def linkedin_callback():
    code      = request.args.get("code", "")
    state_raw = request.args.get("state", "")
    error     = request.args.get("error", "")
    if error:
        return _callback_page(False, f"LinkedIn denied access: {request.args.get('error_description', error)}")
    try:
        parts = state_raw.split(":", 2)
        if len(parts) < 2:
            raise ValueError("Malformed state")
        username    = parts[0]
        slot        = int(parts[1])
        state_token = parts[2] if len(parts) > 2 else ""
    except Exception:
        return _callback_page(False, "Invalid OAuth state — please log in and try again.")
    if not username:
        return _callback_page(False, "Session lost — please log in again and retry.")
    users = load_users()
    if username not in users:
        return _callback_page(False, "Unknown user — please log in and try again.")
    try:
        token_res = requests.post(
            "https://www.linkedin.com/oauth/v2/accessToken",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": LI_REDIRECT_URI,
                  "client_id": LI_CLIENT_ID, "client_secret": LI_CLIENT_SECRET},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15
        ).json()
        access_token = token_res.get("access_token")
        expires_in   = token_res.get("expires_in", 5184000)
        if not access_token:
            return _callback_page(False, f"Token exchange failed: {token_res.get('error_description', str(token_res))}")
        profile  = requests.get("https://api.linkedin.com/v2/userinfo",
                                 headers={"Authorization": f"Bearer {access_token}"}, timeout=10).json()
        li_id    = profile.get("sub", "")
        li_name  = (profile.get("name") or f"{profile.get('given_name','')} {profile.get('family_name','')}".strip() or "LinkedIn User")
        orgs = []
        try:
            acl_res = requests.get("https://api.linkedin.com/v2/organizationAcls",
                headers={"Authorization": f"Bearer {access_token}", "X-Restli-Protocol-Version": "2.0.0"},
                params={"q": "roleAssignee", "count": 50}, timeout=12).json()
            org_ids = []
            seen_ids = set()
            for elem in acl_res.get("elements", []):
                target = str(elem.get("organizationalTarget", ""))
                if "organization:" in target:
                    org_id = target.split("organization:")[-1].strip()
                    if org_id and org_id not in seen_ids:
                        seen_ids.add(org_id); org_ids.append(org_id)
            for org_id in org_ids[:15]:
                try:
                    org_info = requests.get(f"https://api.linkedin.com/v2/organizations/{org_id}",
                        headers={"Authorization": f"Bearer {access_token}", "X-Restli-Protocol-Version": "2.0.0"}, timeout=8).json()
                    org_name = (org_info.get("localizedName") or (org_info.get("name") or {}).get("localized", {}).get("en_US") or
                                next(iter((org_info.get("name") or {}).get("localized", {}).values()), None) or f"Company Page {org_id}")
                    orgs.append({"id": org_id, "name": org_name})
                except Exception:
                    orgs.append({"id": org_id, "name": f"Company Page {org_id}"})
        except Exception as acl_err:
            add_log(username, f"LinkedIn ACL fetch failed: {acl_err}", "warn")
        token_key = f"li_pending_{username}_{slot}"
        session[token_key] = {"access_token": access_token, "expires_in": expires_in, "personal_id": li_id, "personal_name": li_name}
        session.modified = True
        _oauth_url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": LI_CLIENT_ID, "redirect_uri": LI_REDIRECT_URI,
            "state": state_raw, "scope": "openid profile email", "prompt": "login",
        })
        return _li_account_picker_page(username, slot, access_token, expires_in, li_name, li_id, orgs, oauth_url=_oauth_url)
    except Exception as e:
        add_log(username, f"LinkedIn slot {slot} OAuth error: {e}", "error")
        return _callback_page(False, f"Error: {e}")


@app.route("/api/auth/linkedin/disconnect", methods=["POST"])
@require_auth
def linkedin_disconnect(username):
    slot = request.args.get("slot", "1")
    try:
        slot = int(slot)
    except ValueError:
        slot = 1
    slot = max(1, min(3, slot))
    cfg = load_config(username)
    for k in [f"linkedin_{slot}_access_token", f"linkedin_{slot}_urn",
              f"linkedin_{slot}_name", f"linkedin_{slot}_token_expires"]:
        cfg.pop(k, None)
    save_config(username, cfg)
    add_log(username, f"LinkedIn slot {slot} disconnected", "warn")
    return jsonify({"ok": True})


@app.route("/api/auth/facebook")
@require_auth
def facebook_auth(username):
    if not FB_APP_ID:
        return jsonify({"ok": False, "message": "FACEBOOK_APP_ID not set in environment"}), 400
    state_token = secrets.token_hex(16)
    session["fb_state"] = state_token
    state = f"{username}:{state_token}"
    params = {"client_id": FB_APP_ID, "redirect_uri": FB_REDIRECT_URI, "state": state,
              "scope": "pages_manage_posts,pages_read_engagement,instagram_basic,instagram_content_publish",
              "auth_type": "rerequest"}
    oauth_url = "https://www.facebook.com/v19.0/dialog/oauth?" + urllib.parse.urlencode(params)
    session["fb_oauth_url"] = oauth_url
    session["fb_reauth_target"] = oauth_url
    return jsonify({"ok": True, "auth_url": oauth_url})

@app.route("/api/auth/loading")
def auth_loading():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Connecting...</title>
<style>*{margin:0;padding:0;box-sizing:border-box;}body{min-height:100vh;background:#05030f;color:#ede8ff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;gap:16px;}.spin{font-size:40px;animation:sp 1s linear infinite;display:inline-block;}p{font-size:13px;color:rgba(200,185,255,0.5);font-family:monospace;}@keyframes sp{to{transform:rotate(360deg);}}</style></head>
<body><div class="spin">⟳</div><p>Opening connection...</p></body></html>"""

@app.route("/api/auth/facebook/callback")
def facebook_callback():
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    error = request.args.get("error", "")
    if error:
        return _callback_page(False, f"Facebook denied access: {request.args.get('error_description', error)}")
    try:
        username, state_token = state.split(":", 1)
        if not username:
            raise ValueError("Empty username in state")
    except Exception:
        return _callback_page(False, "Invalid OAuth state — please log in and try again.")
    users = load_users()
    if username not in users:
        return _callback_page(False, "Unknown user — please log in and try again.")
    try:
        token_res = requests.get("https://graph.facebook.com/v19.0/oauth/access_token",
            params={"client_id": FB_APP_ID, "client_secret": FB_APP_SECRET,
                    "redirect_uri": FB_REDIRECT_URI, "code": code}, timeout=15).json()
        user_token = token_res.get("access_token")
        if not user_token:
            return _callback_page(False, f"Token error: {token_res}")
        pages_res = requests.get("https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": user_token}, timeout=10).json()
        pages = pages_res.get("data", [])
        cfg   = load_config(username)
        msg   = ""
        if pages:
            page       = pages[0]
            page_token = page.get("access_token")
            page_id    = page.get("id")
            page_name  = page.get("name", "Facebook Page")
            cfg["facebook_access_token"] = page_token
            cfg["facebook_page_id"]      = page_id
            cfg["facebook_page_name"]    = page_name
            ig_res = requests.get(f"https://graph.facebook.com/v19.0/{page_id}",
                params={"fields": "instagram_business_account", "access_token": page_token}, timeout=10).json()
            ig_account = ig_res.get("instagram_business_account", {})
            ig_id = ig_account.get("id")
            if ig_id:
                ig_info = requests.get(f"https://graph.facebook.com/v19.0/{ig_id}",
                    params={"fields": "username", "access_token": page_token}, timeout=10).json()
                cfg["instagram_access_token"] = page_token
                cfg["instagram_account_id"]   = ig_id
                cfg["instagram_username"]     = ig_info.get("username", ig_id)
                cfg["instagram_via_facebook"]  = True
                add_log(username, f"Instagram connected ✓ — @{cfg['instagram_username']}", "ok")
                msg = f"Facebook '{page_name}' + Instagram @{cfg['instagram_username']} connected!"
            else:
                msg = f"Facebook page '{page_name}' connected!"
            save_config(username, cfg)
            add_log(username, f"Facebook connected ✓ — {page_name}", "ok")
        else:
            return _callback_page(False, "No Facebook Pages found on this account.<br><br>To connect Facebook you need a Facebook Page (not a personal profile).<br><br>Go to <strong>facebook.com/pages</strong> → Create a Page → then come back and click Connect Facebook again.")
        oauth_url = session.get("fb_oauth_url", "")
        safe_oauth = oauth_url.replace("'", "\\'")
        display_name = page_name if pages else 'Facebook Account'
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Facebook Connected</title>
<style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{min-height:100vh;background:#05030f;color:#ede8ff;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;padding:24px;}}.wrap{{width:100%;max-width:380px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:14px;}}.ico{{font-size:52px;}}h2{{font-size:20px;font-weight:800;letter-spacing:-0.5px;}}.sub{{font-size:13px;color:rgba(200,185,255,.6);line-height:1.6;}}.signout-btn{{padding:11px 22px;border-radius:11px;border:1.5px solid rgba(255,96,96,.25);background:rgba(255,96,96,.07);color:rgba(255,130,130,.9);font-size:13px;font-weight:600;cursor:pointer;font-family:'Segoe UI',sans-serif;}}.note{{font-size:10px;color:rgba(200,185,255,.25);}}</style></head>
<body><div class="wrap"><div class="ico">📘✓</div><h2>Facebook Connected!</h2><p class="sub">{display_name} connected.<br>Closing in 2 seconds...</p><button class="signout-btn" onclick="doSignOut()">↩ Wrong account? Sign out &amp; use different</button><p class="note">Signs you out so you can connect a different account</p></div>
<script>window.opener&&window.opener.postMessage('channel_connected:facebook:1','*');var t=setTimeout(function(){{window.close();}},2000);function doSignOut(){{clearTimeout(t);sessionStorage.setItem('fb_oauth_url','{safe_oauth}');window.location.href='/api/auth/facebook/signout';}}</script></body></html>"""
    except Exception as e:
        return _callback_page(False, f"Error: {e}")


@app.route("/api/auth/facebook/signout")
def facebook_signout():
    oauth_url = session.get("fb_oauth_url", "")
    if not oauth_url:
        return redirect("/setup")
    safe_oauth = oauth_url.replace("'", "\\'")
    host = request.host_url.rstrip("/")
    reauth_url = f"{host}/api/auth/facebook/reauth"
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Facebook</title>
<style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{min-height:100vh;background:#05030f;color:#ede8ff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;text-align:center;padding:28px;gap:0;}}.btn{{margin-top:22px;width:100%;max-width:340px;padding:15px;border-radius:13px;font-size:15px;font-weight:700;cursor:pointer;border:none;font-family:'Segoe UI',sans-serif;}}.btn-lo{{background:linear-gradient(135deg,#1877f2,#0866ff);color:#fff;}}.btn-go{{background:linear-gradient(135deg,#b085ff,#ff80b5);color:#fff;display:none;margin-top:10px;}}.note{{font-size:11px;color:rgba(200,185,255,.35);margin-top:14px;max-width:300px;line-height:1.6;}}h2{{font-size:20px;font-weight:800;margin-bottom:10px;}}</style></head>
<body><h2>Switch Facebook Account</h2><p style="font-size:13px;color:rgba(200,185,255,.55);">Sign out first, then sign in with a different account.</p>
<button class="btn btn-lo" id="btnLogout" onclick="doLogout()">Sign Out of Facebook</button>
<button class="btn btn-go" id="btnGo" onclick="window.location.href='{reauth_url}'">✓ Continue to Sign In →</button>
<script>window.addEventListener('load',function(){{if(sessionStorage.getItem('fb_logged_out')){{sessionStorage.removeItem('fb_logged_out');document.getElementById('btnLogout').style.display='none';document.getElementById('btnGo').style.display='block';}}}});document.getElementById('btnLogout').addEventListener('click',function(){{sessionStorage.setItem('fb_logged_out','1');}},true);function doLogout(){{document.getElementById('btnLogout').disabled=true;document.getElementById('btnLogout').textContent='Signing out...';window.location.href='https://www.facebook.com';setTimeout(function(){{window.location.href='https://m.facebook.com/logout.php';}},100);}}</script></body></html>"""


@app.route("/api/auth/facebook/reauth")
def facebook_reauth():
    return """<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Sign in to Facebook</title>
<style>*{box-sizing:border-box;margin:0;padding:0;}body{min-height:100vh;background:#05030f;color:#ede8ff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;text-align:center;padding:28px;gap:16px;}.ico{font-size:48px;}h2{font-size:19px;font-weight:800;}p{font-size:13px;color:rgba(200,185,255,.55);max-width:300px;line-height:1.7;}.btn{padding:14px 32px;border-radius:12px;border:none;background:linear-gradient(135deg,#1877f2,#0866ff);color:#fff;font-size:15px;font-weight:700;cursor:pointer;font-family:'Segoe UI',sans-serif;}</style></head>
<body><div class="ico">📘</div><h2>✓ Signed out of Facebook</h2><p>Now sign in with the account you want to connect.</p><button class='btn' id='btn'>Sign In with Facebook →</button>
<script>var u=sessionStorage.getItem('fb_oauth_url');if(u){sessionStorage.removeItem('fb_oauth_url');setTimeout(function(){window.location.href=u;},800);document.getElementById('btn').onclick=function(){window.location.href=u;};}else{document.getElementById('btn').onclick=function(){window.close();};document.querySelector('p').textContent='Please close this window and click Connect again.';}</script></body></html>"""


@app.route("/api/auth/facebook/disconnect", methods=["POST"])
@require_auth
def facebook_disconnect(username):
    cfg = load_config(username)
    for k in ["facebook_access_token", "facebook_page_id", "facebook_page_name"]:
        cfg.pop(k, None)
    save_config(username, cfg)
    add_log(username, "Facebook disconnected", "warn")
    return jsonify({"ok": True})


@app.route("/api/auth/instagram/disconnect", methods=["POST"])
@require_auth
def instagram_disconnect(username):
    cfg = load_config(username)
    for k in ["instagram_access_token", "instagram_account_id",
              "instagram_username", "instagram_via_facebook", "instagram_token_expires"]:
        cfg.pop(k, None)
    save_config(username, cfg)
    add_log(username, "Instagram disconnected", "warn")
    return jsonify({"ok": True})


@app.route("/api/auth/instagram_direct")
@require_auth
def instagram_direct_auth(username):
    if not IG_APP_ID:
        return jsonify({"ok": False, "message": "INSTAGRAM_APP_ID not set in environment."}), 400
    state_token   = secrets.token_hex(16)
    state_payload = f"{username}:{state_token}"
    session["ig_direct_state"] = state_token
    params = {"client_id": IG_APP_ID, "redirect_uri": IG_REDIRECT_URI,
              "scope": "instagram_business_basic,instagram_business_content_publish",
              "response_type": "code", "state": state_payload}
    oauth_url = "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode(params)
    return jsonify({"ok": True, "auth_url": oauth_url})


@app.route("/api/auth/instagram_direct/callback")
def instagram_direct_callback():
    code      = request.args.get("code", "")
    state_raw = request.args.get("state", "")
    error     = request.args.get("error", "")
    if error:
        return _callback_page(False, f"Instagram auth error: {request.args.get('error_description', 'Unknown error')}")
    try:
        username, state_token = state_raw.split(":", 1)
        if not username:
            raise ValueError("Empty username")
    except Exception:
        return _callback_page(False, "Invalid OAuth state — please log in and try again.")
    users = load_users()
    if username not in users:
        return _callback_page(False, "Unknown user — please log in and try again.")
    try:
        token_res = requests.post("https://api.instagram.com/oauth/access_token",
            data={"client_id": IG_APP_ID, "client_secret": IG_APP_SECRET,
                  "grant_type": "authorization_code", "redirect_uri": IG_REDIRECT_URI, "code": code}, timeout=15).json()
        short_token = token_res.get("access_token")
        ig_id       = str(token_res.get("user_id", ""))
        if not short_token:
            return _callback_page(False, f"Token exchange failed: {token_res}")
        long_res = requests.get("https://graph.instagram.com/access_token",
            params={"grant_type": "ig_exchange_token", "client_secret": IG_APP_SECRET, "access_token": short_token}, timeout=15).json()
        long_token = long_res.get("access_token", short_token)
        expires_in = long_res.get("expires_in", 5184000)
        ig_info = requests.get("https://graph.instagram.com/me",
            params={"fields": "id,username", "access_token": long_token}, timeout=10).json()
        ig_username = ig_info.get("username", ig_id)
        ig_id       = ig_info.get("id", ig_id)
        cfg = load_config(username)
        cfg["instagram_access_token"]  = long_token
        cfg["instagram_account_id"]    = ig_id
        cfg["instagram_username"]      = ig_username
        cfg["instagram_via_facebook"]  = False
        cfg["instagram_token_expires"] = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        save_config(username, cfg)
        add_log(username, f"Instagram connected ✓ — @{ig_username}", "ok")
        return (f"<html><body><script>window.opener&&window.opener.postMessage('channel_connected:instagram:1','*');window.close();</script>"
                f"<p>Instagram @{ig_username} connected! Closing...</p></body></html>")
    except Exception as e:
        add_log(username, f"Instagram direct OAuth error: {e}", "error")
        return _callback_page(False, f"Error: {e}")


@app.route("/api/auth/instagram_direct/disconnect", methods=["POST"])
@require_auth
def instagram_direct_disconnect(username):
    cfg = load_config(username)
    for k in ["instagram_access_token", "instagram_account_id",
              "instagram_username", "instagram_via_facebook", "instagram_token_expires"]:
        cfg.pop(k, None)
    save_config(username, cfg)
    add_log(username, "Instagram (direct) disconnected", "warn")
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════════════════════════════
#  STATUS / RUN / SUBJECTS
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/channel/toggle", methods=["POST"])
@require_auth
def channel_toggle(username):
    data     = request.get_json()
    platform = data.get("platform", "")
    slot     = int(data.get("slot", 1))
    enabled  = bool(data.get("enabled", True))
    if platform not in ("linkedin", "facebook", "instagram"):
        return jsonify({"ok": False, "message": "Unknown platform"}), 400
    key = f"{platform}_{slot}"
    cfg = load_config(username)
    if "channel_enabled" not in cfg:
        cfg["channel_enabled"] = {}
    cfg["channel_enabled"][key] = enabled
    save_config(username, cfg)
    add_log(username, f"Channel {platform} #{slot} {'enabled' if enabled else 'disabled'} for posting", "info")
    return jsonify({"ok": True, "key": key, "enabled": enabled})


@app.route("/api/status")
@require_auth
def get_status(username):
    state        = get_state(username)
    next_run_utc = get_next_run_time_for_user(username)
    all_slots    = get_all_next_slots(username)

    subjects_file = user_subjects_path(username)
    subjects = []
    if os.path.exists(subjects_file):
        with open(subjects_file, "r", encoding="utf-8") as f:
            subjects = [l.strip() for l in f if l.strip()]

    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", None)

    channels_info = []
    ch_enabled = cfg.get("channel_enabled", {})

    for slot in [1, 2, 3]:
        tok  = cfg.get(f"linkedin_{slot}_access_token", "")
        name = cfg.get(f"linkedin_{slot}_name", f"LinkedIn #{slot}")
        key  = f"linkedin_{slot}"
        enabled = ch_enabled.get(key, True) if tok else False
        channels_info.append({"slot": slot, "platform": "linkedin", "name": name if tok else f"LinkedIn #{slot}",
                               "exists": bool(tok), "active": bool(tok), "enabled": enabled})
    fb_tok = cfg.get("facebook_access_token", "")
    channels_info.append({"slot": 1, "platform": "facebook", "name": cfg.get("facebook_page_name", "Facebook Page"),
                           "exists": bool(fb_tok), "active": bool(fb_tok),
                           "enabled": ch_enabled.get("facebook_1", True) if fb_tok else False})
    ig_tok = cfg.get("instagram_access_token", "")
    channels_info.append({"slot": 1, "platform": "instagram",
                           "name": f"@{cfg.get('instagram_username', 'instagram')}" if ig_tok else "Instagram",
                           "exists": bool(ig_tok), "active": bool(ig_tok),
                           "enabled": ch_enabled.get("instagram_1", True) if ig_tok else False,
                           "via_facebook": cfg.get("instagram_via_facebook", False)})

    # Review queue — return pending items
    review_queue = load_review_queue(username)
    pending_reviews = [r for r in review_queue if r["status"] == "pending"]

    users = load_users()
    user_email = users.get(username, {}).get("email", "")
    has_password = user_has_password(users.get(username, {}))

    return jsonify({
        "status":         state["status"],
        "running":        state["running"],
        "today_count":    state["today_count"],
        "total_run":      state["total_run"],
        "last_run":       state["last_run"],
        "next_run_iso":   next_run_utc.isoformat() + "Z",
        "seconds_left":   max(0, int((next_run_utc - datetime.utcnow()).total_seconds())),
        "all_slots":      all_slots,
        "subjects":       subjects,
        "logs":           state["logs"][-30:],
        "utc_offset":     offset,
        "time_slots":     cfg.get("time_slots", [TARGET_HOUR]),
        "config":         cfg,
        "channels_info":  channels_info,
        "review_queue":   pending_reviews,
        "email":          user_email,
        "has_password":   has_password,
        "accounts": {
            "linkedin_1":  bool(cfg.get("linkedin_1_access_token")),
            "linkedin_2":  bool(cfg.get("linkedin_2_access_token")),
            "linkedin_3":  bool(cfg.get("linkedin_3_access_token")),
            "facebook":    bool(cfg.get("facebook_access_token")),
            "instagram":   bool(cfg.get("instagram_access_token")),
        }
    })

@app.route("/api/run", methods=["POST"])
@require_auth
def manual_run(username):
    state = get_state(username)
    if state["running"]:
        return jsonify({"ok": False, "message": "Already running"}), 409
    threading.Thread(target=run_batch, args=(username, "manual"), daemon=True).start()
    return jsonify({"ok": True, "message": "Batch triggered!"})

@app.route("/api/subjects", methods=["GET"])
@require_auth
def get_subjects(username):
    subjects_file = user_subjects_path(username)
    subjects = []
    if os.path.exists(subjects_file):
        with open(subjects_file, "r", encoding="utf-8") as f:
            subjects = [l.strip() for l in f if l.strip()]
    return jsonify({"subjects": subjects})

@app.route("/api/subjects", methods=["POST"])
@require_auth
def add_subjects(username):
    data         = request.get_json()
    new_subjects = data.get("subjects", [])
    image_b64    = data.get("image_base64")
    filename     = data.get("filename", "upload.jpg")
    mode         = data.get("mode", "no_image")
    target_channels = data.get("target_channels", None)  # NEW: per-subject channels

    if not new_subjects:
        return jsonify({"ok": False, "message": "No subjects provided"}), 400

    uploaded_url = None
    if mode == "manual_image" and image_b64:
        try:
            if "," in image_b64:
                image_b64 = image_b64.split(",")[1]
            add_log(username, f"Uploading '{filename}'...", "info")
            res = requests.post("https://freeimage.host/api/1/upload", data={
                "key": "6d207e02198a847aa98d0a2a901485a5",
                "action": "upload", "source": image_b64, "format": "json"
            }, timeout=30)
            if res.status_code == 200:
                rj = res.json()
                if "image" in rj:
                    uploaded_url = rj["image"]["url"]
                    add_log(username, f"Upload OK: {uploaded_url}", "ok")
                else:
                    add_log(username, f"Upload failed: {res.text}", "error")
        except Exception as e:
            add_log(username, f"Upload error: {e}", "error")

    subjects_file = user_subjects_path(username)
    with open(subjects_file, "a", encoding="utf-8") as f:
        for s in new_subjects:
            line = s.strip()
            if mode == "auto_image":
                line = f"{line} (create image)"
            elif mode == "manual_image" and uploaded_url:
                line = f"{line} | IMG: {uploaded_url}"
            # Append per-subject channel targeting
            if target_channels and isinstance(target_channels, list) and len(target_channels) > 0:
                channels_str = ",".join(target_channels)
                line = f"{line} | CHANNELS: {channels_str}"
            f.write(line + "\n")

    add_log(username, f"Added {len(new_subjects)} subject(s) [{mode}] ✓", "ok")
    return jsonify({"ok": True, "added": len(new_subjects)})

@app.route("/api/subjects/delete", methods=["POST"])
@require_auth
def delete_subject(username):
    data  = request.get_json()
    index = data.get("index")
    subjects_file = user_subjects_path(username)
    if not os.path.exists(subjects_file):
        return jsonify({"ok": False, "message": "No subjects"}), 404
    with open(subjects_file, "r", encoding="utf-8") as f:
        subjects = [l.strip() for l in f if l.strip()]
    if index is None or index < 0 or index >= len(subjects):
        return jsonify({"ok": False, "message": "Invalid index"}), 400
    subjects.pop(index)
    with open(subjects_file, "w", encoding="utf-8") as f:
        f.write("\n".join(subjects))
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════════════════════════════
#  REVIEW QUEUE API
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/review", methods=["GET"])
@require_auth
def get_review_queue(username):
    queue = load_review_queue(username)
    return jsonify({"ok": True, "queue": queue})

@app.route("/api/review/<review_id>/publish", methods=["POST"])
@require_auth
def publish_review_item(username, review_id):
    queue = load_review_queue(username)
    item  = next((i for i in queue if i["id"] == review_id), None)
    if not item:
        return jsonify({"ok": False, "message": "Review item not found"}), 404
    if item["status"] != "pending":
        return jsonify({"ok": False, "message": f"Already {item['status']}"}), 400

    data = request.get_json() or {}
    # Allow editing text/image before publishing
    if "post_text" in data:
        item["post_text"] = data["post_text"]
    if "image_url" in data:
        item["image_url"] = data["image_url"] or None
    if "target_channels" in data:
        item["target_channels"] = data["target_channels"]

    sent = publish_to_channels(username, item["post_text"], item.get("image_url"), item.get("target_channels"))
    item["status"]             = "published"
    item["published_at"]       = datetime.utcnow().isoformat() + "Z"
    item["published_channels"] = sent

    state = get_state(username)
    state["today_count"] = state.get("today_count", 0) + 1
    state["total_run"]   = state.get("total_run", 0) + sent

    save_review_queue(username, queue)
    add_log(username, f"Manually published review post {review_id} to {sent} channel(s) ✓", "ok")
    return jsonify({"ok": True, "sent": sent})

@app.route("/api/review/<review_id>/discard", methods=["POST"])
@require_auth
def discard_review_item(username, review_id):
    queue = load_review_queue(username)
    item  = next((i for i in queue if i["id"] == review_id), None)
    if not item:
        return jsonify({"ok": False, "message": "Not found"}), 404
    item["status"] = "discarded"
    save_review_queue(username, queue)
    add_log(username, f"Review post {review_id} discarded", "warn")
    return jsonify({"ok": True})

@app.route("/api/review/<review_id>/update_image", methods=["POST"])
@require_auth
def update_review_image(username, review_id):
    """Upload a new image for a review item."""
    data     = request.get_json()
    image_b64 = data.get("image_base64", "")
    filename  = data.get("filename", "upload.jpg")

    if not image_b64:
        return jsonify({"ok": False, "message": "No image provided"}), 400

    try:
        if "," in image_b64:
            image_b64 = image_b64.split(",")[1]
        res = requests.post("https://freeimage.host/api/1/upload", data={
            "key": "6d207e02198a847aa98d0a2a901485a5",
            "action": "upload", "source": image_b64, "format": "json"
        }, timeout=30)
        if res.status_code == 200:
            rj = res.json()
            if "image" in rj:
                new_url = rj["image"]["url"]
                queue = load_review_queue(username)
                item  = next((i for i in queue if i["id"] == review_id), None)
                if item:
                    item["image_url"] = new_url
                    save_review_queue(username, queue)
                return jsonify({"ok": True, "image_url": new_url})
        return jsonify({"ok": False, "message": "Upload failed"}), 500
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

# ─── STARTUP ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    threading.Thread(target=auto_publish_reviewer, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"[STARTUP] LinkedIn Agent on port {port}")
    print(f"[STARTUP] Users: {USERS_FILE}")
    print(f"[STARTUP] Data:  {USER_DATA_DIR}/")
    print(f"[STARTUP] LinkedIn OAuth: {'✓' if LI_CLIENT_ID else '✗ LINKEDIN_CLIENT_ID not set'}")
    print(f"[STARTUP] Facebook OAuth: {'✓' if FB_APP_ID else '✗ FACEBOOK_APP_ID not set'}")
    print(f"[STARTUP] Instagram OAuth: {'✓' if IG_APP_ID else '✗ INSTAGRAM_APP_ID not set'}")
    print(f"[STARTUP] Email (SMTP): {'✓' if SMTP_USER else '✗ SMTP_USER not set'}")
    app.run(host="0.0.0.0", port=port, debug=False)
