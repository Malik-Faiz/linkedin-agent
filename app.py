import requests
import time
import re
import warnings
import os
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

warnings.filterwarnings('ignore', category=DeprecationWarning)

# ─── API KEYS ────────────────────────────────────────────────────────────────
GROQ_API_KEY = "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"
BUFFER_API_KEY = "d6uQ82pUexcxVpA6CTgcIaOnAnFkQ_o4XRj9ux-NYx3"
BUFFER_CHANNEL_ID = "6a0ed242090476fb99433477"

# NEW: Paste your free SerpApi key here
SERPAPI_API_KEY = "7aa81bd2ac8b9e77e2522ec091bd44ffd1eaf0083184bf300980c7d5abf7b447"


# ─── CONFIG ──────────────────────────────────────────────────────────────────
SUBJECTS_FILE = "subjects.txt"
BATCH_SIZE    = 2
TARGET_HOUR   = 8

SYSTEM_PROMPT_POST = """You are an expert LinkedIn ghostwriter.
Write a highly engaging, professional LinkedIn post based on the user's subject.
1. Hook on the first line wrapped in **asterisks**.
2. Short, punchy sentences.
3. Call-To-Action (CTA) question at the end.
4. 3 to 5 relevant hashtags."""

# ─── STATE ───────────────────────────────────────────────────────────────────
agent_state = {
    "status":      "waiting",
    "logs":        [],
    "today_count": 0,
    "total_run":   0,
    "last_run":    None,
    "next_run":    None,
    "running":     False
}

app = Flask(__name__)
CORS(app)

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def to_unicode_bold(text):
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b_chars = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    trans = str.maketrans(normal, b_chars)
    return text.translate(trans)

def format_linkedin_bold(text):
    def replace_with_bold(match):
        return to_unicode_bold(match.group(1))
    return re.sub(r'\*\*(.*?)\*\*', replace_with_bold, text)

def add_log(msg, level="info"):
    now = datetime.now()
    entry = {
        "time":  now.strftime("%H:%M:%S"),
        "msg":   msg,
        "level": level
    }
    agent_state["logs"].append(entry)
    if len(agent_state["logs"]) > 100:
        agent_state["logs"] = agent_state["logs"][-100:]
    print(f"[{entry['time']}] [{level.upper()}] {msg}")

def get_next_run_time():
    now    = datetime.now()
    target = now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target

# ─── CORE FUNCTIONS ──────────────────────────────────────────────────────────
def generate_post(subject):
    clean_subject = subject.replace("(create image)", "").strip()
    url     = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    data    = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_POST},
            {"role": "user",   "content": f"Write a post about: {clean_subject}"}
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30).json()
        if "error" in response:
            add_log(f"Groq Error: {response['error']['message']}", "error")
            return None
        return format_linkedin_bold(response["choices"][0]["message"]["content"])
    except Exception as e:
        add_log(f"Groq network failure: {e}", "error")
        return None

def get_image_url(subject):
    clean_subject = subject.replace("(create image)", "").strip()
    query  = f"{clean_subject} professional infographic"
    params = {"engine": "google_images", "q": query, "api_key": SERPAPI_API_KEY}
    add_log(f"SerpAPI image search: '{query}'", "info")
    try:
        response = requests.get("https://serpapi.com/search.json", params=params, timeout=20).json()
        if "images_results" in response and response["images_results"]:
            return response["images_results"][0]["original"]
        add_log("No image found via SerpAPI", "warn")
        return None
    except Exception as e:
        add_log(f"SerpAPI Error: {e}", "error")
        return None

def send_to_buffer(post_text, image_url=None):
    url     = "https://api.buffer.com/1/graphql"
    headers = {"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"}
    input_data = {
        "text":           post_text,
        "channelId":      BUFFER_CHANNEL_ID,
        "schedulingType": "automatic",
        "mode":           "addToQueue"
    }
    if image_url:
        input_data["assets"] = [{"image": {"url": image_url}}]
    payload = {
        "query": """mutation CreatePost($input: CreatePostInput!) {
            createPost(input: $input) {
                ... on PostActionSuccess { post { id } }
                ... on MutationError { message }
            }
        }""",
        "variables": {"input": input_data}
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        return response.status_code, response.json()
    except Exception as e:
        return 500, {"error": str(e)}

# ─── BATCH ENGINE ─────────────────────────────────────────────────────────────
def run_batch(triggered_by="scheduler"):
    if agent_state["running"]:
        add_log("Batch already running, skipping.", "warn")
        return

    agent_state["running"]     = True
    agent_state["status"]      = "running"
    agent_state["today_count"] = 0
    add_log(f"Batch started (trigger: {triggered_by})", "info")

    if not os.path.exists(SUBJECTS_FILE):
        add_log(f"'{SUBJECTS_FILE}' not found!", "error")
        agent_state["running"] = False
        agent_state["status"]  = "waiting"
        return

    with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
        all_subjects = [line.strip() for line in f if line.strip()]

    if not all_subjects:
        add_log("subjects.txt is empty. Add more subjects!", "warn")
        agent_state["running"] = False
        agent_state["status"]  = "waiting"
        return

    batch = all_subjects[:BATCH_SIZE]
    with open(SUBJECTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_subjects[BATCH_SIZE:]))

    add_log(f"Loaded {len(batch)} subjects. {len(all_subjects) - len(batch)} remaining.", "info")

    for j, subject in enumerate(batch):
        add_log(f"[{j+1}/{len(batch)}] Processing: {subject[:50]}", "info")

        post_text = generate_post(subject)
        if not post_text:
            add_log(f"[{j+1}/{len(batch)}] Skipping — post generation failed.", "error")
            continue

        add_log(f"[{j+1}/{len(batch)}] Post generated via Groq ✓", "ok")

        image_url = None
        if "(create image)" in subject.lower():
            image_url = get_image_url(subject)
            if image_url:
                add_log(f"[{j+1}/{len(batch)}] Image fetched via SerpAPI ✓", "ok")

        status, result = send_to_buffer(post_text, image_url)
        if status == 200 and "errors" not in result:
            post_id = result.get("data", {}).get("createPost", {}).get("post", {}).get("id", "unknown")
            add_log(f"[{j+1}/{len(batch)}] Queued to Buffer ✓ Post ID: {post_id}", "ok")
            agent_state["today_count"] += 1
            agent_state["total_run"]   += 1
        else:
            add_log(f"[{j+1}/{len(batch)}] Buffer failed (HTTP {status}): {result}", "error")

        time.sleep(10)

    agent_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agent_state["running"]  = False
    agent_state["status"]   = "waiting"
    add_log(f"Batch complete! {agent_state['today_count']} posts sent to Buffer.", "ok")

