"""
bot.py — Discord bot: creators link YouTube/TikTok via their Discord
Connections, then run /posted whenever they upload to grow their tree.
Posting again within 25 hours grows it further; going quiet past 25
hours resets it to level 1. Max level 1000. Posting on both platforms
the same day counts as two separate growth events.

Run: python bot.py
Requires: config.py filled in (via environment variables), and the
packages in requirements.txt
"""

import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
import oauth
import storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tree-bot")

intents = discord.Intents.default()


class TreeBot(commands.Bot):
    async def setup_hook(self):
        await self.tree.sync()
        self.loop.create_task(oauth.run_web_server())
        log.info("OAuth web server starting on %s:%s", config.WEB_SERVER_HOST, config.WEB_SERVER_PORT)


bot = TreeBot(command_prefix="!", intents=intents)


# ---------- Tree visuals ----------
# Tiers are (min_level, name, art, color) — art is plain text rendered in
# a ``` code block so spacing lines up regardless of client font, color
# drives the embed's side bar so it visibly shifts as you climb tiers.

TREE_TIERS = [
    (0, "No Tree Yet", "🌰\n\nPost your first video to plant one!", discord.Color.light_gray()),
    (1, "Seedling", "   🌱\n   ||", discord.Color.from_rgb(144, 238, 144)),
    (10, "Sprout", "   🌿\n  🌿🌿\n   ||", discord.Color.from_rgb(110, 220, 110)),
    (50, "Sapling", "    🌳\n   🌳🌳🌳\n    |||", discord.Color.green()),
    (150, "Young Tree", "     🌳🌳\n    🌳🌳🌳🌳\n   🌳🌳🌳🌳🌳🌳\n      |||", discord.Color.from_rgb(46, 160, 90)),
    (400, "Mature Tree", "      🌲🌲🌲\n     🌲🌲🌲🌲🌲\n    🌲🌲🌲🌲🌲🌲🌲\n       |||||", discord.Color.dark_green()),
    (800, "Ancient Tree", "       🌲🌲🌲🌲🌲\n      🌲🌲🌲🌲🌲🌲🌲\n     🌲🌲🌲🌲🌲🌲🌲🌲🌲\n        |||||||", discord.Color.from_rgb(30, 110, 90)),
    (1000, "MAX — Legendary Tree", "    ✨ 🌟 ✨\n   🌳🌳🌳🌳🌳🌳🌳\n  🌳🌳🌳🌳🌳🌳🌳🌳🌳\n 🌳🌳🌳🌳🌳🌳🌳🌳🌳🌳🌳\n     |||||||\n    ⭐ MAX LEVEL ⭐", discord.Color.gold()),
]

TIER_FLAVOR = {
    "No Tree Yet": "Every legendary tree starts with a single video.",
    "Seedling": "It's alive! Keep the streak going.",
    "Sprout": "Growing fast — don't miss a day.",
    "Sapling": "Putting down real roots now.",
    "Young Tree": "Starting to tower over the seedlings.",
    "Mature Tree": "A tree people notice.",
    "Ancient Tree": "Elite territory. Few make it this far.",
    "MAX — Legendary Tree": "Max level. Legendary status achieved.",
}


def get_tier(level: int):
    """Returns (name, art, color) for the highest tier this level qualifies for."""
    current = TREE_TIERS[0]
    for threshold, name, art, color in TREE_TIERS:
        if level >= threshold:
            current = (threshold, name, art, color)
        else:
            break
    return current[1], current[2], current[3]


def progress_bar(level: int, max_level: int = None, length: int = 14) -> str:
    max_level = max_level or config.MAX_TREE_LEVEL
    filled = round(length * min(level, max_level) / max_level)
    pct = round(100 * min(level, max_level) / max_level)
    return f"{'▰' * filled}{'▱' * (length - filled)}  **{pct}%**"


def next_tier_line(level: int) -> str:
    for threshold, name, _art, _color in TREE_TIERS:
        if level < threshold:
            return f"**{threshold - level}** more to reach **{name}**"
    return "Max tier reached"


