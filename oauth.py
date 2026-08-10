"""
oauth.py — small web server that runs Discord's OAuth2 flow so the bot
can read a user's linked YouTube/TikTok from their Discord "Connections"
(User Settings -> Connections), instead of asking them to type a handle.

Discord bots can't see another user's connections just because they're
in a server together — that data is only accessible via OAuth2 with the
"connections" scope, which requires the user to click through Discord's
own authorize screen once. That part can't be skipped. Everything after
it, though, happens back inside Discord: this sends the result as a DM
from the bot rather than showing it on a webpage, and the page itself
just bounces the browser back to Discord.
"""

import secrets
import time

from aiohttp import web, ClientSession

import config
import storage

# state -> {"discord_id": ..., "expires": ...}
_pending_states = {}

STATE_TTL_SECONDS = 600  # 10 minutes to complete the OAuth flow

DISCORD_API = "https://discord.com/api/v10"


def build_authorize_url(discord_id: str) -> str:
    state = secrets.token_urlsafe(24)
    _pending_states[state] = {
        "discord_id": discord_id,
        "expires": time.time() + STATE_TTL_SECONDS,
    }
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={config.CLIENT_ID}"
        f"&redirect_uri={config.REDIRECT_URI}"
        "&response_type=code"
        "&scope=identify%20connections"
        f"&state={state}"
    )


async def _exchange_code(code: str, session: ClientSession) -> str:
    resp = await session.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": config.CLIENT_ID,
            "client_secret": config.CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    payload = await resp.json()
    return payload["access_token"]


async def _fetch_connections(access_token: str, session: ClientSession):
    resp = await session.get(
        f"{DISCORD_API}/users/@me/connections",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return await resp.json()


async def _dm_user(discord_id: str, embed: dict, session: ClientSession):
    """Send a DM from the bot to this user. Returns True on success."""
    bot_headers = {"Authorization": f"Bot {config.BOT_TOKEN}"}

    dm_resp = await session.post(
        f"{DISCORD_API}/users/@me/channels",
        json={"recipient_id": discord_id},
        headers=bot_headers,
    )
    if dm_resp.status >= 400:
        return False
    dm_channel = await dm_resp.json()

    msg_resp = await session.post(
        f"{DISCORD_API}/channels/{dm_channel['id']}/messages",
        json={"embeds": [embed]},
        headers=bot_headers,
    )
    return msg_resp.status < 400


def _bounce_page(dm_sent: bool) -> str:
    """
    Minimal page shown for the ~1 second it takes to redirect back to
    Discord — not a destination page, just a bridge.
    """
    if dm_sent:
        message = "Linked — check your DMs. Taking you back to Discord..."
        redirect_js = '<script>setTimeout(function(){ window.location.href = "https://discord.com/channels/@me"; }, 900);</script>'
    else:
        message = (
            "Linked, but I couldn't DM you the result — you likely have DMs from server "
            "members turned off. Run <code>/tree</code> in the server to see what got linked."
        )
        redirect_js = ""
    return f"""<!DOCTYPE html>
<html><head><title>Linking...</title>
<style>
  body {{ background:#1e1f22; color:#dbdee1; font-family: sans-serif;
          display:flex; align-items:center; justify-content:center;
          height:100vh; margin:0; text-align:center; }}
  .box {{ background:#2b2d31; padding:28px 36px; border-radius:12px; max-width:380px; }}
</style></head>
<body><div class="box"><p>{message}</p></div>
{redirect_js}
</body></html>"""


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Link failed</title>
<style>
  body {{ background:#1e1f22; color:#dbdee1; font-family: sans-serif;
          display:flex; align-items:center; justify-content:center;
          height:100vh; margin:0; text-align:center; }}
  .box {{ background:#2b2d31; padding:28px 36px; border-radius:12px; max-width:420px; }}
</style></head>
<body><div class="box"><p>{message}</p></div></body></html>"""


async def handle_callback(request: web.Request):
    code = request.query.get("code")
    state = request.query.get("state")

    if not code or not state or state not in _pending_states:
        return web.Response(
            text=_error_page("That link expired or is invalid — run /link again in Discord."),
            content_type="text/html",
            status=400,
        )

    pending = _pending_states.pop(state)
    if pending["expires"] < time.time():
        return web.Response(
            text=_error_page("That link expired — run /link again in Discord."),
            content_type="text/html",
            status=400,
        )

    discord_id = pending["discord_id"]

    async with ClientSession() as session:
        try:
            access_token = await _exchange_code(code, session)
            connections = await _fetch_connections(access_token, session)
        except Exception:
            return web.Response(
                text=_error_page("Something went wrong talking to Discord — try /link again."),
                content_type="text/html",
                status=500,
            )

        youtube_name = None
        tiktok_name = None
        for conn in connections:
            if conn.get("type") == "youtube":
                youtube_name = conn.get("name")
            elif conn.get("type") == "tiktok":
                tiktok_name = conn.get("name")

        data = storage.load()
        entry = storage.get_user(data, discord_id)
        if youtube_name:
            entry["youtube_username"] = youtube_name
        if tiktok_name:
            entry["tiktok_username"] = tiktok_name
        storage.save(data)

        if not youtube_name and not tiktok_name:
            embed = {
                "title": "No YouTube/TikTok found",
                "description": (
                    "You authorized, but I didn't see a YouTube or TikTok connection on your "
                    "Discord account. Add one under Discord Settings -> Connections, then run "
                    "`/link` again."
                ),
                "color": 0xED4245,
            }
        else:
            lines = []
            lines.append(f"• YouTube: {youtube_name}" if youtube_name else "• YouTube: Not connected")
            lines.append(f"• TikTok: {tiktok_name}" if tiktok_name else "• TikTok: Not connected")
            embed = {
                "title": "Found connected accounts",
                "description": "\n".join(lines),
                "color": 0x57F287,
            }

        dm_sent = await _dm_user(discord_id, embed, session)

    return web.Response(text=_bounce_page(dm_sent), content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/discord-oauth-callback", handle_callback)
    return app


async def run_web_server():
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_SERVER_HOST, config.WEB_SERVER_PORT)
    await site.start()
