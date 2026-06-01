import requests
import time
import re
import warnings
import os
import threading
import json
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, session, send_from_directory
from flask_cors import CORS

warnings.filterwarnings('ignore', category=DeprecationWarning)

# ─── PATHS ───────────────────────────────────────────────────────────────────
_DATA_ROOT    = "/data" if os.path.isdir("/data") else "."
USERS_FILE    = os.path.join(_DATA_ROOT, "users.json")
USER_DATA_DIR = os.path.join(_DATA_ROOT, "user_data")

BATCH_SIZE  = 2
TARGET_HOUR = 8

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

# ─── HTML FILE LOADER ─────────────────────────────────────────────────────────
def load_html(name):
    path = os.path.join(os.path.dirname(__file__), name)
    if os.path.exists(path):
        return open(path, encoding="utf-8").read()
    return f"<h1>{name} not found</h1>"

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
        return json.load(f)

def save_config(username, cfg):
    with open(user_config_path(username), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

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

# In-memory state per user
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

# ─── CORE HELPERS ─────────────────────────────────────────────────────────────
def to_unicode_bold(text):
    normal  = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b_chars = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return text.translate(str.maketrans(normal, b_chars))

def format_linkedin_bold(text):
    return re.sub(r'\*\*(.*?)\*\*', lambda m: to_unicode_bold(m.group(1)), text)

def get_next_run_time_for_user(username):
    """Return the next 08:00 in user's local timezone as a UTC datetime."""
    cfg    = load_config(username)
    offset = cfg.get("utc_offset_hours", 0)
    now_utc  = datetime.utcnow()
    user_now = now_utc + timedelta(hours=offset)
    target_local = user_now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
    if user_now >= target_local:
        target_local += timedelta(days=1)
    return target_local - timedelta(hours=offset)

# ─── IMAGE VALIDATION ─────────────────────────────────────────────────────────
def validate_image_url(url, timeout=8):
    """
    Check that a URL returns a reachable image.
    Returns True only if HTTP 200 and content-type is image/*.
    Falls back to streaming GET if server rejects HEAD.
    """
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            if "image" in ct:
                return True
        # Some servers reject HEAD — fall back to a tiny streaming GET
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
    """Generate a short LinkedIn post."""
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
    """Generate a long-form LinkedIn article."""
    cfg      = load_config(username)
    groq_key = cfg.get("groq_api_key", "")
    clean    = subject.replace("(article)", "").strip()
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant",
                  "max_tokens": 2000,
                  "messages": [
                      {"role": "system", "content": SYSTEM_PROMPT_ARTICLE},
                      {"role": "user",   "content": f"Write a LinkedIn article about: {clean}"}
                  ]}, timeout=60
        ).json()
        if "error" in response:
            add_log(username, f"Groq Article Error: {response['error']['message']}", "error")
            return None, None
        raw   = response["choices"][0]["message"]["content"]
        lines = raw.strip().split("\n")
        title = clean   # default title
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
            "engine": "google_images",
            "q": f"{clean} infographic",
            "api_key": serp_key
        }, timeout=20).json()

        results = response.get("images_results", [])
        if not results:
            add_log(username, "SerpAPI returned no images.", "warn")
            return None

        # Try up to 5 candidates — skip any that are unreachable or expired
        for i, img in enumerate(results[:5]):
            url = img.get("original", "")
            if not url:
                continue
            add_log(username, f"Checking image {i+1}/5...", "info")
            if validate_image_url(url):
                add_log(username, f"Image {i+1} reachable ✓", "ok")
                return url
            else:
                add_log(username, f"Image {i+1} unreachable — trying next", "warn")

        add_log(username, "All candidate images unreachable — post sent without image.", "warn")
        return None

    except Exception as e:
        add_log(username, f"SerpAPI Error: {e}", "error")
        return None

# ─── CHANNEL HELPERS ──────────────────────────────────────────────────────────
# Channel platform defaults:
#   slot 4 → facebook (locked in UI)
#   slot 5 → instagram (locked in UI)
#   slots 1-3 → user-selectable (default: linkedin)
CHANNEL_PLATFORM_DEFAULTS = {4: "facebook", 5: "instagram"}

def get_active_channels(cfg):
    """Return list of (slot, channel_id, channel_name, platform) for all active channels."""
    active_list = cfg.get("active_channels", [1])
    result = []
    for slot in active_list:
        cid      = cfg.get(f"buffer_channel_{slot}", "").strip()
        name     = cfg.get(f"buffer_channel_{slot}_name", f"Channel {slot}")
        platform = cfg.get(f"buffer_channel_{slot}_platform",
                           CHANNEL_PLATFORM_DEFAULTS.get(slot, "linkedin"))
        if cid:
            result.append((slot, cid, name, platform))
    return result