def build_tree_embed(user: discord.abc.User, entry: dict) -> discord.Embed:
    level = entry.get("tree_level", 0)
    tier_name, art, color = get_tier(level)

    embed = discord.Embed(
        title=f"🌳 {user.display_name}'s Tree",
        description=f"```\n{art}\n```",
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Tier", value=f"**{tier_name}**", inline=True)
    embed.add_field(name="Level", value=f"**{level}** / {config.MAX_TREE_LEVEL}", inline=True)
    embed.add_field(
        name="Streak",
        value=f"🔥 **{level}** day{'s' if level != 1 else ''}" if level > 0 else "No active streak",
        inline=True,
    )
    embed.add_field(name="Progress", value=progress_bar(level), inline=False)
    embed.add_field(name="Next tier", value=next_tier_line(level), inline=False)

    linked = []
    if entry.get("youtube_username"):
        linked.append(f"▸ YouTube: **{entry['youtube_username']}**")
    if entry.get("tiktok_username"):
        linked.append(f"▸ TikTok: **{entry['tiktok_username']}**")
    embed.add_field(
        name="Linked accounts",
        value="\n".join(linked) if linked else "Nothing linked yet — use `/link`",
        inline=False,
    )
    embed.set_footer(text=TIER_FLAVOR.get(tier_name, ""))
    return embed


async def announce_growth(user: discord.abc.User, platform: str, level: int, url: str = None):
    channel = bot.get_channel(config.ALERT_CHANNEL_ID)
    if channel is None:
        log.warning("ALERT_CHANNEL_ID %s not found/visible to the bot.", config.ALERT_CHANNEL_ID)
        return

    tier_name, art, color = get_tier(level)

    if level == 1:
        desc = f"{user.mention} posted on **{platform}** — a new tree just sprouted! 🌱"
    elif level >= config.MAX_TREE_LEVEL:
        desc = f"{user.mention}'s tree just hit **MAX LEVEL {config.MAX_TREE_LEVEL}** after posting on **{platform}**! ✨"
    else:
        desc = f"{user.mention} posted on **{platform}** — their tree grew to **Level {level}**!"

    embed = discord.Embed(
        title=f"{tier_name}",
        description=f"{desc}\n```\n{art}\n```",
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Progress", value=progress_bar(level), inline=False)
    if url:
        embed.add_field(name="Video", value=url, inline=False)
    embed.set_footer(text=TIER_FLAVOR.get(tier_name, ""))
    await channel.send(embed=embed)


# ---------- Video URL validation ----------
# Makes sure a URL passed to /posted is an actual video (not a channel
# link), belongs to the account the user linked, and hasn't already been
# redeemed by anyone. Used URLs are stored as {platform}:{video_id} in
# data["used_urls"] so it's permanent and global across users.

TIKTOK_VIDEO_RE = re.compile(r"^/@(?P<user>[\w.\-]+)/video/(?P<video_id>\d+)/?$")
YOUTUBE_SHORTS_RE = re.compile(r"^/shorts/(?P<video_id>[\w\-]{6,})/?$")


def clean_handle(name: str) -> str:
    return (name or "").strip().lstrip("@").lower()


def normalize_for_match(name: str) -> str:
    """Looser than clean_handle: strips everything but letters/digits and
    lowercases. Meant to absorb emoji, punctuation, or spacing differences
    between what Discord reports for a linked account and what a platform's
    own page/oEmbed reports for the same account."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def validate_posted_url(platform_value: str, url: str, entry: dict):
    """Returns (dedupe_key, None) on success, or (None, error_message) on failure."""
    if not url or not url.strip():
        return None, "You need to include a link to the video itself."

    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path or ""

    if platform_value == "tiktok":
        if host != "tiktok.com":
            return None, "That doesn't look like a tiktok.com link."

        match = TIKTOK_VIDEO_RE.match(path)
        if not match:
            return None, (
                "That's not a video link — it looks like a channel/profile link "
                "(or a shortened link). Open the video in the TikTok app, hit "
                "Share -> Copy Link, and use that full "
                "`tiktok.com/@username/video/...` URL."
            )

        url_user = clean_handle(match.group("user"))
        linked_user = clean_handle(entry.get("tiktok_username"))
        if url_user != linked_user:
            return None, (
                f"That video is from @{match.group('user')}, but your linked "
                f"TikTok account is @{entry.get('tiktok_username')}. You can only "
                "submit videos from your own linked account."
            )

        return f"tiktok:{match.group('video_id')}", None

    if platform_value == "youtube":
        if host not in ("youtube.com", "youtu.be"):
            return None, "That doesn't look like a youtube.com or youtu.be link."

        video_id = None
        if host == "youtu.be":
            video_id = path.strip("/").split("/")[0] or None
        elif path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        else:
            shorts_match = YOUTUBE_SHORTS_RE.match(path)
            if shorts_match:
                video_id = shorts_match.group("video_id")

        if not video_id:
            return None, (
                "That's not a video link — it looks like a channel link (or "
                "something else). Use the full video URL from the Share button "
                "(`youtube.com/watch?v=...`, `youtu.be/...`, or "
                "`youtube.com/shorts/...`)."
            )

        # NOTE: unlike TikTok, a YouTube video URL never contains the channel
        # name, so we can't confirm this specific video belongs to the linked
        # channel from the URL alone — that needs a YouTube Data API lookup
        # (video -> channelId) which isn't wired up here.
        return f"youtube:{video_id}", None

    return None, "Unknown platform."


def url_already_used(data: dict, dedupe_key: str):
    return data.setdefault("used_urls", {}).get(dedupe_key)


def mark_url_used(data: dict, dedupe_key: str, discord_id: str):
    data.setdefault("used_urls", {})[dedupe_key] = discord_id


async def get_youtube_video_details(video_id: str):
    """
    No API key needed. Two free lookups:
      - YouTube's public oEmbed endpoint gives the channel's display name
        (author_name) — no auth required, ever.
      - The watch page itself embeds the upload timestamp in its page JSON
        (publishDate/uploadDate) — scraped with a plain GET, same as how
        yt-dlp and similar tools get it without the Data API.
    Returns a dict with "channel_name" and "published_at" (epoch seconds,
    may be None if that part couldn't be parsed), or None if the video
    couldn't be found/reached at all.
    """
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            oembed_resp = await session.get(
                "https://www.youtube.com/oembed",
                params={"url": watch_url, "format": "json"},
            )
        except Exception:
            log.exception("YouTube oEmbed request failed for video %s", video_id)
            return None

        if oembed_resp.status != 200:
            log.warning(
                "YouTube oEmbed returned HTTP %s for video %s — deleted/private/bad id.",
                oembed_resp.status, video_id,
            )
            return None

        oembed_payload = await oembed_resp.json()
        channel_name = oembed_payload.get("author_name")

        published_at = None
        try:
            page_resp = await session.get(watch_url)
            html = await page_resp.text()
            match = re.search(r'"publishDate":"([^"]+)"', html) or re.search(
                r'"uploadDate":"([^"]+)"', html
            )
            if match:
                published_at = datetime.fromisoformat(match.group(1)).timestamp()
            else:
                log.warning("No publishDate/uploadDate found on watch page for video %s", video_id)
        except Exception:
            log.exception("Couldn't scrape upload date for video %s", video_id)

    return {"channel_name": channel_name, "published_at": published_at}


# ---------- Per-platform cooldown + streak reset ----------
# Posting on TikTok locks TikTok for 24h but doesn't touch YouTube (and vice
# versa) — same-day posts on both platforms still count as two growth events,
# per the original design. If NEITHER platform gets a post within 25h of the
# last one, the tree resets to level 1.
#
# These timestamps (last_tiktok_post_ts / last_youtube_post_ts) are tracked
# here in main.py, separate from whatever staleness logic already lives in
# storage.grow_tree()/storage.check_and_reset_if_stale() — I don't have
# storage.py, so I can't merge this into it. If that file already tracks its
# own "last posted" timestamp, send it over and I'll fold these into one
# system instead of running two in parallel.

PLATFORM_COOLDOWN_SECONDS = 24 * 60 * 60
STREAK_RESET_SECONDS = 25 * 60 * 60
VIDEO_MAX_AGE_SECONDS = 24 * 60 * 60


def tiktok_video_timestamp(video_id: str):
    """
    TikTok video IDs are Snowflake-style: the top 32 bits are the Unix
    upload timestamp (in seconds). No API call needed. Returns None if the
    ID doesn't parse as an int.
    """
    try:
        return int(video_id) >> 32
    except ValueError:
        return None


def format_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def most_recent_post_ts(entry: dict):
    timestamps = [
        entry.get("last_tiktok_post_ts"),
        entry.get("last_youtube_post_ts"),
    ]
    timestamps = [t for t in timestamps if t is not None]
    return max(timestamps) if timestamps else None


def reset_if_stale(entry: dict) -> bool:
    """If 25h+ passed since the last post on either platform, resets the
    tree to level 1 and clears both cooldowns. Returns True if it reset."""
    last_ts = most_recent_post_ts(entry)
    if last_ts is None:
        return False
    if time.time() - last_ts >= STREAK_RESET_SECONDS:
        entry["tree_level"] = 1
        entry["last_tiktok_post_ts"] = None
        entry["last_youtube_post_ts"] = None
        return True
    return False


# ---------- Slash commands ----------

@bot.tree.command(name="link", description="Link your YouTube/TikTok from your Discord Connections.")
async def link(interaction: discord.Interaction):
    url = oauth.build_authorize_url(
        str(interaction.user.id), str(interaction.application_id), interaction.token
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Authorize on Discord", style=discord.ButtonStyle.link, url=url))

    await interaction.response.send_message(
        "Click below and authorize — you'll get bounced straight back to Discord, and I'll "
        "post what I found right here (only you can see it). Make sure your YouTube/TikTok "
        "are added under Discord **Settings -> Connections** first.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="unlink", description="Unlink a platform from your tree.")
@app_commands.describe(platform="Which platform to unlink")
@app_commands.choices(platform=[
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="TikTok", value="tiktok"),
    app_commands.Choice(name="Both", value="both"),
])
async def unlink(interaction: discord.Interaction, platform: app_commands.Choice[str]):
    data = storage.load()
    entry = storage.get_user(data, str(interaction.user.id))

    if platform.value in ("youtube", "both"):
        entry["youtube_username"] = None
    if platform.value in ("tiktok", "both"):
        entry["tiktok_username"] = None

    storage.save(data)
    await interaction.response.send_message(f"Unlinked {platform.name}.", ephemeral=True)


@bot.tree.command(name="posted", description="Tell the bot you just posted a video, to grow your tree.")
@app_commands.describe(platform="Which platform you posted on", url="Link to the video (required)")
@app_commands.choices(platform=[
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="TikTok", value="tiktok"),
])
async def posted(interaction: discord.Interaction, platform: app_commands.Choice[str], url: str):
    data = storage.load()
    entry = storage.get_user(data, str(interaction.user.id))

    if reset_if_stale(entry):
        storage.save(data)

    linked_field = "youtube_username" if platform.value == "youtube" else "tiktok_username"
    if not entry.get(linked_field):
        await interaction.response.send_message(
            f"You haven't linked {platform.name} yet — run `/link` first.", ephemeral=True
        )
        return

    last_ts = entry.get(f"last_{platform.value}_post_ts")
    if last_ts is not None:
        elapsed = time.time() - last_ts
        if elapsed < PLATFORM_COOLDOWN_SECONDS:
            remaining = format_remaining(PLATFORM_COOLDOWN_SECONDS - elapsed)
            await interaction.response.send_message(
                f"⚠️ You already grew your tree with {platform.name} recently — you can use "
                f"{platform.name} again in **{remaining}**. (The other platform isn't affected.)",
                ephemeral=True,
            )
            return

    dedupe_key, error = validate_posted_url(platform.value, url, entry)
    if error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return

    if platform.value == "youtube":
        video_id = dedupe_key.split(":", 1)[1]
        details = await get_youtube_video_details(video_id)
        if details is None:
            await interaction.response.send_message(
                "⚠️ Couldn't fetch that video from YouTube right now — double check the "
                "link, or try again in a bit.",
                ephemeral=True,
            )
            return
        linked_name = entry.get("youtube_username")
        log.info(
            "YouTube ownership check for video %s: oEmbed channel=%r, linked=%r",
            video_id, details["channel_name"], linked_name,
        )
        if normalize_for_match(details["channel_name"]) != normalize_for_match(linked_name):
            await interaction.response.send_message(
                f"⚠️ That video isn't from your linked YouTube channel "
                f"({linked_name}). You can only submit videos from "
                "your own linked account.",
                ephemeral=True,
            )
            return
        published_at = details["published_at"]
        if published_at is not None and time.time() - published_at > VIDEO_MAX_AGE_SECONDS:
            await interaction.response.send_message(
                "⚠️ That video was posted more than 24 hours ago — only videos from the "
                "last 24 hours count.",
                ephemeral=True,
            )
            return

    else:  # tiktok
        video_id = dedupe_key.split(":", 1)[1]
        video_ts = tiktok_video_timestamp(video_id)
        if video_ts is not None and time.time() - video_ts > VIDEO_MAX_AGE_SECONDS:
            await interaction.response.send_message(
                "⚠️ That video was posted more than 24 hours ago — only videos from the "
                "last 24 hours count.",
                ephemeral=True,
            )
            return

    used_by = url_already_used(data, dedupe_key)
    if used_by is not None:
        await interaction.response.send_message(
            "That video's already been used to grow a tree — each video only counts once.",
            ephemeral=True,
        )
        return

    level = storage.grow_tree(entry)
    entry[f"last_{platform.value}_post_ts"] = time.time()
    mark_url_used(data, dedupe_key, str(interaction.user.id))
    storage.save(data)

    await interaction.response.send_message(
        f"Nice! Your tree grew to **Level {level}** 🌳", ephemeral=True
    )
    await announce_growth(interaction.user, platform.name, level, url)


@bot.tree.command(name="tree", description="Check your tree.")
async def tree_cmd(interaction: discord.Interaction):
    data = storage.load()
    entry = storage.get_user(data, str(interaction.user.id))

    stale = reset_if_stale(entry)
    if storage.check_and_reset_if_stale(entry) or stale:
        storage.save(data)

    embed = build_tree_embed(interaction.user, entry)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(config.BOT_TOKEN)
