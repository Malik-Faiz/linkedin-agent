# LinkedIn AI Agent — Setup Guide

## Files
- app.py          → Flask backend (the AI agent)
- dashboard.html  → Beautiful frontend dashboard
- requirements.txt
- subjects.txt    → Auto-created on first run

## Setup (one time)
pip install -r requirements.txt

## Run the Agent
python app.py

Then open dashboard.html in your browser.

## How it works
- app.py runs 24/7, checks every second if it's 08:00 AM
- At 8 AM it auto-processes the first 2 subjects from subjects.txt
- Generates posts via Groq → fetches images via SerpAPI → queues to Buffer
- Dashboard polls Flask every 2 seconds for live updates

## API Keys (edit in app.py)
GROQ_API_KEY      = "your_key"
BUFFER_API_KEY    = "your_key"
BUFFER_CHANNEL_ID = "your_channel_id"
SERPAPI_API_KEY   = "your_key"
