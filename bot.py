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
from urllib.parse import urlparse, parse_qs

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
# Tiers are (min_level, name, art) — art is plain text, rendered inside a
# ``` code block so spacing lines up regardless of client font.

TREE_TIERS = [
    (0, "No Tree Yet", "🌰\n\nPost your first video to plant one!"),
    (1, "Seedling", "  🌱\n  ||"),
    (10, "Sprout", "   🌿\n  🌿🌿\n   ||"),
    (50, "Sapling", "    🌳\n   🌳🌳🌳\n    |||"),
    (150, "Young Tree", "     🌳🌳\n    🌳🌳🌳🌳\n   🌳🌳🌳🌳🌳🌳\n      |||"),
    (400, "Mature Tree", "      🌲🌲🌲\n     🌲🌲🌲🌲🌲\n    🌲🌲🌲🌲🌲🌲🌲\n       |||||"),
    (800, "Ancient Tree", "       🌲🌲🌲🌲🌲\n      🌲🌲🌲🌲🌲🌲🌲\n     🌲🌲🌲🌲🌲🌲🌲🌲🌲\n        |||||||"),
    (1000, "MAX — Legendary Tree", "    ✨ 🌟 ✨\n   🌳🌳🌳🌳🌳🌳🌳\n  🌳🌳🌳🌳🌳🌳🌳🌳🌳\n 🌳🌳🌳🌳🌳🌳🌳🌳🌳🌳🌳\n     |||||||\n    ⭐ MAX LEVEL ⭐"),
]


def get_tier(level: int):
    """Returns (name, art) for the highest tier this level qualifies for."""
    current = TREE_TIERS[0]
    for threshold, name, art in TREE_TIERS:
        if level >= threshold:
            current = (threshold, name, art)
        else:
            break
    return current[1], current[2]


def progress_bar(level: int, max_level: int = None, length: int = 12) -> str:
    max_level = max_level or config.MAX_TREE_LEVEL
    filled = round(length * min(level, max_level) / max_level)
    pct = round(100 * min(level, max_level) / max_level)
    return f"{'▰' * filled}{'▱' * (length - filled)}  {pct}%"


def tier_color(level: int) -> discord.Color:
    if level >= config.MAX_TREE_LEVEL:
        return discord.Color.gold()
    if level >= 400:
        return discord.Color.dark_green()
    if level >= 50:
        return discord.Color.green()
    if level >= 1:
        return discord.Color.from_rgb(144, 238, 144)
    return discord.Color.light_gray()


def build_tree_embed(display_name: str, entry: dict) -> discord.Embed:
    level = entry.get("tree_level", 0)
    tier_name, art = get_tier(level)

    embed = discord.Embed(
        title=f"{display_name}'s Tree — {tier_name}",
        description=f"```\n{art}\n```",
        color=tier_color(level),
    )
    embed.add_field(name="Level", value=f"**{level} / {config.MAX_TREE_LEVEL}**", inline=True)
    embed.add_field(
        name="Streak",
        value=f"🔥 {level} day{'s' if level != 1 else ''}" if level > 0 else "No active streak",
        inline=True,
    )
    embed.add_field(name="Progress", value=progress_bar(level), inline=False)

    linked = []
    if entry.get("youtube_username"):
        linked.append(f"YouTube: {entry['youtube_username']}")
    if entry.get("tiktok_username"):
        linked.append(f"TikTok: {entry['tiktok_username']}")
    embed.add_field(
        name="Linked accounts",
        value="\n".join(linked) if linked else "Nothing linked yet — use `/link`",
        inline=False,
    )
    return embed


async def announce_growth(user: discord.abc.User, platform: str, level: int, url: str = None):
    channel = bot.get_channel(config.ALERT_CHANNEL_ID)
    if channel is None:
        log.warning("ALERT_CHANNEL_ID %s not found/visible to the bot.", config.ALERT_CHANNEL_ID)
        return

    tier_name, art = get_tier(level)

    if level == 1:
        desc = f"{user.mention} posted on **{platform}** — a new tree just sprouted! 🌱"
    elif level >= config.MAX_TREE_LEVEL:
        desc = f"{user.mention}'s tree just hit **MAX LEVEL {config.MAX_TREE_LEVEL}** after posting on **{platform}**! ✨"
    else:
        desc = f"{user.mention} posted on **{platform}** — their tree grew to **Level {level}**!"

    embed = discord.Embed(title=tier_name, description=f"{desc}\n```\n{art}\n```", color=tier_color(level))
    embed.add_field(name="Progress", value=progress_bar(level), inline=False)
    if url:
        embed.add_field(name="Video", value=url, inline=False)
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

    linked_field = "youtube_username" if platform.value == "youtube" else "tiktok_username"
    if not entry.get(linked_field):
        await interaction.response.send_message(
            f"You haven't linked {platform.name} yet — run `/link` first.", ephemeral=True
        )
        return

    dedupe_key, error = validate_posted_url(platform.value, url, entry)
    if error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return

    used_by = url_already_used(data, dedupe_key)
    if used_by is not None:
        await interaction.response.send_message(
            "That video's already been used to grow a tree — each video only counts once.",
            ephemeral=True,
        )
        return

    level = storage.grow_tree(entry)
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

    if storage.check_and_reset_if_stale(entry):
        storage.save(data)

    embed = build_tree_embed(interaction.user.display_name, entry)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(config.BOT_TOKEN)