# ─── BUFFER PUBLISHING ────────────────────────────────────────────────────────
def send_to_one_channel(buffer_key, channel_id, post_text, image_url=None, platform="linkedin"):
    input_data = {
        "text": post_text,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "addToQueue"
    }
    if platform == "facebook":
        input_data["metadata"] = {"facebook": {"type": "post"}}
    elif platform == "instagram":
        input_data["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    if image_url:
        input_data["assets"] = [{"image": {"url": image_url}}]
    try:
        response = requests.post(
            "https://api.buffer.com/1/graphql",
            headers={"Authorization": f"Bearer {buffer_key}", "Content-Type": "application/json"},
            json={"query": """mutation CreatePost($input: CreatePostInput!) {
                createPost(input: $input) {
                    ... on PostActionSuccess { post { id } }
                    ... on MutationError { message }
                }
            }""", "variables": {"input": input_data}}, timeout=20
        )
        return response.status_code, response.json()
    except Exception as e:
        return 500, {"error": str(e)}

def send_article_to_linkedin(buffer_key, channel_id, title, body):
    """Send a long-form article to a LinkedIn channel via Buffer."""
    input_data = {
        "text": body,
        "title": title,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "metadata": {"linkedin": {"type": "article"}}
    }
    try:
        response = requests.post(
            "https://api.buffer.com/1/graphql",
            headers={"Authorization": f"Bearer {buffer_key}", "Content-Type": "application/json"},
            json={"query": """mutation CreatePost($input: CreatePostInput!) {
                createPost(input: $input) {
                    ... on PostActionSuccess { post { id } }
                    ... on MutationError { message }
                }
            }""", "variables": {"input": input_data}}, timeout=30
        )
        return response.status_code, response.json()
    except Exception as e:
        return 500, {"error": str(e)}

def send_to_buffer(username, post_text, image_url=None, is_article=False, article_title=None):
    cfg      = load_config(username)
    buf_key  = cfg.get("buffer_api_key", "")
    channels = get_active_channels(cfg)

    if not channels:
        add_log(username, "No active Buffer channels — toggle at least one ON.", "error")
        return 0

    success = 0
    for slot, cid, name, platform in channels:

        # Articles only publish to LinkedIn channels
        if is_article and platform != "linkedin":
            add_log(username, f"  → [{name}] Skipped — articles only go to LinkedIn", "info")
            continue

        if is_article:
            status, result = send_article_to_linkedin(buf_key, cid, article_title, post_text)
        else:
            status, result = send_to_one_channel(buf_key, cid, post_text, image_url, platform=platform)

        if status != 200:
            add_log(username, f"  → [{name}] HTTP {status}: {result}", "error")
            continue

        if "errors" in result:
            errs = "; ".join(e.get("message", str(e)) for e in result["errors"])
            add_log(username, f"  → [{name}] GraphQL error: {errs}", "error")
            continue

        create_post = (result.get("data") or {}).get("createPost") or {}

        if "message" in create_post:
            err_msg = create_post["message"]
            # Image fallback — retry without image if Buffer rejects it
            if image_url and not is_article and (
                "image" in err_msg.lower() or
                "dimensions" in err_msg.lower() or
                "fetch" in err_msg.lower()
            ):
                add_log(username, f"  → [{name}] Image rejected — retrying without image", "warn")
                status2, result2 = send_to_one_channel(buf_key, cid, post_text, None, platform=platform)
                cp2 = (result2.get("data") or {}).get("createPost") or {}
                if status2 == 200 and "post" in cp2:
                    pid = (cp2.get("post") or {}).get("id")
                    add_log(username, f"  → [{name}] Queued without image ✓ ID: {pid}", "ok")
                    success += 1
                    continue
                else:
                    add_log(username, f"  → [{name}] Retry also failed: {cp2.get('message','unknown')}", "error")
            else:
                add_log(username, f"  → [{name}] Buffer error: {err_msg}", "error")
            continue

        pid   = (create_post.get("post") or {}).get("id")
        label = "article" if is_article else "post"
        add_log(username, f"  → [{name}] Queued {label} ✓ ID: {pid or 'n/a'}", "ok")
        success += 1

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
        state["running"] = False
        state["status"]  = "waiting"
        return

    with open(subjects_file, "r", encoding="utf-8") as f:
        all_subjects = [l.strip() for l in f if l.strip()]

    if not all_subjects:
        add_log(username, "Queue is empty!", "warn")
        state["running"] = False
        state["status"]  = "waiting"
        return

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
            # ── ARTICLE FLOW ──
            title, body = generate_article(username, base_subject)
            if not title or not body:
                continue
            add_log(username, f"[{j+1}/{len(batch)}] Article generated ✓ — {title[:40]}", "ok")
            sent = send_to_buffer(username, body, is_article=True, article_title=title)

        else:
            # ── POST FLOW ──
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

            sent = send_to_buffer(username, post_text, image_url)

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

# ─── KEEP ALIVE ───────────────────────────────────────────────────────────────
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
def page_login():
    return load_html("login.html")

@app.route("/setup")
def page_setup():
    return load_html("setup.html")

@app.route("/dashboard")
def page_dashboard():
    return load_html("dashboard.html")

@app.route("/ping")
def ping():
    # Always UTC — frontend converts to user local time
    utc_now = datetime.utcnow()
    return jsonify({
        "status":   "alive",
        "utc_time": utc_now.strftime("%H:%M:%S"),
        "utc_iso":  utc_now.isoformat() + "Z"
    })

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
    has_config = bool(load_config(username).get("groq_api_key") and load_config(username).get("buffer_api_key"))
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
    has_config = bool(cfg.get("groq_api_key") and cfg.get("buffer_api_key"))
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
    has_config = bool(cfg.get("groq_api_key") and cfg.get("buffer_api_key"))
    return jsonify({"ok": True, "authenticated": True, "username": username, "has_config": has_config})

# ════════════════════════════════════════════════════════════════════════════════
#  CONFIG ROUTES
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/config", methods=["GET"])
@require_auth
def get_config(username):
    cfg = load_config(username)
    no_mask = {
        "active_channel", "utc_offset_hours", "active_channels",
        "buffer_channel_1",          "buffer_channel_2",          "buffer_channel_3",
        "buffer_channel_4",          "buffer_channel_5",
        "buffer_channel_1_name",     "buffer_channel_2_name",     "buffer_channel_3_name",
        "buffer_channel_4_name",     "buffer_channel_5_name",
        "buffer_channel_1_platform", "buffer_channel_2_platform", "buffer_channel_3_platform",
        "buffer_channel_4_platform", "buffer_channel_5_platform",
    }
    masked = {}
    for k, v in cfg.items():
        if k in no_mask:
            masked[k] = v
        elif isinstance(v, str) and v and len(v) > 8:
            masked[k] = "*" * (len(v) - 4) + v[-4:]
        else:
            masked[k] = v
    return jsonify({"ok": True, "config": masked})

@app.route("/api/config", methods=["POST"])
@require_auth
def save_config_route(username):
    data = request.get_json()
    cfg  = load_config(username)

    # Only overwrite sensitive keys when a real non-masked value is sent
    for field in ["groq_api_key", "buffer_api_key", "serpapi_key"]:
        val = (data.get(field) or "").strip()
        if val and "*" not in val:
            cfg[field] = val

    # Channels 1–5: ID, name, platform
    for i in [1, 2, 3, 4, 5]:
        cid      = (data.get(f"buffer_channel_{i}") or "").strip()
        name     = (data.get(f"buffer_channel_{i}_name") or "").strip()
        # Enforce locked platforms for slots 4 & 5 regardless of what frontend sends
        if i == 4:
            platform = "facebook"
        elif i == 5:
            platform = "instagram"
        else:
            platform = (data.get(f"buffer_channel_{i}_platform") or "").strip() or "linkedin"

        if cid and "*" not in cid:
            cfg[f"buffer_channel_{i}"] = cid
        if name and "*" not in name:
            cfg[f"buffer_channel_{i}_name"] = name
        # Always save the platform (even if no new ID — keeps it consistent)
        if cid or cfg.get(f"buffer_channel_{i}"):
            cfg[f"buffer_channel_{i}_platform"] = platform

    if "active_channels" not in cfg:
        cfg["active_channels"] = [1]

    save_config(username, cfg)
    active_list = cfg.get("active_channels", [1])
    names = [cfg.get(f"buffer_channel_{i}_name", f"Channel {i}") for i in active_list]
    add_log(username, f"Config updated ✓ — Active: {', '.join(names)}", "ok")
    return jsonify({"ok": True, "message": "Configuration saved!", "active_channels": active_list})

# ════════════════════════════════════════════════════════════════════════════════
#  CHANNEL ROUTES
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/channel/toggle", methods=["POST"])
@require_auth
def toggle_channel(username):
    data = request.get_json()
    ch   = data.get("channel")
    if ch not in [1, 2, 3, 4, 5]:
        return jsonify({"ok": False, "message": "Channel must be 1–5"}), 400
    cfg = load_config(username)
    if not cfg.get(f"buffer_channel_{ch}", "").strip():
        return jsonify({"ok": False, "message": f"Channel {ch} has no ID configured"}), 400

    active = cfg.get("active_channels", [1])
    if not isinstance(active, list):
        active = [active]

    if ch in active:
        if len(active) <= 1:
            return jsonify({"ok": False, "message": "At least one channel must remain active"}), 400
        active.remove(ch)
        state = "OFF"
    else:
        active.append(ch)
        active.sort()
        state = "ON"

    cfg["active_channels"] = active
    save_config(username, cfg)
    name = cfg.get(f"buffer_channel_{ch}_name", f"Channel {ch}")
    add_log(username, f"Channel [{name}] toggled {state} — Active: {active}", "ok")
    return jsonify({"ok": True, "active_channels": active, "channel": ch, "state": state})

@app.route("/api/channel/delete", methods=["POST"])
@require_auth
def delete_channel(username):
    data = request.get_json()
    ch   = data.get("channel")
    if ch not in [2, 3, 4, 5]:
        return jsonify({"ok": False, "message": "Only channels 2–5 can be deleted"}), 400
    cfg  = load_config(username)
    name = cfg.get(f"buffer_channel_{ch}_name", f"Channel {ch}")

    for key in [f"buffer_channel_{ch}", f"buffer_channel_{ch}_name", f"buffer_channel_{ch}_platform"]:
        cfg.pop(key, None)

    active = cfg.get("active_channels", [1])
    if not isinstance(active, list):
        active = [active]
    if ch in active:
        active.remove(ch)
    if not active:
        active = [1]
    cfg["active_channels"] = active

    save_config(username, cfg)
    add_log(username, f"Channel [{name}] deleted from slot {ch}", "warn")
    return jsonify({"ok": True, "message": f"Channel {ch} deleted"})

# ════════════════════════════════════════════════════════════════════════════════
#  STATUS ROUTE
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

    cfg        = load_config(username)
    offset     = cfg.get("utc_offset_hours", None)
    active_chs = cfg.get("active_channels", [1])
    if not isinstance(active_chs, list):
        active_chs = [active_chs]

    channels_info = []
    for i in [1, 2, 3, 4, 5]:
        cid      = cfg.get(f"buffer_channel_{i}", "").strip()
        name     = cfg.get(f"buffer_channel_{i}_name", f"Channel {i}")
        platform = cfg.get(f"buffer_channel_{i}_platform",
                           CHANNEL_PLATFORM_DEFAULTS.get(i, "linkedin"))
        channels_info.append({
            "slot":     i,
            "id":       cid,
            "name":     name,
            "platform": platform,
            "active":   (i in active_chs) and bool(cid),
            "exists":   bool(cid),
        })

    return jsonify({
        "status":          state["status"],
        "running":         state["running"],
        "today_count":     state["today_count"],
        "total_run":       state["total_run"],
        "last_run":        state["last_run"],
        "next_run_iso":    next_run.isoformat() + "Z",
        "seconds_left":    max(0, int((next_run - datetime.utcnow()).total_seconds())),
        "subjects":        subjects,
        "logs":            state["logs"][-30:],
        "utc_offset":      offset,
        "active_channels": active_chs,
        "channels_info":   channels_info,
        "config":          cfg,
    })

# ════════════════════════════════════════════════════════════════════════════════
#  MANUAL RUN
# ════════════════════════════════════════════════════════════════════════════════
@app.route("/api/run", methods=["POST"])
@require_auth
def manual_run(username):
    state = get_state(username)
    if state["running"]:
        return jsonify({"ok": False, "message": "Already running"}), 409
    threading.Thread(target=run_batch, args=(username, "manual"), daemon=True).start()
    return jsonify({"ok": True, "message": "Batch triggered!"})

# ════════════════════════════════════════════════════════════════════════════════
#  SUBJECTS ROUTES
# ════════════════════════════════════════════════════════════════════════════════
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
    content_type = data.get("content_type", "post")  # "post" or "article"

    if not new_subjects:
        return jsonify({"ok": False, "message": "No subjects provided"}), 400

    uploaded_url = None
    if mode == "manual_image" and image_b64:
        try:
            if "," in image_b64:
                image_b64 = image_b64.split(",")[1]
            add_log(username, f"Uploading '{filename}'...", "info")
            res = requests.post("https://freeimage.host/api/1/upload", data={
                "key":    "6d207e02198a847aa98d0a2a901485a5",
                "action": "upload",
                "source": image_b64,
                "format": "json"
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
    removed = subjects.pop(index)
    with open(subjects_file, "w", encoding="utf-8") as f:
        f.write("\n".join(subjects))
    return jsonify({"ok": True, "removed": removed})

# ─── STARTUP ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"[STARTUP] LinkedIn Agent on port {port}")
    print(f"[STARTUP] Users: {USERS_FILE}")
    print(f"[STARTUP] Data:  {USER_DATA_DIR}/")
    print(f"[STARTUP] Scheduler fires at {TARGET_HOUR:02d}:00 per user's local timezone")
    print(f"[STARTUP] Pages: / → login.html | /setup → setup.html | /dashboard → dashboard.html")
    app.run(host="0.0.0.0", port=port, debug=False)
