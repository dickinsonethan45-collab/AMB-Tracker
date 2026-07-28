"""
ReTracker — public multi-server Oculus/Meta app version tracker.
Each guild configures its own apps, channels, and pings via slash commands.
"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# DATA_DIR lets you point persistent state at a Railway Volume mount (e.g. "/data").
# Railway's filesystem is ephemeral on every redeploy — without a Volume attached
# and DATA_DIR pointed at its mount path, guilds.json resets and you lose every
# server's tracked apps/pings on each deploy.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
GUILDS_PATH = os.path.join(DATA_DIR, "guilds.json")

# ── config ─────────────────────────────────────────────────────────────────────
def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


# config.json is committed to the repo and holds everything except the bot
# token. Only BOT_TOKEN comes from a Railway Variable (or shell env var
# locally), since a live bot token is the one credential that's an instant
# full account takeover if leaked.
cfg = load_json(CONFIG_PATH, {})

ACCESS_TOKEN: str = cfg.get("ACCESS_TOKEN", "")
BOT_TOKEN: str    = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise SystemExit(
        "No bot token found. Set the BOT_TOKEN environment variable "
        "(Railway Variables tab, or `export BOT_TOKEN=...` locally)."
    )

CHECK_INTERVAL: int = _as_int(cfg.get("CheckInterval"), 60)
OWNER_IDS: set[int] = {int(x) for x in cfg.get("OwnerIDs", [])}

GQL_URL = "https://graph.oculus.com/graphql"
VERSION_DOC_ID = cfg.get("DocID", 6771539532935162)

# How many consecutive polls must report the same new version before it's
# treated as a real update. Meta's CDN edges can serve slightly stale/ahead
# data during a rollout, so a single poll can see version B, the next poll
# see version A again, then B again — announcing on every poll turns one
# real update into several. Requiring N consecutive matching reads filters
# that flapping out while still catching genuine updates within N*CHECK_INTERVAL.
CONFIRMATIONS_REQUIRED: int = _as_int(cfg.get("ConfirmationsRequired"), 2)

# ── guild storage helpers ──────────────────────────────────────────────────────
# guilds.json schema (updated):
# {
#   "<guild_id>": {
#     "channel_id": 123,
#     "ping_role_live": 456,
#     "ping_role_dev": 789,
#     "apps": {
#       "<app_id>": {
#         "name": "Animal Company",
#         "last_live": "1.74.4.2954",
#         "last_dev":  "1.75.0.2969",
#         "last_live_at": "2026-05-27T20:04:00+00:00",   # ISO timestamp of last live update
#         "last_dev_at":  "2026-05-27T23:23:00+00:00",   # ISO timestamp of last dev update
#         "dev_builds_since_live": 3,                     # dev builds dropped since last live release
#         "pending_live": "1.83.5.3252",                  # unconfirmed candidate live version (debounce)
#         "pending_live_count": 1,                        # consecutive polls that saw pending_live
#         "pending_dev": "1.83.5.3260",                   # unconfirmed candidate dev version (debounce)
#         "pending_dev_count": 1                          # consecutive polls that saw pending_dev
#       }
#     }
#   }
# }

def guilds_data() -> dict:
    return load_json(GUILDS_PATH, {})


def save_guilds(data: dict) -> None:
    save_json(GUILDS_PATH, data)


def guild_cfg(guild_id: int) -> dict:
    return guilds_data().get(str(guild_id), {})


def update_guild(guild_id: int, patch: dict) -> None:
    data = guilds_data()
    gid = str(guild_id)
    existing = data.get(gid, {})
    existing.update(patch)
    data[gid] = existing
    save_guilds(data)


# ── time helpers ───────────────────────────────────────────────────────────────
def _fmt_timedelta(dt: timedelta) -> str:
    """Convert a timedelta into a human-readable string like '2d 4h 31m'."""
    total_seconds = int(dt.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h {mins}m"


def _time_since(iso_str: Optional[str]) -> Optional[str]:
    """Return a Discord timestamp string: <t:unix:F> (<t:unix:R>)"""
    if not iso_str:
        return None
    try:
        past = datetime.fromisoformat(iso_str)
        unix = int(past.timestamp())
        return f"<t:{unix}:F> (<t:{unix}:R>)"
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── graphql ────────────────────────────────────────────────────────────────────
class GraphQLClient:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._timestamps: list[float] = []

    async def _throttle(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._timestamps = [t for t in self._timestamps if now - t < 5.0]
        if len(self._timestamps) >= 5:
            delay = 5.0 - (now - self._timestamps[0])
            if delay > 0:
                await asyncio.sleep(delay)
        self._timestamps.append(loop.time())

    async def post(self, payload: dict) -> Optional[dict]:
        await self._throttle()
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with self._session.post(GQL_URL, data=payload) as r:
                r.raise_for_status()
                return await r.json(content_type=None)
        except Exception as e:
            print(f"[GQL] {type(e).__name__}: {e}")
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _version_payload(app_id: str) -> dict:
    return {
        "access_token": ACCESS_TOKEN,
        "variables": json.dumps({"applicationID": app_id}),
        "doc_id": str(VERSION_DOC_ID),
    }


async def fetch_app_meta(gql: GraphQLClient, app_id: str) -> Optional[dict]:
    return await gql.post(_version_payload(app_id))


def _extract_live_version(meta: dict) -> Optional[str]:
    nodes = (meta.get("data", {}).get("node", {})
               .get("liveChannel", {}).get("nodes", []))
    return nodes[0].get("latest_supported_binary", {}).get("version") if nodes else None


def _extract_dev_version(meta: dict) -> Optional[str]:
    nodes = (meta.get("data", {}).get("node", {})
               .get("primary_binaries", {}).get("nodes", []))
    return nodes[0].get("version") if nodes else None


def _extract_image(meta: Any, priorities: list[str]) -> Optional[str]:
    best: tuple[int, str] | None = None

    def walk(obj: Any) -> None:
        nonlocal best
        if isinstance(obj, dict):
            uri = obj.get("uri")
            if isinstance(uri, str) and uri:
                t = obj.get("image_type") or obj.get("imageType") or ""
                score = priorities.index(t) if t in priorities else len(priorities)
                if best is None or score < best[0]:
                    best = (score, uri)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(meta)
    return best[1] if best else None


def get_banner_url(meta: Any) -> Optional[str]:
    return _extract_image(meta, [
        "APP_IMG_HERO", "APP_IMG_COVER_LANDSCAPE", "APP_IMG_COVER_PORTRAIT",
        "APP_IMG_COVER_SQUARE", "APP_IMG_ICON", "APP_IMG_LOGO_TRANSPARENT",
    ])


def get_icon_url(meta: Any) -> Optional[str]:
    return _extract_image(meta, [
        "APP_IMG_ICON", "APP_IMG_COVER_SQUARE", "APP_IMG_LOGO_TRANSPARENT",
    ])


def get_app_name(meta: Any) -> Optional[str]:
    try:
        return meta["data"]["node"]["display_name"]
    except Exception:
        return None


# ── embeds ─────────────────────────────────────────────────────────────────────
def make_embed(
    app_name: str,
    old_version: Optional[str],
    new_version: str,
    is_live: bool,
    image_url: Optional[str] = None,
    time_since_last: Optional[str] = None,
    dev_builds_since_live: Optional[int] = None,
) -> discord.Embed:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if is_live:
        embed = discord.Embed(
            title="Update Detected",
            description="### LIVE Build",
            color=0xFFFFFF,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🟢 Updated Version:", value=f"```{new_version}```", inline=False)
        embed.add_field(name="🔴 Last Logged:",      value=f"```{old_version or 'None'}```", inline=False)

        # Time since last live update
        if time_since_last:
            embed.add_field(name="⏱️ Time of Live Release:", value=time_since_last, inline=True)

        # Dev builds that preceded this live release
        if dev_builds_since_live is not None and dev_builds_since_live > 0:
            embed.add_field(
                name="🔨 Dev Builds Before Release:",
                value=f"`{dev_builds_since_live}` dev build{'s' if dev_builds_since_live != 1 else ''}",
                inline=True,
            )

        if image_url:
            embed.set_image(url=image_url)
    else:
        embed = discord.Embed(
            title="New Developer Build",
            color=0xFFFFFF,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🟢 Updated Version:", value=f"```{new_version}```", inline=False)

        # Time since last dev build
        if time_since_last:
            embed.add_field(name="⏱️ Time of Dev Build:", value=time_since_last, inline=True)

        if image_url:
            embed.set_thumbnail(url=image_url)

    embed.set_author(name="AMB Tracker")
    embed.set_footer(text=f"Checked at {now_str}")
    return embed


# ── bot ────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()


class ReTracker(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.gql = GraphQLClient()
        self._update_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        await self.tree.sync()
        print("Slash commands synced.")
        self._update_task = asyncio.create_task(update_loop(self))

    async def close(self) -> None:
        await self.gql.close()
        await super().close()


bot = ReTracker()


# ── update loop ────────────────────────────────────────────────────────────────
async def update_loop(bot: ReTracker) -> None:
    await bot.wait_until_ready()
    print("Update loop started.")

    while not bot.is_closed():
        data = guilds_data()
        for gid_str, gcfg in data.items():
            channel_id = _as_int(gcfg.get("channel_id", 0))
            if not channel_id:
                continue

            apps: dict = gcfg.get("apps", {})
            changed_apps: dict = {}

            for app_id, app_info in apps.items():
                meta = await fetch_app_meta(bot.gql, app_id)
                if not meta:
                    continue

                app_name  = app_info.get("name") or get_app_name(meta) or app_id
                old_live  = app_info.get("last_live")
                old_dev   = app_info.get("last_dev")
                new_live  = _extract_live_version(meta)
                new_dev   = _extract_dev_version(meta)
                banner    = get_banner_url(meta)
                icon      = get_icon_url(meta)

                # Retrieve stored timestamps, dev counter, and pending (unconfirmed) versions
                last_live_at          = app_info.get("last_live_at")
                last_dev_at           = app_info.get("last_dev_at")
                dev_builds_since_live = _as_int(app_info.get("dev_builds_since_live", 0))

                pending_live       = app_info.get("pending_live")
                pending_live_count = _as_int(app_info.get("pending_live_count", 0))
                pending_dev        = app_info.get("pending_dev")
                pending_dev_count  = _as_int(app_info.get("pending_dev_count", 0))

                live_confirmed_this_tick = False

                print(f"[{app_name}] Live: {new_live or 'N/A'} | Dev: {new_dev or 'N/A'}")

                # ── live version (debounced) ──────────────────────────────
                # Meta's API can flap between the old and new version across
                # consecutive polls while a rollout propagates. Don't announce
                # until the same new version has been seen CONFIRMATIONS_REQUIRED
                # times in a row.
                if new_live and new_live != old_live:
                    if new_live == pending_live:
                        pending_live_count += 1
                    else:
                        pending_live = new_live
                        pending_live_count = 1

                    if pending_live_count >= CONFIRMATIONS_REQUIRED:
                        print(f"[{app_name}] 🟢 NEW LIVE (confirmed): {old_live} → {new_live}")
                        now_live_iso = _now_iso()
                        time_since = _time_since(now_live_iso)
                        ping = gcfg.get("ping_role_live")
                        await _send(
                            bot, channel_id,
                            make_embed(
                                app_name, old_live, new_live, True, banner,
                                time_since_last=time_since,
                                dev_builds_since_live=dev_builds_since_live,
                            ),
                            ping,
                        )
                        app_info["last_live"] = new_live
                        app_info["last_live_at"] = now_live_iso
                        # Reset the dev counter now that live caught up
                        app_info["dev_builds_since_live"] = 0
                        dev_builds_since_live = 0
                        pending_live = None
                        pending_live_count = 0
                        live_confirmed_this_tick = True
                    else:
                        print(f"[{app_name}] 🟡 Live change seen ({new_live}), awaiting confirmation "
                              f"({pending_live_count}/{CONFIRMATIONS_REQUIRED})")
                else:
                    # Reading matches what we already have confirmed — clear any stale flap candidate.
                    pending_live = None
                    pending_live_count = 0

                app_info["pending_live"] = pending_live
                app_info["pending_live_count"] = pending_live_count

                # ── dev version (debounced) ───────────────────────────────
                if new_dev and new_dev != old_dev:
                    if new_dev == pending_dev:
                        pending_dev_count += 1
                    else:
                        pending_dev = new_dev
                        pending_dev_count = 1

                    if pending_dev_count >= CONFIRMATIONS_REQUIRED:
                        print(f"[{app_name}] 🔨 NEW DEV (confirmed): {old_dev} → {new_dev}")
                        now_dev_iso = _now_iso()
                        time_since = _time_since(now_dev_iso)
                        ping = gcfg.get("ping_role_dev")
                        await _send(
                            bot, channel_id,
                            make_embed(
                                app_name, old_dev, new_dev, False, icon,
                                time_since_last=time_since,
                            ),
                            ping,
                        )
                        app_info["last_dev"] = new_dev
                        app_info["last_dev_at"] = now_dev_iso
                        # Increment dev builds counter (only if live wasn't confirmed this tick)
                        if not live_confirmed_this_tick:
                            app_info["dev_builds_since_live"] = dev_builds_since_live + 1
                        pending_dev = None
                        pending_dev_count = 0
                    else:
                        print(f"[{app_name}] 🟡 Dev change seen ({new_dev}), awaiting confirmation "
                              f"({pending_dev_count}/{CONFIRMATIONS_REQUIRED})")
                else:
                    pending_dev = None
                    pending_dev_count = 0

                app_info["pending_dev"] = pending_dev
                app_info["pending_dev_count"] = pending_dev_count

                changed_apps[app_id] = app_info

            if changed_apps:
                data[gid_str]["apps"].update(changed_apps)
                save_guilds(data)

        await asyncio.sleep(CHECK_INTERVAL)


async def _send(
    bot: ReTracker,
    channel_id: int,
    embed: discord.Embed,
    ping_role_id: Optional[int] = None,
) -> None:
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if ping_role_id:
            await ch.send(
                content=f"<@&{int(ping_role_id)}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        else:
            await ch.send(embed=embed)
    except Exception as e:
        print(f"[send] channel={channel_id} {type(e).__name__}: {e}")


# ── slash commands ─────────────────────────────────────────────────────────────

def _require_manage(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    member = interaction.user
    if isinstance(member, discord.Member):
        if member.guild_permissions.manage_guild:
            return True
    return interaction.user.id in OWNER_IDS


# /setchannel
@bot.tree.command(name="setchannel", description="Sets used channel as channel for updates on this server")
@app_commands.describe(channel="The channel to send update messages to")
async def cmd_setchannel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not _require_manage(interaction):
        await interaction.response.send_message("You need **Manage Server** to do that.", ephemeral=True)
        return

    update_guild(interaction.guild_id, {"channel_id": channel.id})
    await interaction.response.send_message(
        f"✅ Update channel set to {channel.mention}.", ephemeral=True
    )


# /set-ping
@bot.tree.command(name="set-ping", description="Sets the ping for an app on update messages")
@app_commands.describe(
    build="Which build type to configure the ping for",
    role="Role to ping (leave empty to clear)",
)
@app_commands.choices(build=[
    app_commands.Choice(name="Live", value="live"),
    app_commands.Choice(name="Dev",  value="dev"),
])
async def cmd_set_ping(
    interaction: discord.Interaction,
    build: app_commands.Choice[str],
    role: Optional[discord.Role] = None,
) -> None:
    if not _require_manage(interaction):
        await interaction.response.send_message("You need **Manage Server** to do that.", ephemeral=True)
        return

    key = "ping_role_live" if build.value == "live" else "ping_role_dev"
    update_guild(interaction.guild_id, {key: role.id if role else None})

    if role:
        await interaction.response.send_message(
            f"✅ {build.name} build pings will mention {role.mention}.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"✅ {build.name} build ping cleared.", ephemeral=True
        )


# /add-or-remove
@bot.tree.command(name="add-or-remove", description="Add or remove an app from this server's tracking list")
@app_commands.describe(app_id="The Oculus/Meta App ID to track or untrack")
async def cmd_add_or_remove(interaction: discord.Interaction, app_id: str) -> None:
    if not _require_manage(interaction):
        await interaction.response.send_message("You need **Manage Server** to do that.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    data = guilds_data()
    gid  = str(interaction.guild_id)
    gcfg = data.setdefault(gid, {})
    apps = gcfg.setdefault("apps", {})

    if app_id in apps:
        removed_name = apps[app_id].get("name", app_id)
        del apps[app_id]
        save_guilds(data)
        await interaction.followup.send(f"🗑️ Removed **{removed_name}** (`{app_id}`) from tracking.", ephemeral=True)
        return

    meta = await fetch_app_meta(bot.gql, app_id)
    if not meta or "data" not in meta:
        await interaction.followup.send(
            f"❌ Couldn't fetch data for App ID `{app_id}`. Make sure the ID is correct.",
            ephemeral=True,
        )
        return

    name     = get_app_name(meta) or app_id
    live_ver = _extract_live_version(meta)
    dev_ver  = _extract_dev_version(meta)

    apps[app_id] = {
        "name": name,
        "last_live": live_ver,
        "last_dev": dev_ver,
        "last_live_at": _now_iso(),
        "last_dev_at": _now_iso(),
        "dev_builds_since_live": 0,
    }
    save_guilds(data)

    await interaction.followup.send(
        f"✅ Now tracking **{name}** (`{app_id}`).\n"
        f"> Live: `{live_ver or 'N/A'}` · Dev: `{dev_ver or 'N/A'}`",
        ephemeral=True,
    )


# /list-apps
@bot.tree.command(name="list-apps", description="List apps on this server's tracking list")
async def cmd_list_apps(interaction: discord.Interaction) -> None:
    gcfg = guild_cfg(interaction.guild_id)
    apps: dict = gcfg.get("apps", {})

    if not apps:
        await interaction.response.send_message("No apps are being tracked yet. Use `/add-or-remove` to add one.", ephemeral=True)
        return

    lines = []
    for app_id, info in apps.items():
        name     = info.get("name", app_id)
        live     = info.get("last_live", "?")
        dev      = info.get("last_dev",  "?")
        live_ago = _time_since(info.get("last_live_at"))
        dev_ago  = _time_since(info.get("last_dev_at"))
        dev_count = _as_int(info.get("dev_builds_since_live", 0))

        live_str = f"`{live}`" + (f" *(last updated {live_ago} ago)*" if live_ago else "")
        dev_str  = f"`{dev}`"  + (f" *(last updated {dev_ago} ago)*"  if dev_ago  else "")
        count_str = f"\n> 🔨 Dev builds since last live: `{dev_count}`" if dev_count > 0 else ""

        lines.append(f"**{name}** `{app_id}`\n> Live: {live_str}\n> Dev: {dev_str}{count_str}")

    embed = discord.Embed(
        title="Tracked Apps",
        description="\n\n".join(lines),
        color=0xFFFFFF,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# /details
@bot.tree.command(name="details", description="Get details of an app")
@app_commands.describe(app_id="App ID to inspect (must already be in tracking list)")
async def cmd_details(interaction: discord.Interaction, app_id: str) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    gcfg = guild_cfg(interaction.guild_id)
    apps = gcfg.get("apps", {})
    info = apps.get(app_id)

    meta = await fetch_app_meta(bot.gql, app_id)
    if not meta:
        await interaction.followup.send("❌ Failed to fetch app metadata.", ephemeral=True)
        return

    name     = get_app_name(meta) or (info.get("name") if info else app_id) or app_id
    live_ver = _extract_live_version(meta)
    dev_ver  = _extract_dev_version(meta)
    icon     = get_icon_url(meta)
    banner   = get_banner_url(meta)

    live_ago  = _time_since(info.get("last_live_at")) if info else None
    dev_ago   = _time_since(info.get("last_dev_at"))  if info else None
    dev_count = _as_int(info.get("dev_builds_since_live", 0)) if info else 0

    embed = discord.Embed(
        title=name,
        description=f"App ID: `{app_id}`",
        color=0xFFFFFF,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Live Version", value=f"`{live_ver or 'N/A'}`", inline=True)
    embed.add_field(name="Dev Version",  value=f"`{dev_ver  or 'N/A'}`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

    if live_ago:
        embed.add_field(name="⏱️ Live Last Updated", value=f"`{live_ago} ago`", inline=True)
    if dev_ago:
        embed.add_field(name="⏱️ Dev Last Updated",  value=f"`{dev_ago} ago`",  inline=True)
    if dev_count > 0:
        embed.add_field(
            name="🔨 Dev Builds Since Live",
            value=f"`{dev_count}` build{'s' if dev_count != 1 else ''} ahead",
            inline=True,
        )

    embed.add_field(
        name="Tracking",
        value="✅ Yes" if app_id in apps else "❌ Not tracked on this server",
        inline=False,
    )
    if banner:
        embed.set_image(url=banner)
    if icon:
        embed.set_thumbnail(url=icon)

    await interaction.followup.send(embed=embed, ephemeral=True)


# /test
@bot.tree.command(name="test", description="Test embed — sends a sample update message to this channel")
async def cmd_test(interaction: discord.Interaction) -> None:
    if not _require_manage(interaction):
        await interaction.response.send_message("You need **Manage Server** to do that.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    gcfg = guild_cfg(interaction.guild_id)
    apps = gcfg.get("apps", {})

    if not apps:
        await interaction.followup.send("No apps tracked yet. Add one with `/add-or-remove`.", ephemeral=True)
        return

    app_id, info = next(iter(apps.items()))
    meta = await fetch_app_meta(bot.gql, app_id)
    name      = info.get("name", app_id)
    live_ver  = _extract_live_version(meta) if meta else info.get("last_live")
    dev_ver   = _extract_dev_version(meta)  if meta else info.get("last_dev")
    banner    = get_banner_url(meta) if meta else None
    icon      = get_icon_url(meta)   if meta else None
    dev_count = _as_int(info.get("dev_builds_since_live", 0))

    ch = interaction.channel
    if live_ver:
        await ch.send(embed=make_embed(
            name, info.get("last_live"), live_ver, True, banner,
            time_since_last=_time_since(info.get("last_live_at")),
            dev_builds_since_live=dev_count,
        ))
    if dev_ver:
        await ch.send(embed=make_embed(
            name, None, dev_ver, False, icon,
            time_since_last=_time_since(info.get("last_dev_at")),
        ))

    await interaction.followup.send("✅ Test embeds sent.", ephemeral=True)


# ── ready ──────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready() -> None:
    data = guilds_data()

    for guild in bot.guilds:
        gcfg  = data.get(str(guild.id), {})
        apps  = gcfg.get("apps", {})
        names = [info.get("name", app_id) for app_id, info in apps.items()]
        print(f"Guild {guild.name} synced: {names}")

    print(f"Logged in as {bot.user} | Slash commands synced")


bot.run(BOT_TOKEN)
