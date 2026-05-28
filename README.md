
<div align="center">

```
██╗     ██╗███╗   ██╗██╗  ██╗███████╗██████╗ ██╗███╗   ██╗
██║     ██║████╗  ██║██║ ██╔╝██╔════╝██╔══██╗██║████╗  ██║
██║     ██║██╔██╗ ██║█████╔╝ █████╗  ██║  ██║██║██╔██╗ ██║
██║     ██║██║╚██╗██║██╔═██╗ ██╔══╝  ██║  ██║██║██║╚██╗██║
███████╗██║██║ ╚████║██║  ██╗███████╗██████╔╝██║██║ ╚████║
╚══════╝╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚══════╝╚═════╝ ╚═╝╚═╝  ╚═══╝
                                                             
          █████╗  ██████╗ ███████╗███╗   ██╗████████╗       
         ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝       
         ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║          
         ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║          
         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║          
         ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝          
```

**AI-powered LinkedIn post scheduler — set it, forget it, grow it.**

[![Python](https://img.shields.io/badge/Python-3.10+-7c6aff?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-ff6b9d?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![Groq](https://img.shields.io/badge/Groq-LLaMA_3.1-00d4aa?style=for-the-badge&logoColor=white)](https://groq.com)
[![Buffer](https://img.shields.io/badge/Buffer-API_v1-4fc3f7?style=for-the-badge&logoColor=white)](https://buffer.com)
[![Railway](https://img.shields.io/badge/Deploy-Railway-ffb347?style=for-the-badge&logo=railway&logoColor=white)](https://railway.app)

</div>

---

## ✨ What It Does

LinkedIn Agent automatically writes and schedules professional LinkedIn posts using AI. Give it a list of topics — it handles everything else.

```
You  →  "AI tools for marketers"
         "Remote leadership tips"
         "Personal branding in 2025"

Agent  →  🤖 Generates post with Groq LLaMA 3.1
       →  🖼️  Fetches relevant image (SerpAPI)
       →  📤 Queues to Buffer at 08:00 your local time
       →  ✅ Done. Every. Single. Day.
```

---

## 🗂️ File Structure

```
linkedin-agent/
│
├── 🐍  app.py              ← Flask backend — all API routes & scheduler
│
├── 🔐  login.html          ← Sign-in page
├── 📝  register.html       ← Account creation with password strength meter
├── 🔑  api.html            ← API keys & Buffer channel configuration
└── 📊  dashboard.html      ← Live dashboard, queue, logs & manual trigger
```

---

## 🔄 User Flow

```
  ┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
  │  /login     │────▶│  /register       │────▶│  /api-setup     │
  │             │     │                  │     │                 │
  │ • Username  │     │ • Username       │     │ • Groq key      │
  │ • Password  │     │ • Password       │     │ • Buffer key    │
  │             │     │ • Strength meter │     │ • Channel IDs   │
  │             │     │ • TZ detection   │     │ • SerpAPI key   │
  └─────────────┘     └──────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
                                               ┌─────────────────┐
                                               │  /dashboard     │
                                               │                 │
                                               │ • Countdown     │
                                               │ • Subject queue │
                                               │ • Channel mgmt  │
                                               │ • Activity log  │
                                               │ • Manual run    │
                                               └─────────────────┘
```

---

## ⚡ Quick Start

### 1 — Clone & install

```bash
git clone https://github.com/yourname/linkedin-agent.git
cd linkedin-agent
pip install flask flask-cors requests
```

### 2 — Run locally

```bash
python app.py
# → http://localhost:5000
```

### 3 — Get your API keys

| Service | Where to get it | Required? |
|---------|----------------|-----------|
| **Groq** | [console.groq.com](https://console.groq.com) → API Keys | ✅ Yes |
| **Buffer** | [buffer.com/developers](https://buffer.com/developers) → Access Token | ✅ Yes |
| **SerpAPI** | [serpapi.com](https://serpapi.com) → Dashboard | ⚡ Optional (images) |

---

## 🔑 API Routes

### Page routes

| Route | Page |
|-------|------|
| `GET /` | Redirects to `/login` |
| `GET /login` | Sign-in page |
| `GET /register` | Registration page |
| `GET /api-setup` | API configuration page |
| `GET /dashboard` | Main dashboard |

### API endpoints

```
Auth
  POST  /api/login          Sign in
  POST  /api/register       Create account
  POST  /api/logout         Sign out
  GET   /api/me             Session check

Config
  GET   /api/config         Get (masked) API keys
  POST  /api/config         Save API keys & channels

Channels
  POST  /api/channel/toggle Toggle a channel ON/OFF
  POST  /api/channel/delete Delete channel slot (2–4)

Scheduler
  GET   /api/status         Full dashboard state
  POST  /api/run            Trigger batch manually

Subjects
  GET   /api/subjects       List queued subjects
  POST  /api/subjects       Add subjects (3 modes)
  POST  /api/subjects/delete  Remove one subject
```

---

## 🖼️ Post Modes

```
┌─────────────────────────────────────────────────────────┐
│                     ADD SUBJECTS                        │
│                                                         │
│   ┌──────────┐   ┌──────────────┐   ┌───────────────┐  │
│   │ No Image │   │  Auto Image  │   │ Upload Image  │  │
│   │          │   │              │   │               │  │
│   │  Text    │   │  SerpAPI     │   │  Your own     │  │
│   │  only    │   │  fetches a   │   │  image file   │  │
│   │          │   │  relevant    │   │  uploaded to  │  │
│   │          │   │  photo       │   │  freeimage    │  │
│   └──────────┘   └──────────────┘   └───────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## ⏰ How the Scheduler Works

```
  Every user has their own 08:00 AM fire time
  based on their local timezone, auto-detected at login.

  UTC+0  →  fires at 08:00 UTC
  UTC+5  →  fires at 03:00 UTC (= 08:00 local)
  UTC-5  →  fires at 13:00 UTC (= 08:00 local)
  UTC+5:30 →  fires at 02:30 UTC (= 08:00 IST)

  ┌──────────────────────────────────────────┐
  │  scheduler_loop() runs every 1 second    │
  │  checks every user's local time          │
  │  fires run_batch() at exactly 08:00:00   │
  │  prevents double-fire with fired_today   │
  └──────────────────────────────────────────┘

  Batch size: 2 posts per day
  Posts are dequeued from the top of subjects.txt
```

---

## 📡 Buffer Channel Setup

Up to **4 channels** supported simultaneously. All active channels receive the same post.

```
Channel 1  ─────────────────── LinkedIn Personal   (required)
Channel 2  ─────────────────── LinkedIn Company    (optional)
Channel 3  ─────────────────── LinkedIn Agency     (optional)
Channel 4  ─────────────────── Facebook Page       (optional, auto-detects FB type)
```

To find your Channel ID:
1. Go to [buffer.com](https://buffer.com) → Connect a channel
2. Open browser dev tools → Network tab
3. Look for any API call — the channel ID appears as `670d...`

---

## 🚀 Deploy to Railway

```bash
# 1. Push your code to GitHub

# 2. Go to railway.app → New Project → Deploy from GitHub

# 3. Set environment variable (optional):
PORT=5000

# 4. Railway auto-detects Python and runs:
#    python app.py

# 5. Your app is live at:
#    https://your-app.up.railway.app
```

> **Persistent data on Railway:** Add a volume mounted at `/data` so `users.json` and subject queues survive redeploys.

```
Settings → Volumes → Mount Path: /data
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        BROWSER                                  │
│  login.html  register.html  api.html  dashboard.html            │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP / fetch()
┌────────────────────────▼────────────────────────────────────────┐
│                      app.py (Flask)                             │
│                                                                 │
│  Page Routes          API Routes          Background Threads    │
│  /login        ──►   /api/login           scheduler_loop()      │
│  /register     ──►   /api/register        keep_alive_loop()     │
│  /api-setup    ──►   /api/config                                │
│  /dashboard    ──►   /api/status                                │
│                ──►   /api/run                                   │
└──────────────┬────────────────────────┬────────────────────────┘
               │                        │
    ┌──────────▼──────┐      ┌──────────▼──────────┐
    │   /data/         │      │   External APIs      │
    │   users.json     │      │                      │
    │   user_data/     │      │  groq.com  (LLM)     │
    │     alice/       │      │  buffer.com (post)   │
    │       config.json│      │  serpapi.com (image) │
    │       subjects.txt│      │  freeimage.host      │
    │       state.json │      └─────────────────────┘
    └─────────────────┘
```

---

## 🔒 Security

- Passwords hashed with **SHA-256 + random salt** (per user)
- API keys masked in GET responses (only last 4 chars visible)
- Flask **session cookies** for auth (server-side, 24h expiry)
- Each user's data is **fully isolated** under `user_data/<username>/`
- No admin backdoor — accounts are self-service only

---

## 🤖 Post Generation Prompt

```
You are an expert LinkedIn ghostwriter.
Write a highly engaging, professional LinkedIn post based on the user's subject.
  1. Hook on the first line wrapped in **asterisks** (rendered as Unicode bold).
  2. Short, punchy sentences.
  3. Call-To-Action (CTA) question at the end.
  4. 3 to 5 relevant hashtags.
```

Model: `llama-3.1-8b-instant` via Groq (fast, free tier available)

---

## 🐛 Troubleshooting

| Problem | Fix |
|---------|-----|
| `Not Found` on Railway | Make sure all 5 files (`app.py` + 4 HTML) are in the repo root |
| `dashboard.html not found` | Old `app.py` — replace with the new version that has page routes |
| Posts not sending | Check Buffer API key and that at least 1 channel is toggled ON |
| Images not attaching | Add your SerpAPI key in `/api-setup` |
| Scheduler not firing | Timezone offset is stored on login — log out and back in to refresh |
| Railway data lost on redeploy | Add a `/data` volume in Railway settings |

---

## 📄 License

MIT — free to use, modify, and deploy.

---

<div align="center">

**Built with** 🐍 Flask · 🤖 Groq LLaMA · 📤 Buffer · 🖼️ SerpAPI

*Write less. Post more. Grow consistently.*

</div>
