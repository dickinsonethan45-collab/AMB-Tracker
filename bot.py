"""
bot.py — Discord bot: creators link YouTube/TikTok via their Discord
Connections, then run /posted whenever they upload to grow their tree.
Posting again within 25 hours grows it further; going quiet past 25
hours resets it to level 1. Max level 1000. Posting on both platforms
the same day counts as two separate growth events.

Run: python bot.py
Requires: config.py filled in, and the packages in requirements.txt
"""

import logging

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
        # Run the little OAuth callback server alongside the bot.
        self.loop.create_task(oauth.run_web_server())
        log.info("OAuth web server starting on %s:%s", config.WEB_SERVER_HOST, config.WEB_SERVER_PORT)


bot = TreeBot(command_prefix="!", intents=intents)


def tree_emoji(level: int) -> str:
    if level >= config.MAX_TREE_LEVEL:
        return "🌳✨"
    if level >= 500:
        return "🌳"
    if level >= 100:
        return "🌲"
    if level >= 10:
        return "🌱"
    return "🌰"


async def announce_growth(user: discord.abc.User, platform: str, level: int, url: str = None):
    channel = bot.get_channel(config.ALERT_CHANNEL_ID)
    if channel is None:
        log.warning("ALERT_CHANNEL_ID %s not found/visible to the bot.", config.ALERT_CHANNEL_ID)
        return

    if level == 1:
        desc = f"{user.mention} posted on **{platform}** and their tree is starting fresh at **Level 1**! {tree_emoji(1)}"
    elif level >= config.MAX_TREE_LEVEL:
        desc = f"{user.mention}'s tree just hit **MAX LEVEL {config.MAX_TREE_LEVEL}** {tree_emoji(level)} after posting on **{platform}**!"
    else:
        desc = f"{user.mention} posted on **{platform}** — their tree grew to **Level {level}**! {tree_emoji(level)}"

    embed = discord.Embed(description=desc, color=discord.Color.green())
    if url:
        embed.add_field(name="Video", value=url, inline=False)
    await channel.send(embed=embed)


# ---------- Slash commands ----------

@bot.tree.command(name="link", description="Link your YouTube/TikTok from your Discord Connections.")
async def link(interaction: discord.Interaction):
    url = oauth.build_authorize_url(str(interaction.user.id))
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Authorize on Discord", style=discord.ButtonStyle.link, url=url))

    await interaction.response.send_message(
        "Click below and authorize — you'll get bounced straight back to Discord, and I'll "
        "DM you what I found. Make sure your YouTube/TikTok are added under Discord "
        "**Settings -> Connections** first.",
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
@app_commands.describe(platform="Which platform you posted on", url="Optional link to the video")
@app_commands.choices(platform=[
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="TikTok", value="tiktok"),
])
async def posted(interaction: discord.Interaction, platform: app_commands.Choice[str], url: str = None):
    data = storage.load()
    entry = storage.get_user(data, str(interaction.user.id))

    linked_field = "youtube_username" if platform.value == "youtube" else "tiktok_username"
    if not entry.get(linked_field):
        await interaction.response.send_message(
            f"You haven't linked {platform.name} yet — run `/link` first.", ephemeral=True
        )
        return

    level = storage.grow_tree(entry)
    storage.save(data)

    await interaction.response.send_message(
        f"Nice! Your tree grew to **Level {level}** {tree_emoji(level)}", ephemeral=True
    )
    await announce_growth(interaction.user, platform.name, level, url)


@bot.tree.command(name="tree", description="Check your (or someone else's) tree status.")
@app_commands.describe(user="Whose tree to check (defaults to you)")
async def tree_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    data = storage.load()
    entry = storage.get_user(data, str(target.id))

    if storage.check_and_reset_if_stale(entry):
        storage.save(data)

    level = entry.get("tree_level", 0)
    linked = []
    if entry.get("youtube_username"):
        linked.append(f"YouTube ({entry['youtube_username']})")
    if entry.get("tiktok_username"):
        linked.append(f"TikTok ({entry['tiktok_username']})")
    linked_str = ", ".join(linked) if linked else "nothing linked yet — use `/link`"

    embed = discord.Embed(
        title=f"{target.display_name}'s tree {tree_emoji(level)}",
        description=f"**Level {level} / {config.MAX_TREE_LEVEL}**\nLinked: {linked_str}",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(config.BOT_TOKEN)