# ─── SCHEDULER THREAD ─────────────────────────────────────────────────────────
def scheduler_loop():
    add_log("Scheduler started — waiting for 08:00 AM trigger.", "info")
    while True:
        now = datetime.now()
        if now.hour == TARGET_HOUR and now.minute == 0 and now.second < 5:
            add_log("08:00 AM trigger fired — launching auto batch!", "warn")
            threading.Thread(target=run_batch, args=("scheduler",), daemon=True).start()
            time.sleep(60)
        time.sleep(1)

# ─── KEEP ALIVE (self-ping every 10 mins) ─────────────────────────────────────
def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            port = os.environ.get("PORT", 5000)
            url  = os.environ.get("RENDER_EXTERNAL_URL") or \
                   os.environ.get("RAILWAY_STATIC_URL") or \
                   f"http://localhost:{port}"
            requests.get(f"{url}/ping", timeout=10)
            add_log("Keep-alive ping sent ✓", "info")
        except Exception as e:
            add_log(f"Keep-alive ping failed: {e}", "warn")
        time.sleep(600)

# ─── FLASK ROUTES ─────────────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "time": datetime.now().strftime("%H:%M:%S")})

@app.route("/")
def home():
    return jsonify({"message": "LinkedIn Agent is running!", "status": agent_state["status"]})

@app.route("/api/status")
def get_status():
    next_run = get_next_run_time()
    now      = datetime.now()
    diff     = (next_run - now).total_seconds()

    subjects = []
    if os.path.exists(SUBJECTS_FILE):
        with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
            subjects = [line.strip() for line in f if line.strip()]

    return jsonify({
        "status":       agent_state["status"],
        "running":      agent_state["running"],
        "today_count":  agent_state["today_count"],
        "total_run":    agent_state["total_run"],
        "last_run":     agent_state["last_run"],
        "next_run_iso": next_run.isoformat(),
        "seconds_left": max(0, int(diff)),
        "subjects":     subjects,
        "logs":         agent_state["logs"][-30:]
    })

@app.route("/api/run", methods=["POST"])
def manual_run():
    if agent_state["running"]:
        return jsonify({"ok": False, "message": "Batch already running"}), 409
    threading.Thread(target=run_batch, args=("manual",), daemon=True).start()
    return jsonify({"ok": True, "message": "Manual batch triggered"})

@app.route("/api/subjects", methods=["GET"])
def get_subjects():
    subjects = []
    if os.path.exists(SUBJECTS_FILE):
        with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
            subjects = [line.strip() for line in f if line.strip()]
    return jsonify({"subjects": subjects})

@app.route("/api/subjects", methods=["POST"])
def add_subjects():
    data         = request.get_json()
    new_subjects = data.get("subjects", [])
    if not new_subjects:
        return jsonify({"ok": False, "message": "No subjects provided"}), 400
    with open(SUBJECTS_FILE, "a", encoding="utf-8") as f:
        for s in new_subjects:
            f.write(s.strip() + "\n")
    add_log(f"Added {len(new_subjects)} new subject(s) to queue.", "ok")
    return jsonify({"ok": True, "added": len(new_subjects)})

# ─── STARTUP ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not os.path.exists(SUBJECTS_FILE):
        with open(SUBJECTS_FILE, "w", encoding="utf-8") as f:
            f.write("How AI is reshaping B2B SaaS workflows\n")
            f.write("The rise of async-first remote teams (create image)\n")
            f.write("Why most LinkedIn hooks fail and how to fix them\n")
            f.write("Prompt engineering for product managers (create image)\n")
            f.write("Zero to 10k MRR lessons from indie hackers\n")
            f.write("Building a personal brand from scratch in 90 days\n")
        print("✅ Created default subjects.txt")

    # Start scheduler thread (waits for 8 AM)
    threading.Thread(target=scheduler_loop, daemon=True).start()

    # Start keep-alive thread (pings every 10 mins)
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    add_log(f"🚀 LinkedIn Agent started on port {port}", "info")
    add_log("⏰ Scheduler active — will auto-run at 08:00 AM", "ok")
    add_log("💓 Keep-alive ping active every 10 minutes", "ok")
    app.run(host="0.0.0.0", port=port, debug=False)