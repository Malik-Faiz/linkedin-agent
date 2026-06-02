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
import urllib.parse
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

# ─── OAUTH APP CREDENTIALS (set in Railway env vars) ─────────────────────────
LI_CLIENT_ID     = os.environ.get("LINKEDIN_CLIENT_ID", "")
LI_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
LI_REDIRECT_URI  = os.environ.get("LINKEDIN_REDIRECT_URI", "")

FB_APP_ID        = os.environ.get("FACEBOOK_APP_ID", "")
FB_APP_SECRET    = os.environ.get("FACEBOOK_APP_SECRET", "")
FB_REDIRECT_URI  = os.environ.get("FACEBOOK_REDIRECT_URI", "")

# Standalone Instagram Graph API app (independent of Facebook)
IG_APP_ID        = os.environ.get("INSTAGRAM_APP_ID", "")
IG_APP_SECRET    = os.environ.get("INSTAGRAM_APP_SECRET", "")
IG_REDIRECT_URI  = os.environ.get("INSTAGRAM_REDIRECT_URI", "")

# ─── AI PROMPTS ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT_POST = """You are an expert LinkedIn ghostwriter.
Write a highly engaging, professional LinkedIn post based on the user's subject.
1. Hook on the first line wrapped in **asterisks**.
2. Short, punchy sentences.
3. Call-To-Action (CTA) question at the end.
4. 3 to 5 relevant hashtags."""

SYSTEM_PROMPT_ARTICLE = """You are an expert LinkedIn article writer.
Write a long-form, in-depth LinkedIn article based on the user's subject.
Structure:
1. Compelling title on the first line prefixed with TITLE:
2. A strong introduction paragraph.
3. 4 to 6 sections with clear headings wrapped in ## markdown.
4. Each section has 2-3 detailed paragraphs with real insights.
5. A conclusion section with key takeaways.
6. End with a thought-provoking question for readers.
Write in a professional yet conversational tone. Minimum 600 words."""

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
    # LinkedIn slots 1-3
    "linkedin_1_access_token", "linkedin_2_access_token", "linkedin_3_access_token",
    # Facebook / Instagram
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

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def to_unicode_bold(text):
    normal  = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b_chars = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return text.translate(str.maketrans(normal, b_chars))

def format_linkedin_bold(text):
    return re.sub(r'\*\*(.*?)\*\*', lambda m: to_unicode_bold(m.group(1)), text)

def get_next_run_time_for_user(username):
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", 0)
    now_utc  = datetime.utcnow()
    user_now = now_utc + timedelta(hours=offset)
    target_local = user_now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
    if user_now >= target_local:
        target_local += timedelta(days=1)
    return target_local - timedelta(hours=offset)

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
    clean    = subject.replace("(create image)", "").replace("(article)", "").strip()
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

def generate_article(username, subject):
    cfg      = load_config(username)
    groq_key = cfg.get("groq_api_key", "")
    clean    = subject.replace("(article)", "").strip()
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "max_tokens": 2000, "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_ARTICLE},
                {"role": "user",   "content": f"Write a LinkedIn article about: {clean}"}
            ]}, timeout=60
        ).json()
        if "error" in response:
            add_log(username, f"Groq Article Error: {response['error']['message']}", "error")
            return None, None
        raw   = response["choices"][0]["message"]["content"]
        lines = raw.strip().split("\n")
        title = clean
        body  = raw
        if lines[0].startswith("TITLE:"):
            title = lines[0].replace("TITLE:", "").strip()
            body  = "\n".join(lines[1:]).strip()
        return title, body
    except Exception as e:
        add_log(username, f"Groq article failure: {e}", "error")
        return None, None

def get_image_url(username, subject):
    cfg      = load_config(username)
    serp_key = cfg.get("serpapi_key", "").strip()
    if not serp_key:
        add_log(username, "SerpAPI key not set — post sent without image.", "warn")
        return None
    clean = subject.replace("(create image)", "").replace("(article)", "").strip()
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

def publish_to_linkedin_slot(username, slot, text, image_url=None, is_article=False):
    """Publish to a specific LinkedIn account slot (1, 2, or 3).
    Supports both personal profiles (urn:li:person:) and company pages (urn:li:organization:).
    """
    cfg      = load_config(username)
    token    = cfg.get(f"linkedin_{slot}_access_token", "")
    urn      = cfg.get(f"linkedin_{slot}_urn", "")
    name     = cfg.get(f"linkedin_{slot}_name", f"Slot {slot}")
    acct_type = cfg.get(f"linkedin_{slot}_account_type", "personal")

    if not token or not urn:
        add_log(username, f"  → [LinkedIn #{slot}] Not connected — skipping.", "warn")
        return False

    # Company pages use a different visibility URN
    if acct_type == "organization":
        visibility = {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
    else:
        visibility = {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    if is_article:
        payload = {
            "author":         urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary":    {"text": text},
                    "shareMediaCategory": "NONE"
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
        }
    elif image_url:
        # Step 1: register image upload
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
        payload = {
            "author": urn, "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text}, "shareMediaCategory": "NONE"
            }},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
        }

    try:
        res = requests.post("https://api.linkedin.com/v2/ugcPosts",
                            headers=headers, json=payload, timeout=20)
        if res.status_code in (200, 201):
            pid   = res.headers.get("x-restli-id", "n/a")
            label = "article" if is_article else "post"
            add_log(username, f"  → [LinkedIn #{slot} — {name}] Published {label} ✓ ID: {pid}", "ok")
            return True
        else:
            add_log(username, f"  → [LinkedIn #{slot}] Failed {res.status_code}: {res.text[:200]}", "error")
            return False
    except Exception as e:
        add_log(username, f"  → [LinkedIn #{slot}] Exception: {e}", "error")
        return False


def publish_to_facebook(username, text, image_url=None):
    """Publish post directly to Facebook Page."""
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
    """Publish image post to Instagram Business account."""
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


def publish_to_all(username, text, image_url=None, is_article=False, article_title=None):
    """
    Publish to ALL connected channels:
      - LinkedIn slots 1, 2, 3 (each is a separate account)
      - Facebook page
      - Instagram business
    Returns count of successful publishes.
    """
    cfg     = load_config(username)
    success = 0

    # LinkedIn — up to 3 slots
    for slot in [1, 2, 3]:
        token = cfg.get(f"linkedin_{slot}_access_token", "")
        if token:
            ok = publish_to_linkedin_slot(username, slot, text, image_url, is_article)
            if ok:
                success += 1

    # Facebook — articles not supported
    if cfg.get("facebook_access_token") and not is_article:
        ok = publish_to_facebook(username, text, image_url)
        if ok:
            success += 1

    # Instagram — articles not supported, image required
    if cfg.get("instagram_access_token") and not is_article:
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

    batch = all_subjects[:BATCH_SIZE]
    with open(subjects_file, "w", encoding="utf-8") as f:
        f.write("\n".join(all_subjects[BATCH_SIZE:]))

    add_log(username, f"Processing {len(batch)} subjects. {len(all_subjects)-len(batch)} remaining.", "info")

    for j, subject in enumerate(batch):
        manual_image_url = None
        base_subject     = subject
        is_article       = "(article)" in subject.lower()

        if "| IMG:" in subject:
            parts            = subject.split("| IMG:")
            base_subject     = parts[0].strip()
            manual_image_url = parts[1].strip()

        add_log(username, f"[{j+1}/{len(batch)}] {'[ARTICLE] ' if is_article else ''}{base_subject[:50]}", "info")

        if is_article:
            title, body = generate_article(username, base_subject)
            if not title or not body:
                continue
            add_log(username, f"[{j+1}/{len(batch)}] Article generated ✓ — {title[:40]}", "ok")
            sent = publish_to_all(username, body, is_article=True, article_title=title)
        else:
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

            sent = publish_to_all(username, post_text, image_url)

        if sent > 0:
            add_log(username, f"[{j+1}/{len(batch)}] Sent to {sent} channel(s) ✓", "ok")
            state["today_count"] += 1
            state["total_run"]   += sent
        else:
            add_log(username, f"[{j+1}/{len(batch)}] All channels failed.", "error")
        time.sleep(10)

    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["running"]  = False
    state["status"]   = "waiting"
    add_log(username, f"Batch complete! {state['today_count']} posts sent.", "ok")
    save_state(username, state)

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def scheduler_loop():
    print("[SCHEDULER] Started — fires at 08:00 local time per user")
    fired_today = set()
    while True:
        now_utc = datetime.utcnow()
        for uname in load_users():
            cfg      = load_config(uname)
            offset   = cfg.get("utc_offset_hours", 0)
            user_now = now_utc + timedelta(hours=offset)
            fire_key = (uname, user_now.date())
            if (user_now.hour == TARGET_HOUR and user_now.minute == 0
                    and user_now.second < 5 and fire_key not in fired_today):
                fired_today.add(fire_key)
                add_log(uname, f"Scheduler fired at local 08:00 (UTC{offset:+.1f}h)", "info")
                threading.Thread(target=run_batch, args=(uname, "scheduler"), daemon=True).start()
        fired_today = {k for k in fired_today if k[1] >= now_utc.date()}
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
def page_login():     return load_html("login.html")
@app.route("/setup")
def page_setup():     return load_html("setup.html")
@app.route("/dashboard")
def page_dashboard(): return load_html("dashboard.html")

@app.route("/ping")
def ping():
    utc_now = datetime.utcnow()
    return jsonify({"status": "alive", "utc_time": utc_now.strftime("%H:%M:%S"),
                    "utc_iso": utc_now.isoformat() + "Z"})

# ════════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/register", methods=["POST"])
def register():
    data     = request.get_json()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "message": "Username and password required."}), 400
    if len(username) < 3:
        return jsonify({"ok": False, "message": "Username must be at least 3 characters."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."}), 400
    if not re.match(r'^[a-z0-9_]+$', username):
        return jsonify({"ok": False, "message": "Username: letters, numbers, underscores only."}), 400
    users = load_users()
    if username in users:
        return jsonify({"ok": False, "message": "Username already taken."}), 409
    h, salt = hash_password(password)
    users[username] = {"password_hash": h, "salt": salt, "created_at": datetime.now().isoformat()}
    save_users(users)
    os.makedirs(user_dir(username), exist_ok=True)
    utc_offset = data.get("utc_offset_hours")
    if utc_offset is not None:
        cfg = load_config(username)
        cfg["utc_offset_hours"] = float(utc_offset)
        save_config(username, cfg)
    session["username"] = username
    session.permanent   = True
    cfg = load_config(username)
    has_config = bool(cfg.get("groq_api_key"))
    return jsonify({"ok": True, "username": username, "has_config": has_config, "message": "Account created!"})

@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    users = load_users()
    if username not in users:
        return jsonify({"ok": False, "message": "Invalid username or password."}), 401
    u = users[username]
    if not verify_password(password, u["password_hash"], u["salt"]):
        return jsonify({"ok": False, "message": "Invalid username or password."}), 401
    utc_offset = data.get("utc_offset_hours")
    if utc_offset is not None:
        cfg = load_config(username)
        cfg["utc_offset_hours"] = float(utc_offset)
        save_config(username, cfg)
    session["username"] = username
    session.permanent   = True
    cfg = load_config(username)
    has_config = bool(cfg.get("groq_api_key"))
    return jsonify({"ok": True, "username": username, "has_config": has_config})

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
def me():
    username = session.get("username")
    if not username:
        return jsonify({"ok": False, "authenticated": False})
    cfg = load_config(username)
    has_config = bool(cfg.get("groq_api_key"))
    return jsonify({"ok": True, "authenticated": True, "username": username, "has_config": has_config})

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
    save_config(username, cfg)
    add_log(username, "Config updated ✓", "ok")
    return jsonify({"ok": True, "message": "Configuration saved!"})

# ════════════════════════════════════════════════════════════════════════════════
#  CHANNELS STATUS  — GET /api/channels
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

    # Build LinkedIn slots 1-3
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
#  OAUTH CALLBACK HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def _callback_page(success, message="", msg_key="", platform="linkedin", slot=1):
    """
    Returns an HTML page that:
     - On success: posts a message to the opener and closes the popup
     - On failure: shows the error message in the popup window
    Uses a self-contained page so it works even when the session cookie
    is not available in the popup (common cross-origin scenario on Railway).
    """
    if success:
        event = msg_key or f"channel_connected:{platform}:{slot}"
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;
       background:#05030f;color:#3dffc0;font-family:'Segoe UI',sans-serif;text-align:center;}}
  .box{{padding:40px;border:1px solid rgba(61,255,192,0.3);border-radius:20px;background:rgba(61,255,192,0.05);}}
  .ico{{font-size:48px;margin-bottom:16px;}}
  h2{{font-size:20px;margin-bottom:8px;}}
  p{{font-size:13px;color:rgba(200,185,255,0.6);}}
</style></head>
<body><div class="box">
  <div class="ico">✓</div>
  <h2>{message or 'Connected!'}</h2>
  <p>This window will close automatically...</p>
</div>
<script>
  try {{ window.opener && window.opener.postMessage('{event}', '*'); }} catch(e) {{}}
  setTimeout(function(){{ window.close(); }}, 1200);
</script>
</body></html>"""
    else:
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;
       background:#05030f;color:#ff6060;font-family:'Segoe UI',sans-serif;text-align:center;}}
  .box{{padding:40px;border:1px solid rgba(255,96,96,0.3);border-radius:20px;background:rgba(255,96,96,0.05);max-width:420px;}}
  .ico{{font-size:48px;margin-bottom:16px;}}
  h2{{font-size:18px;margin-bottom:8px;}}
  p{{font-size:12px;color:rgba(200,185,255,0.5);margin-top:16px;}}
  button{{margin-top:20px;padding:10px 24px;background:rgba(255,96,96,0.15);border:1px solid rgba(255,96,96,0.3);
          color:#ff6060;border-radius:10px;cursor:pointer;font-size:13px;}}
</style></head>
<body><div class="box">
  <div class="ico">✕</div>
  <h2>Connection Failed</h2>
  <div style="font-size:12px;color:rgba(255,150,150,0.8);margin-top:8px;">{message}</div>
  <p>You can close this window and try again.</p>
  <button onclick="window.close()">Close</button>
</div>
</body></html>""", 400


# ════════════════════════════════════════════════════════════════════════════════
#  LINKEDIN OAUTH — supports slot param (1, 2, 3)
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
    slot = max(1, min(3, slot))  # clamp 1-3

    state_token   = secrets.token_hex(16)
    # Encode username + slot into state — popup cookie may not be forwarded on Railway
    state_payload = f"{username}:{slot}:{state_token}"
    session[f"li_state_{slot}"] = state_token
    session["li_active_slot"]   = slot

    params = {
        "response_type": "code",
        "client_id":     LI_CLIENT_ID,
        "redirect_uri":  LI_REDIRECT_URI,
        "state":         state_payload,
        "scope":         "openid profile w_member_social",
    }
    oauth_url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params)

    # LinkedIn ignores prompt=login when a session cookie already exists.
    # Real fix: route the popup through our own /api/auth/linkedin/start/<slot>
    # which serves an HTML page that:
    #   1. Clears LinkedIn cookies by loading linkedin.com/m/logout in an invisible iframe
    #   2. After a short delay, redirects to the OAuth URL
    # This forces a fresh login screen every time regardless of existing LinkedIn session.
    start_url = f"{request.host_url.rstrip('/')}/api/auth/linkedin/start/{slot}?oauth={urllib.parse.quote(oauth_url)}"
    return jsonify({"ok": True, "auth_url": start_url})


def _li_account_picker_page(username, slot, access_token, expires_in, personal_name, personal_id, orgs):
    """
    Renders an in-popup page that lets the user pick:
      • their personal LinkedIn profile, OR
      • one of their company pages (organisations)
    Submits to /api/auth/linkedin/pick to save the chosen account.
    """
    org_cards = ""
    for org in orgs:
        oid   = org.get("id", "")
        oname = org.get("name", f"Company {oid}")
        org_cards += f"""
        <label class="acct-card" onclick="pick('organization', '{oid}', '{oname.replace("'", "\'")}')">
          <span class="acct-ico">🏢</span>
          <span class="acct-info">
            <strong>{oname}</strong>
            <small>Company Page</small>
          </span>
          <span class="acct-arrow">→</span>
        </label>"""

    # Store token temporarily for the pick endpoint
    token_key = f"li_pending_{username}_{slot}"
    session[token_key] = {
        "access_token": access_token,
        "expires_in":   expires_in,
        "personal_id":  personal_id,
        "personal_name": personal_name,
    }

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Choose LinkedIn Account</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{min-height:100vh;background:#05030f;color:#ede8ff;font-family:'Segoe UI',sans-serif;
     display:flex;align-items:center;justify-content:center;padding:24px;}}
.wrap{{width:100%;max-width:420px;}}
.hdr{{text-align:center;margin-bottom:28px;}}
.hdr h2{{font-size:20px;font-weight:800;letter-spacing:-0.5px;margin-bottom:6px;}}
.hdr p{{font-size:12px;color:rgba(200,185,255,0.55);}}
.slot-badge{{display:inline-block;background:rgba(176,133,255,0.15);border:1px solid rgba(176,133,255,0.3);
             color:#b085ff;border-radius:20px;padding:4px 14px;font-size:11px;margin-bottom:12px;}}
.section-label{{font-size:10px;letter-spacing:2px;text-transform:uppercase;
                color:rgba(200,185,255,0.4);margin-bottom:10px;}}
.acct-card{{display:flex;align-items:center;gap:14px;padding:14px 16px;
            border:1.5px solid rgba(160,120,255,0.2);border-radius:14px;
            background:rgba(255,255,255,0.03);margin-bottom:10px;
            cursor:pointer;transition:all 0.2s;text-decoration:none;color:inherit;}}
.acct-card:hover{{border-color:rgba(176,133,255,0.5);background:rgba(176,133,255,0.07);
                  transform:translateY(-1px);}}
.acct-ico{{font-size:26px;flex-shrink:0;}}
.acct-info{{flex:1;min-width:0;}}
.acct-info strong{{display:block;font-size:14px;font-weight:700;
                   white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.acct-info small{{display:block;font-size:11px;color:rgba(200,185,255,0.5);margin-top:2px;}}
.acct-arrow{{color:rgba(176,133,255,0.5);font-size:16px;flex-shrink:0;}}
.divider{{display:flex;align-items:center;gap:10px;margin:16px 0;}}
.divider::before,.divider::after{{content:'';flex:1;height:1px;background:rgba(160,120,255,0.15);}}
.divider span{{font-size:10px;color:rgba(200,185,255,0.35);letter-spacing:1px;}}
.loading{{display:none;text-align:center;padding:20px;font-size:13px;color:rgba(200,185,255,0.5);}}
.spin{{display:inline-block;animation:sp 0.8s linear infinite;}}
@keyframes sp{{to{{transform:rotate(360deg);}}}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="slot-badge">LinkedIn Channel {slot}</div>
    <h2>Choose account to connect</h2>
    <p>Select your personal profile or a company page you manage</p>
  </div>

  <div class="section-label">Personal Profile</div>
  <label class="acct-card" onclick="pick('personal', '{personal_id}', '{personal_name.replace("'", "\'")}')">
    <span class="acct-ico">👤</span>
    <span class="acct-info">
      <strong>{personal_name}</strong>
      <small>Personal LinkedIn Profile</small>
    </span>
    <span class="acct-arrow">→</span>
  </label>

  {'<div class="divider"><span>OR COMPANY PAGE</span></div>' + org_cards if orgs else ''}

  <div class="loading" id="loadingBox">
    <span class="spin">⟳</span> Connecting...
  </div>
</div>

<script>
async function pick(acct_type, acct_id, acct_name) {{
  document.querySelectorAll('.acct-card').forEach(c => c.style.pointerEvents='none');
  document.getElementById('loadingBox').style.display='block';
  try {{
    const res = await fetch('/api/auth/linkedin/pick', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        username: '{username}',
        slot:     {slot},
        acct_type: acct_type,
        acct_id:   acct_id,
        acct_name: acct_name,
      }})
    }});
    const data = await res.json();
    if (data.ok) {{
      window.opener && window.opener.postMessage('channel_connected:linkedin:{slot}', '*');
      window.close();
    }} else {{
      alert('Error: ' + (data.message || 'Unknown error'));
      document.querySelectorAll('.acct-card').forEach(c => c.style.pointerEvents='');
      document.getElementById('loadingBox').style.display='none';
    }}
  }} catch(e) {{
    alert('Network error: ' + e.message);
    document.querySelectorAll('.acct-card').forEach(c => c.style.pointerEvents='');
    document.getElementById('loadingBox').style.display='none';
  }}
}}
</script>
</body></html>"""


@app.route("/api/auth/linkedin/pick", methods=["POST"])
def linkedin_pick():
    """
    Called from the in-popup picker page.
    Saves the chosen account (personal or company page) to the slot.
    """
    data      = request.get_json()
    username  = data.get("username", "")
    slot      = int(data.get("slot", 1))
    acct_type = data.get("acct_type", "personal")   # "personal" | "organization"
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

    if acct_type == "organization":
        # For company pages, use the organization URN
        urn = f"urn:li:organization:{acct_id}"
    else:
        # Personal profile
        urn = f"urn:li:person:{acct_id}"

    cfg[f"linkedin_{slot}_access_token"]  = access_token
    cfg[f"linkedin_{slot}_urn"]           = urn
    cfg[f"linkedin_{slot}_name"]          = acct_name
    cfg[f"linkedin_{slot}_account_type"]  = acct_type   # "personal" | "organization"
    cfg[f"linkedin_{slot}_token_expires"] = (
        datetime.utcnow() + timedelta(seconds=expires_in)
    ).isoformat()
    save_config(username, cfg)

    # Clean up pending token from session
    session.pop(token_key, None)

    add_log(username, f"LinkedIn slot {slot} → {acct_type} '{acct_name}' connected ✓", "ok")
    return jsonify({"ok": True})


@app.route("/api/auth/linkedin/start/<int:slot>")
def linkedin_start(slot):
    """
    Intermediate page opened in the OAuth popup.
    Solves the "same account reused" problem:
      - LinkedIn ignores prompt=login if the user already has a LinkedIn session cookie.
      - We can't clear linkedin.com cookies from our domain (cross-origin).
      - Solution: serve an HTML page that opens linkedin.com/m/logout in an invisible
        iframe, waits 1.5 s for the logout to complete, then navigates to the real
        OAuth URL. This clears the LinkedIn session before the auth dialog appears,
        forcing the user to type their credentials again every time.
    """
    oauth_url = request.args.get("oauth", "")
    if not oauth_url:
        return "Missing oauth param", 400

    # Decode back to real URL
    oauth_url = urllib.parse.unquote(oauth_url)

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Connecting LinkedIn #{slot}...</title>
<style>
  body{{margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
       min-height:100vh;background:#05030f;color:#ede8ff;font-family:'Segoe UI',sans-serif;
       text-align:center;gap:16px;}}
  .spin{{font-size:32px;animation:sp 1s linear infinite;display:inline-block;}}
  @keyframes sp{{to{{transform:rotate(360deg);}}}}
  p{{font-size:13px;color:rgba(200,185,255,0.5);margin:0;}}
  iframe{{display:none;}}
</style>
</head>
<body>
  <div class="spin">⟳</div>
  <p>Preparing fresh login for Channel {slot}...</p>
  <!-- Logout iframe clears LinkedIn session cookie -->
  <iframe id="logoutFrame" src="https://www.linkedin.com/m/logout"></iframe>
<script>
  // Give LinkedIn time to process the logout, then redirect to OAuth
  setTimeout(function() {{
    window.location.href = {json.dumps(oauth_url)};
  }}, 1800);
</script>
</body></html>"""


@app.route("/api/auth/linkedin/callback")
def linkedin_callback():
    code      = request.args.get("code", "")
    state_raw = request.args.get("state", "")
    error     = request.args.get("error", "")

    if error:
        desc = request.args.get("error_description", error)
        return _callback_page(False, f"LinkedIn denied access: {desc}")

    # State format: "username:slot:token"
    try:
        username, slot_str, state_token = state_raw.split(":", 2)
        slot = int(slot_str)
    except Exception:
        username    = session.get("username", "")
        slot        = session.get("li_active_slot", 1)
        state_token = state_raw

    if not username:
        return _callback_page(False, "Session lost — please log in again and retry.")

    users = load_users()
    if username not in users:
        return _callback_page(False, "Unknown user.")

    try:
        # Exchange code for token
        token_res = requests.post(
            "https://www.linkedin.com/oauth/v2/accessToken",
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": LI_REDIRECT_URI,
                  "client_id": LI_CLIENT_ID, "client_secret": LI_CLIENT_SECRET},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15
        ).json()
        access_token = token_res.get("access_token")
        expires_in   = token_res.get("expires_in", 5184000)
        if not access_token:
            err = token_res.get("error_description", str(token_res))
            return _callback_page(False, f"Token exchange failed: {err}")

        # Get personal profile
        profile  = requests.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=10
        ).json()
        li_id    = profile.get("sub", "")
        li_name  = (
            profile.get("name") or
            f"{profile.get('given_name','')} {profile.get('family_name','')}".strip() or
            "LinkedIn User"
        )

        # Fetch organisations (company pages) this user administers.
        # Uses /v2/organizationAcls which works with w_member_social scope.
        # If the app doesn't have access, we gracefully skip — user still gets personal profile.
        orgs = []
        try:
            org_res = requests.get(
                "https://api.linkedin.com/v2/organizationAcls",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                },
                params={
                    "q":          "roleAssignee",
                    "role":       "ADMINISTRATOR",
                    "count":      10,
                },
                timeout=10
            ).json()

            # Extract org IDs from ACL response
            org_ids = []
            for elem in org_res.get("elements", []):
                target = elem.get("organizationalTarget", "")
                # target looks like "urn:li:organization:12345"
                if "organization:" in str(target):
                    org_id = str(target).split("organization:")[-1].strip()
                    if org_id:
                        org_ids.append(org_id)

            # Fetch names for each org
            for org_id in org_ids[:5]:  # max 5
                try:
                    org_info = requests.get(
                        f"https://api.linkedin.com/v2/organizations/{org_id}",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "X-Restli-Protocol-Version": "2.0.0",
                        },
                        timeout=8
                    ).json()
                    org_name = (
                        org_info.get("localizedName") or
                        org_info.get("name", {}).get("localized", {}).get("en_US", f"Company {org_id}")
                    )
                    orgs.append({"id": org_id, "name": org_name})
                except Exception:
                    orgs.append({"id": org_id, "name": f"Company Page {org_id}"})

        except Exception as org_err:
            # Org fetch is best-effort — user still gets personal profile picker
            add_log(username, f"LinkedIn org fetch skipped: {org_err}", "warn")

        # Store token in session temporarily for /pick endpoint
        token_key = f"li_pending_{username}_{slot}"
        session[token_key]     = {
            "access_token":  access_token,
            "expires_in":    expires_in,
            "personal_id":   li_id,
            "personal_name": li_name,
        }
        session.modified = True

        # Show account picker in popup
        return _li_account_picker_page(
            username, slot, access_token, expires_in, li_name, li_id, orgs
        )

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

# ════════════════════════════════════════════════════════════════════════════════
#  FACEBOOK OAUTH
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/auth/facebook")
@require_auth
def facebook_auth(username):
    if not FB_APP_ID:
        return jsonify({"ok": False, "message": "FACEBOOK_APP_ID not set in environment"}), 400
    state_token = secrets.token_hex(16)
    session["fb_state"] = state_token
    # Encode username in state for session-less callback
    state = f"{username}:{state_token}"
    params = {
        "client_id":    FB_APP_ID,
        "redirect_uri": FB_REDIRECT_URI,
        "state":        state,
        "scope":        "pages_manage_posts,pages_read_engagement,instagram_basic,instagram_content_publish"
    }
    url = "https://www.facebook.com/v19.0/dialog/oauth?" + urllib.parse.urlencode(params)
    return jsonify({"ok": True, "auth_url": url})


@app.route("/api/auth/facebook/callback")
def facebook_callback():
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    error = request.args.get("error", "")

    if error:
        desc = request.args.get("error_description", error)
        return _callback_page(False, f"Facebook denied access: {desc}")

    # Decode state: "username:token"
    try:
        username, state_token = state.split(":", 1)
    except Exception:
        username    = session.get("username", "")
        state_token = state

    if not username:
        return _callback_page(False, "Session lost — please log in again and retry.")

    users = load_users()
    if username not in users:
        return _callback_page(False, "Unknown user.")

    try:
        token_res = requests.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={"client_id": FB_APP_ID, "client_secret": FB_APP_SECRET,
                    "redirect_uri": FB_REDIRECT_URI, "code": code}, timeout=15
        ).json()
        user_token = token_res.get("access_token")
        if not user_token:
            return f"<script>window.close();</script>Token error: {token_res}", 400

        pages_res = requests.get(
            "https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": user_token}, timeout=10
        ).json()
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

            # Check for linked Instagram Business account
            ig_res = requests.get(
                f"https://graph.facebook.com/v19.0/{page_id}",
                params={"fields": "instagram_business_account", "access_token": page_token},
                timeout=10
            ).json()
            ig_account = ig_res.get("instagram_business_account", {})
            ig_id = ig_account.get("id")
            if ig_id:
                ig_info = requests.get(
                    f"https://graph.facebook.com/v19.0/{ig_id}",
                    params={"fields": "username", "access_token": page_token}, timeout=10
                ).json()
                cfg["instagram_access_token"] = page_token
                cfg["instagram_account_id"]   = ig_id
                cfg["instagram_username"]     = ig_info.get("username", ig_id)
                cfg["instagram_via_facebook"]  = True
                add_log(username, f"Instagram connected ✓ — @{cfg['instagram_username']}", "ok")
                msg = f"Facebook '{page_name}' + Instagram @{cfg['instagram_username']} connected!"
            else:
                msg = f"Facebook page '{page_name}' connected! (No Instagram Business account found)"

            save_config(username, cfg)
            add_log(username, f"Facebook connected ✓ — {page_name}", "ok")
        else:
            cfg["facebook_access_token"] = user_token
            save_config(username, cfg)
            msg = "Facebook connected (no pages found)"
            add_log(username, "Facebook connected (no pages found)", "warn")

        return (
            f"<html><body><script>"
            f"window.opener && window.opener.postMessage('channel_connected:facebook:1','*');"
            f"window.close();"
            f"</script><p>{msg} Closing...</p></body></html>"
        )
    except Exception as e:
        return f"<script>window.close();</script>Error: {e}", 500


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
    """Disconnect Instagram (works for both via-Facebook and direct connections)."""
    cfg = load_config(username)
    for k in ["instagram_access_token", "instagram_account_id",
              "instagram_username", "instagram_via_facebook", "instagram_token_expires"]:
        cfg.pop(k, None)
    save_config(username, cfg)
    add_log(username, "Instagram disconnected", "warn")
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM DIRECT OAUTH (independent of Facebook)
#  Uses Instagram Graph API with its own app credentials:
#    INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET, INSTAGRAM_REDIRECT_URI
#
#  Flow:
#   1. GET  /api/auth/instagram_direct  → returns Instagram OAuth URL
#   2. User logs in on Instagram dialog
#   3. Instagram redirects to /api/auth/instagram_direct/callback
#   4. We exchange code → short-lived token → long-lived token
#   5. Fetch Instagram Business/Creator account ID + username
#   6. Store token + account_id + username in user config
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/instagram_direct")
@require_auth
def instagram_direct_auth(username):
    """Start standalone Instagram OAuth (Graph API, no Facebook required)."""
    if not IG_APP_ID:
        return jsonify({
            "ok":      False,
            "message": "INSTAGRAM_APP_ID not set in environment. "
                       "Add INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET, and "
                       "INSTAGRAM_REDIRECT_URI to your Railway env vars."
        }), 400

    state_token = secrets.token_hex(16)
    session["ig_direct_state"] = state_token

    # Instagram Graph API OAuth dialog
    # Scopes needed for business publishing:
    #   instagram_basic, instagram_content_publish, pages_show_list (if using FB-linked),
    #   instagram_manage_insights (optional)
    params = {
        "client_id":     IG_APP_ID,
        "redirect_uri":  IG_REDIRECT_URI,
        "scope":         "instagram_basic,instagram_content_publish,instagram_manage_insights",
        "response_type": "code",
        "state":         state_token,
    }
    # Instagram uses the Facebook OAuth dialog even for standalone IG apps
    url = "https://www.facebook.com/v19.0/dialog/oauth?" + urllib.parse.urlencode(params)
    return jsonify({"ok": True, "auth_url": url})


@app.route("/api/auth/instagram_direct/callback")
def instagram_direct_callback():
    """Handle callback from standalone Instagram OAuth."""
    username = session.get("username")
    if not username:
        return "<script>window.close();</script>Not authenticated", 401

    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    error = request.args.get("error", "")

    if error:
        err_reason = request.args.get("error_reason", "")
        err_desc   = request.args.get("error_description", "Unknown error")
        return (
            f"<html><body><script>window.close();</script>"
            f"<p>Instagram auth error: {err_desc}</p></body></html>"
        ), 400

    if state != session.get("ig_direct_state"):
        return "<script>window.close();</script>Invalid state — please try again", 400

    try:
        # Step 1: Exchange code for short-lived user access token
        token_res = requests.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "client_id":     IG_APP_ID,
                "client_secret": IG_APP_SECRET,
                "redirect_uri":  IG_REDIRECT_URI,
                "code":          code,
            },
            timeout=15
        ).json()

        short_token = token_res.get("access_token")
        if not short_token:
            err = token_res.get("error", {})
            return (
                f"<script>window.close();</script>"
                f"Token exchange failed: {err.get('message', str(token_res))}"
            ), 400

        # Step 2: Exchange short-lived token for long-lived token (60 days)
        long_res = requests.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "grant_type":        "fb_exchange_token",
                "client_id":         IG_APP_ID,
                "client_secret":     IG_APP_SECRET,
                "fb_exchange_token": short_token,
            },
            timeout=15
        ).json()
        long_token  = long_res.get("access_token", short_token)
        expires_in  = long_res.get("expires_in", 5184000)  # ~60 days

        # Step 3: Find the user's Instagram Business/Creator account
        # First, get their Facebook Pages (needed even for "direct" IG app)
        pages_res = requests.get(
            "https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": long_token, "fields": "id,name,instagram_business_account"},
            timeout=10
        ).json()
        pages = pages_res.get("data", [])

        ig_id       = None
        ig_username = None
        ig_token    = long_token

        for page in pages:
            ig_acct = page.get("instagram_business_account", {})
            if ig_acct.get("id"):
                ig_id = ig_acct["id"]
                # Get the page token (needed to publish via this IG account)
                page_tok_res = requests.get(
                    f"https://graph.facebook.com/v19.0/{page['id']}",
                    params={"fields": "access_token", "access_token": long_token},
                    timeout=10
                ).json()
                ig_token = page_tok_res.get("access_token", long_token)
                break

        if not ig_id:
            # Try fetching IG account directly via /me for Instagram-native app
            me_res = requests.get(
                "https://graph.facebook.com/v19.0/me",
                params={"fields": "id,name", "access_token": long_token},
                timeout=10
            ).json()
            # If still no IG account found, surface a helpful error
            return (
                "<html><body><script>window.close();</script>"
                "<p style='font-family:sans-serif;padding:20px'>"
                "⚠ No Instagram Business or Creator account found linked to this Facebook login.<br><br>"
                "Make sure:<br>"
                "• Your Instagram account is set to Business or Creator<br>"
                "• It is linked to a Facebook Page<br>"
                "• You granted all requested permissions"
                "</p></body></html>"
            ), 400

        # Step 4: Get Instagram username
        ig_info = requests.get(
            f"https://graph.facebook.com/v19.0/{ig_id}",
            params={"fields": "username,name", "access_token": ig_token},
            timeout=10
        ).json()
        ig_username = ig_info.get("username") or ig_info.get("name") or ig_id

        # Step 5: Save to config (marks as direct connection)
        cfg = load_config(username)
        cfg["instagram_access_token"] = ig_token
        cfg["instagram_account_id"]   = ig_id
        cfg["instagram_username"]     = ig_username
        cfg["instagram_via_facebook"] = False  # ← direct connection flag
        cfg["instagram_token_expires"] = (
            datetime.utcnow() + timedelta(seconds=expires_in)
        ).isoformat()
        save_config(username, cfg)
        add_log(username, f"Instagram (direct) connected ✓ — @{ig_username}", "ok")

        return (
            "<html><body><script>"
            "window.opener && window.opener.postMessage('channel_connected:instagram:1','*');"
            "window.close();"
            "</script>"
            f"<p>Instagram @{ig_username} connected directly! Closing...</p>"
            "</body></html>"
        )

    except Exception as e:
        add_log(username, f"Instagram direct OAuth error: {e}", "error")
        return f"<script>window.close();</script>Error: {e}", 500


@app.route("/api/auth/instagram_direct/disconnect", methods=["POST"])
@require_auth
def instagram_direct_disconnect(username):
    """Disconnect standalone Instagram account."""
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
@app.route("/api/status")
@require_auth
def get_status(username):
    state    = get_state(username)
    next_run = get_next_run_time_for_user(username)
    subjects_file = user_subjects_path(username)
    subjects = []
    if os.path.exists(subjects_file):
        with open(subjects_file, "r", encoding="utf-8") as f:
            subjects = [l.strip() for l in f if l.strip()]
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", None)

    # Build channels_info for dashboard
    channels_info = []
    for slot in [1, 2, 3]:
        tok  = cfg.get(f"linkedin_{slot}_access_token", "")
        name = cfg.get(f"linkedin_{slot}_name", f"LinkedIn #{slot}")
        channels_info.append({
            "slot":     slot,
            "platform": "linkedin",
            "name":     name if tok else f"LinkedIn #{slot}",
            "exists":   bool(tok),
            "active":   bool(tok),
        })
    # Facebook
    fb_tok = cfg.get("facebook_access_token", "")
    channels_info.append({
        "slot":     4,
        "platform": "facebook",
        "name":     cfg.get("facebook_page_name", "Facebook Page"),
        "exists":   bool(fb_tok),
        "active":   bool(fb_tok),
    })
    # Instagram
    ig_tok = cfg.get("instagram_access_token", "")
    channels_info.append({
        "slot":         5,
        "platform":     "instagram",
        "name":         f"@{cfg.get('instagram_username', 'instagram')}" if ig_tok else "Instagram",
        "exists":       bool(ig_tok),
        "active":       bool(ig_tok),
        "via_facebook": cfg.get("instagram_via_facebook", False),
    })

    return jsonify({
        "status":        state["status"],
        "running":       state["running"],
        "today_count":   state["today_count"],
        "total_run":     state["total_run"],
        "last_run":      state["last_run"],
        "next_run_iso":  next_run.isoformat() + "Z",
        "seconds_left":  max(0, int((next_run - datetime.utcnow()).total_seconds())),
        "subjects":      subjects,
        "logs":          state["logs"][-30:],
        "utc_offset":    offset,
        "config":        cfg,
        "channels_info": channels_info,
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
    content_type = data.get("content_type", "post")
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
            else:
                add_log(username, f"Upload HTTP {res.status_code}", "error")
        except Exception as e:
            add_log(username, f"Upload error: {e}", "error")
    subjects_file = user_subjects_path(username)
    with open(subjects_file, "a", encoding="utf-8") as f:
        for s in new_subjects:
            line = s.strip()
            if content_type == "article":
                line = f"{line} (article)"
            elif mode == "auto_image":
                line = f"{line} (create image)"
            elif mode == "manual_image" and uploaded_url:
                line = f"{line} | IMG: {uploaded_url}"
            f.write(line + "\n")
    add_log(username, f"Added {len(new_subjects)} subject(s) [{content_type}/{mode}] ✓", "ok")
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

# ─── STARTUP ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"[STARTUP] LinkedIn Agent on port {port}")
    print(f"[STARTUP] Users: {USERS_FILE}")
    print(f"[STARTUP] Data:  {USER_DATA_DIR}/")
    print(f"[STARTUP] LinkedIn OAuth: {'✓ configured' if LI_CLIENT_ID else '✗ LINKEDIN_CLIENT_ID not set'}")
    print(f"[STARTUP] Facebook OAuth: {'✓ configured' if FB_APP_ID else '✗ FACEBOOK_APP_ID not set'}")
    print(f"[STARTUP] Instagram OAuth: {'✓ configured' if IG_APP_ID else '✗ INSTAGRAM_APP_ID not set (optional — only needed for direct IG connect)'}")
    app.run(host="0.0.0.0", port=port, debug=False)
