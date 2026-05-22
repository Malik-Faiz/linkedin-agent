import requests
import time
import re
import warnings
import os
import threading
import base64
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

warnings.filterwarnings('ignore', category=DeprecationWarning)
# ─── API KEYS ────────────────────────────────────────────────────────────────

GROQ_API_KEY = "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"
BUFFER_API_KEY = "OEF_iqPQxmZ4zJzwJ-PddYHlfaARGRriXnzamvWdFx8"
BUFFER_CHANNEL_ID = "67fe17592799bb0a23ef270d"
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
    "running":     False
}

app = Flask(__name__)
CORS(app)

# ─── DASHBOARD HTML ──────────────────────────────────────────────────────────
DASHBOARD = open("dashboard.html", encoding="utf-8").read() if os.path.exists("dashboard.html") else "<h1>dashboard.html not found</h1>"

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def to_unicode_bold(text):
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b_chars = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return text.translate(str.maketrans(normal, b_chars))

def format_linkedin_bold(text):
    return re.sub(r'\*\*(.*?)\*\*', lambda m: to_unicode_bold(m.group(1)), text)

def add_log(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    agent_state["logs"].append(entry)
    if len(agent_state["logs"]) > 100:
        agent_state["logs"] = agent_state["logs"][-100:]
    print(f"[{entry['time']}] [{level.upper()}] {msg}")

def get_next_run_time():
    now = datetime.now()
    target = now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target

# ─── CORE FUNCTIONS ──────────────────────────────────────────────────────────
def generate_post(subject):
    clean = subject.replace("(create image)", "").strip()
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_POST},
                {"role": "user", "content": f"Write a post about: {clean}"}
            ]}, timeout=30
        ).json()
        if "error" in response:
            add_log(f"Groq Error: {response['error']['message']}", "error")
            return None
        return format_linkedin_bold(response["choices"][0]["message"]["content"])
    except Exception as e:
        add_log(f"Groq failure: {e}", "error")
        return None

def get_image_url(subject):
    clean = subject.replace("(create image)", "").strip()
    try:
        response = requests.get("https://serpapi.com/search.json", params={
            "engine": "google_images", "q": f"{clean} infographic", "api_key": SERPAPI_API_KEY
        }, timeout=20).json()
        if "images_results" in response and response["images_results"]:
            return response["images_results"][0]["original"]
    except Exception as e:
        add_log(f"SerpAPI Error: {e}", "error")
    return None

def send_to_buffer(post_text, image_url=None):
    input_data = {
        "text": post_text,
        "channelId": BUFFER_CHANNEL_ID,
        "schedulingType": "automatic",
        "mode": "addToQueue"
    }
    if image_url:
        input_data["assets"] = [{"image": {"url": image_url}}]
    try:
        response = requests.post(
            "https://api.buffer.com/1/graphql",
            headers={"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"},
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
def run_batch(triggered_by="scheduler"):
    if agent_state["running"]:
        add_log("Batch already running.", "warn")
        return
    agent_state["running"]     = True
    agent_state["status"]      = "running"
    agent_state["today_count"] = 0
    add_log(f"Batch started (trigger: {triggered_by})", "info")

    if not os.path.exists(SUBJECTS_FILE):
        add_log("Queue is empty! Add subjects from dashboard.", "warn")
        agent_state["running"] = False
        agent_state["status"]  = "waiting"
        return

    with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
        all_subjects = [l.strip() for l in f if l.strip()]

    if not all_subjects:
        add_log("Queue is empty! Add subjects from dashboard.", "warn")
        agent_state["running"] = False
        agent_state["status"]  = "waiting"
        return

    batch = all_subjects[:BATCH_SIZE]
    with open(SUBJECTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_subjects[BATCH_SIZE:]))

    add_log(f"Processing {len(batch)} subjects. {len(all_subjects)-len(batch)} remaining.", "info")

    for j, subject in enumerate(batch):
        manual_image_url = None
        base_subject = subject

        # Parse manual image if it exists
        if "| IMG:" in subject:
            parts = subject.split("| IMG:")
            base_subject = parts[0].strip()
            manual_image_url = parts[1].strip()

        add_log(f"[{j+1}/{len(batch)}] {base_subject[:50]}", "info")
        
        post_text = generate_post(base_subject)
        if not post_text:
            continue
        
        add_log(f"[{j+1}/{len(batch)}] Post generated ✓", "ok")
        
        image_url = None
        if manual_image_url:
            image_url = manual_image_url
            add_log(f"[{j+1}/{len(batch)}] Attached uploaded image ✓", "ok")
        elif "(create image)" in base_subject.lower():
            image_url = get_image_url(base_subject)
            if image_url:
                add_log(f"[{j+1}/{len(batch)}] Image fetched via SerpAPI ✓", "ok")

        status, result = send_to_buffer(post_text, image_url)
        if status == 200 and "errors" not in result:
            pid = result.get("data",{}).get("createPost",{}).get("post",{}).get("id","?")
            add_log(f"[{j+1}/{len(batch)}] Queued to Buffer ✓ ID: {pid}", "ok")
            agent_state["today_count"] += 1
            agent_state["total_run"]   += 1
        else:
            add_log(f"[{j+1}/{len(batch)}] Buffer failed: {result}", "error")
        time.sleep(10)

    agent_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agent_state["running"]  = False
    agent_state["status"]   = "waiting"
    add_log(f"Batch complete! {agent_state['today_count']} posts sent.", "ok")

