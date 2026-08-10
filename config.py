"""
config.py — reads all secrets/settings from environment variables.

Locally: create a `.env` file (see .env.example) and this will load it
automatically via python-dotenv.

On Railway: set these as Variables on the service (Project -> your
service -> Variables). Don't commit real values to GitHub — .env is
gitignored for that reason.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # no-ops if there's no .env file, e.g. on Railway


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it locally in .env or on Railway under Variables."
        )
    return value


# --- Secrets / IDs (required) ---

BOT_TOKEN = _require("BOT_TOKEN")
CLIENT_ID = _require("CLIENT_ID")
CLIENT_SECRET = _require("CLIENT_SECRET")

# Must exactly match a redirect URL you add under
# Developer Portal -> OAuth2 -> Redirects. On Railway this should be your
# service's public domain (Settings -> Networking -> Generate Domain)
# plus "/discord-oauth-callback", e.g.
# https://your-app.up.railway.app/discord-oauth-callback
REDIRECT_URI = _require("REDIRECT_URI")

# Channel where "tree grew to level X" alerts get posted. Enable Discord
# Developer Mode (Settings -> Advanced), right-click the channel ->
# Copy Channel ID.
ALERT_CHANNEL_ID = int(_require("ALERT_CHANNEL_ID"))

# --- Web server ---

WEB_SERVER_HOST = "0.0.0.0"
# Railway injects PORT automatically — don't hardcode this on Railway.
WEB_SERVER_PORT = int(os.getenv("PORT", "8080"))

# --- Tree / growth rules (optional, sensible defaults) ---

GROWTH_WINDOW_HOURS = float(os.getenv("GROWTH_WINDOW_HOURS", "25"))
MAX_TREE_LEVEL = int(os.getenv("MAX_TREE_LEVEL", "1000"))

# --- Storage ---

# NOTE: Railway's filesystem is ephemeral by default — data.json will be
# wiped on redeploy unless this path is on a mounted Railway Volume.
# See README for how to attach one.
DATA_FILE = os.getenv("DATA_FILE", "data.json")
