import requests
import time
import re
import warnings
import os
import threading
import base64
import json
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, session
from flask_cors import CORS

warnings.filterwarnings('ignore', category=DeprecationWarning)

# ─── PATHS ───────────────────────────────────────────────────────────────────
USERS_FILE    = "users.json"          # { username: { password_hash, salt } }
USER_DATA_DIR = "user_data"           # user_data/{username}/config.json
                                      # user_data/{username}/subjects.txt
                                      # user_data/{username}/state.json

BATCH_SIZE  = 2
TARGET_HOUR = 8

SYSTEM_PROMPT_POST = """You are an expert LinkedIn ghostwriter.
Write a highly engaging, professional LinkedIn post based on the user's subject.
1. Hook on the first line wrapped in **asterisks**.
2. Short, punchy sentences.
3. Call-To-Action (CTA) question at the end.
4. 3 to 5 relevant hashtags."""

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)   # session signing key
CORS(app, supports_credentials=True)

os.makedirs(USER_DATA_DIR, exist_ok=True)

# ─── DASHBOARD HTML ──────────────────────────────────────────────────────────
DASHBOARD = (
    open("dashboard.html", encoding="utf-8").read()
    if os.path.exists("dashboard.html")
    else "<h1>dashboard.html not found</h1>"
)

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
    # running flag should never persist across restarts
    s["running"] = False
    return s

def save_state(username, state):
    with open(user_state_path(username), "w", encoding="utf-8") as f:
        # Don't persist the full logs to disk every tick — only on batch events
        json.dump(state, f, indent=2)

# In-memory state per user (loaded on first access)
_states = {}
_state_lock = threading.Lock()

def get_state(username):
    with _state_lock:
        if username not in _states:
            _states[username] = load_state(username)
        return _states[username]

def add_log(username, msg, level="info"):
    state = get_state(username)
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
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
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b_chars = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return text.translate(str.maketrans(normal, b_chars))

def format_linkedin_bold(text):
    return re.sub(r'\*\*(.*?)\*\*', lambda m: to_unicode_bold(m.group(1)), text)

def get_next_run_time():
    now = datetime.now()
    target = now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target

# ─── CORE AI / POST FUNCTIONS ─────────────────────────────────────────────────
def generate_post(username, subject):
    cfg = load_config(username)
    groq_key = cfg.get("groq_api_key", "")
    clean = subject.replace("(create image)", "").strip()
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_POST},
                {"role": "user", "content": f"Write a post about: {clean}"}
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
    cfg = load_config(username)
    serp_key = cfg.get("serpapi_key", "")
    clean = subject.replace("(create image)", "").strip()
    try:
        response = requests.get("https://serpapi.com/search.json", params={
            "engine": "google_images", "q": f"{clean} infographic", "api_key": serp_key
        }, timeout=20).json()
        if "images_results" in response and response["images_results"]:
            return response["images_results"][0]["original"]
    except Exception as e:
        add_log(username, f"SerpAPI Error: {e}", "error")
    return None

def send_to_buffer(username, post_text, image_url=None):
    cfg = load_config(username)
    buffer_key    = cfg.get("buffer_api_key", "")
    buffer_channel = cfg.get("buffer_channel_id", "")
    input_data = {
        "text": post_text,
        "channelId": buffer_channel,
        "schedulingType": "automatic",
        "mode": "addToQueue"
    }
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

        if "| IMG:" in subject:
            parts = subject.split("| IMG:")
            base_subject     = parts[0].strip()
            manual_image_url = parts[1].strip()

        add_log(username, f"[{j+1}/{len(batch)}] {base_subject[:50]}", "info")
        post_text = generate_post(username, base_subject)
        if not post_text:
            continue
        add_log(username, f"[{j+1}/{len(batch)}] Post generated ✓", "ok")

        image_url = None
        if manual_image_url:
            image_url = manual_image_url
            add_log(username, f"[{j+1}/{len(batch)}] Using uploaded image ✓", "ok")
        elif "(create image)" in base_subject.lower() or state.get("_auto_img_" + str(j)):
            image_url = get_image_url(username, base_subject)
            if image_url:
                add_log(username, f"[{j+1}/{len(batch)}] Image fetched via SerpAPI ✓", "ok")

        status, result = send_to_buffer(username, post_text, image_url)
        if status == 200 and "errors" not in result:
            pid = result.get("data",{}).get("createPost",{}).get("post",{}).get("id","?")
            add_log(username, f"[{j+1}/{len(batch)}] Queued to Buffer ✓ ID: {pid}", "ok")
            state["today_count"] += 1
            state["total_run"]   += 1
        else:
            add_log(username, f"[{j+1}/{len(batch)}] Buffer failed: {result}", "error")
        time.sleep(10)

    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["running"]  = False
    state["status"]   = "waiting"
    add_log(username, f"Batch complete! {state['today_count']} posts sent.", "ok")
    save_state(username, state)

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def scheduler_loop():
    print("[SCHEDULER] Started — waiting for 08:00 AM")
    while True:
        now = datetime.now()
        if now.hour == TARGET_HOUR and now.minute == 0 and now.second < 5:
            users = load_users()
            for uname in users:
                threading.Thread(target=run_batch, args=(uname, "scheduler"), daemon=True).start()
            time.sleep(60)
        time.sleep(1)