# ─── THREADS ─────────────────────────────────────────────────────────────────
def scheduler_loop():
    add_log("Scheduler started — waiting for 08:00 AM.", "info")
    while True:
        now = datetime.now()
        if now.hour == TARGET_HOUR and now.minute == 0 and now.second < 5:
            add_log("08:00 AM — auto batch firing!", "warn")
            threading.Thread(target=run_batch, args=("scheduler",), daemon=True).start()
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
            add_log("Keep-alive ping ✓", "info")
        except Exception as e:
            add_log(f"Ping failed: {e}", "warn")
        time.sleep(600)

# ─── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return DASHBOARD

@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "time": datetime.now().strftime("%H:%M:%S")})

@app.route("/api/status")
def get_status():
    next_run = get_next_run_time()
    subjects = []
    if os.path.exists(SUBJECTS_FILE):
        with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
            subjects = [l.strip() for l in f if l.strip()]
    return jsonify({
        "status":       agent_state["status"],
        "running":      agent_state["running"],
        "today_count":  agent_state["today_count"],
        "total_run":    agent_state["total_run"],
        "last_run":     agent_state["last_run"],
        "next_run_iso": next_run.isoformat(),
        "seconds_left": max(0, int((next_run - datetime.now()).total_seconds())),
        "subjects":     subjects,
        "logs":         agent_state["logs"][-30:]
    })

@app.route("/api/run", methods=["POST"])
def manual_run():
    if agent_state["running"]:
        return jsonify({"ok": False, "message": "Already running"}), 409
    threading.Thread(target=run_batch, args=("manual",), daemon=True).start()
    return jsonify({"ok": True, "message": "Batch triggered!"})

@app.route("/api/subjects", methods=["GET"])
def get_subjects():
    subjects = []
    if os.path.exists(SUBJECTS_FILE):
        with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
            subjects = [l.strip() for l in f if l.strip()]
    return jsonify({"subjects": subjects})

@app.route("/api/subjects", methods=["POST"])

def add_subjects():

    data         = request.get_json()

    new_subjects = data.get("subjects", [])

    image_b64    = data.get("image_base64")

    filename     = data.get("filename", "upload.jpg")



    if not new_subjects:

        return jsonify({"ok": False, "message": "No subjects provided"}), 400



    uploaded_url = None



    # Automatically upload physical file to a free link host

    if image_b64:

        try:

            if "," in image_b64:

                image_b64 = image_b64.split(",")[1]

            

            img_data = base64.b64decode(image_b64)

            add_log(f"Uploading '{filename}' to image host...", "info")

            

            files = {'fileToUpload': (filename, img_data)}

            data_payload = {'reqtype': 'fileupload'}

            res = requests.post("https://catbox.moe/user/api.php", data=data_payload, files=files, timeout=30)

            

            if res.status_code == 200 and "catbox.moe" in res.text:

                uploaded_url = res.text.strip()

                add_log(f"Upload success! Public link: {uploaded_url}", "ok")

            else:

                add_log(f"Upload failed: {res.text}", "error")

        except Exception as e:

            add_log(f"Error processing image file: {e}", "error")



    with open(SUBJECTS_FILE, "a", encoding="utf-8") as f:

        for s in new_subjects:

            line = s.strip()

            if uploaded_url:

                line = f"{line} | IMG: {uploaded_url}"

            f.write(line + "\n")



    add_log(f"Added {len(new_subjects)} subject(s) from dashboard.", "ok")

    return jsonify({"ok": True, "added": len(new_subjects)})

# ─── STARTUP ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    add_log(f"LinkedIn Agent started on port {port}", "info")
    add_log("Scheduler active — auto-run at 08:00 AM daily", "ok")
    add_log("Keep-alive ping active every 10 minutes", "ok")
    app.run(host="0.0.0.0", port=port, debug=False)