def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            render_url  = os.environ.get("RENDER_EXTERNAL_URL")
            port        = os.environ.get("PORT", 5000)
            if railway_url:
                url = f"https://{railway_url}"
            elif render_url:
                url = render_url
            else:
                url = f"http://localhost:{port}"
            requests.get(f"{url}/ping", timeout=10)
        except Exception:
            pass
        time.sleep(600)

# ════════════════════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/")
def home():
    return DASHBOARD

@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "time": datetime.now().strftime("%H:%M:%S")})

# ── AUTH ──────────────────────────────────────────────────────────────────────
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

    session["username"] = username
    session.permanent = True
    return jsonify({"ok": True, "username": username, "message": "Account created!"})

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

    session["username"] = username
    session.permanent = True

    # Tell frontend whether APIs are already configured
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

# ── CONFIG ────────────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
@require_auth
def get_config(username):
    cfg = load_config(username)
    # Mask keys — only show last 4 chars
    masked = {}
    for k, v in cfg.items():
        if v and len(v) > 8:
            masked[k] = "*" * (len(v) - 4) + v[-4:]
        else:
            masked[k] = v
    return jsonify({"ok": True, "config": masked})

@app.route("/api/config", methods=["POST"])
@require_auth
def save_config_route(username):
    data = request.get_json()
    cfg  = load_config(username)  # preserve existing values
    fields = ["groq_api_key", "buffer_api_key", "buffer_channel_id", "serpapi_key"]
    for field in fields:
        val = (data.get(field) or "").strip()
        if val and not val.startswith("*"):  # don't overwrite with masked value
            cfg[field] = val
    save_config(username, cfg)
    add_log(username, "API configuration updated ✓", "ok")
    return jsonify({"ok": True, "message": "Configuration saved!"})

# ── STATUS ────────────────────────────────────────────────────────────────────
@app.route("/api/status")
@require_auth
def get_status(username):
    state         = get_state(username)
    next_run      = get_next_run_time()
    subjects_file = user_subjects_path(username)
    subjects = []
    if os.path.exists(subjects_file):
        with open(subjects_file, "r", encoding="utf-8") as f:
            subjects = [l.strip() for l in f if l.strip()]
    return jsonify({
        "status":       state["status"],
        "running":      state["running"],
        "today_count":  state["today_count"],
        "total_run":    state["total_run"],
        "last_run":     state["last_run"],
        "next_run_iso": next_run.isoformat(),
        "seconds_left": max(0, int((next_run - datetime.now()).total_seconds())),
        "subjects":     subjects,
        "logs":         state["logs"][-30:]
    })

# ── MANUAL RUN ────────────────────────────────────────────────────────────────
@app.route("/api/run", methods=["POST"])
@require_auth
def manual_run(username):
    state = get_state(username)
    if state["running"]:
        return jsonify({"ok": False, "message": "Already running"}), 409
    threading.Thread(target=run_batch, args=(username, "manual"), daemon=True).start()
    return jsonify({"ok": True, "message": "Batch triggered!"})

# ── SUBJECTS ──────────────────────────────────────────────────────────────────
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
    mode         = data.get("mode", "no_image")   # "no_image" | "auto_image" | "manual_image"

    if not new_subjects:
        return jsonify({"ok": False, "message": "No subjects provided"}), 400

    uploaded_url = None

    if mode == "manual_image" and image_b64:
        try:
            if "," in image_b64:
                image_b64 = image_b64.split(",")[1]
            add_log(username, f"Uploading '{filename}' to image host...", "info")
            res = requests.post("https://freeimage.host/api/1/upload", data={
                "key": "6d207e02198a847aa98d0a2a901485a5",
                "action": "upload",
                "source": image_b64,
                "format": "json"
            }, timeout=30)
            if res.status_code == 200:
                rj = res.json()
                if "image" in rj:
                    uploaded_url = rj["image"]["url"]
                    add_log(username, f"Upload success! URL: {uploaded_url}", "ok")
                else:
                    add_log(username, f"Upload failed: {res.text}", "error")
            else:
                add_log(username, f"Upload HTTP {res.status_code}: {res.text}", "error")
        except Exception as e:
            add_log(username, f"Image upload error: {e}", "error")

    subjects_file = user_subjects_path(username)
    with open(subjects_file, "a", encoding="utf-8") as f:
        for s in new_subjects:
            line = s.strip()
            if mode == "auto_image":
                line = f"{line} (create image)"
            elif mode == "manual_image" and uploaded_url:
                line = f"{line} | IMG: {uploaded_url}"
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

# ─── STARTUP ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"[STARTUP] LinkedIn Agent on port {port}")
    print(f"[STARTUP] Users file: {USERS_FILE}")
    print(f"[STARTUP] User data dir: {USER_DATA_DIR}/")
    app.run(host="0.0.0.0", port=port, debug=False)
