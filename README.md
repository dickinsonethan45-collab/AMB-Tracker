# AMB Tracker (ReTracker)

Discord bot that polls the Meta/Oculus GraphQL API for live/dev build version
changes and posts embeds per-guild.

## Config

Everything lives in `config.json`, which is committed to this repo, **except
the bot token**:

| config.json key         | Meaning                                            | Default                |
|--------------------------|-----------------------------------------------------|-------------------------|
| `ACCESS_TOKEN`            | Oculus/Meta GraphQL access token                     | `""`                     |
| `DocID`                   | GraphQL persisted query doc ID                       | `6771539532935162`       |
| `OwnerIDs`                | Discord user IDs treated as bot owners                | `[]`                      |
| `CheckInterval`           | Seconds between polls                                 | `60`                      |
| `ConfirmationsRequired`   | Consecutive matching polls needed before announcing   | `2`                       |

The bot token is the one thing that's **not** in `config.json` — it's read
from the `BOT_TOKEN` environment variable only, since a leaked bot token is
an instant full takeover of the bot account (whereas the repo above is
assumed private/trusted enough for the rest).

## Local dev

```bash
pip install -r requirements.txt
export BOT_TOKEN=your-discord-bot-token
python main.py
```

## Deploying: GitHub → Railway

1. **Push to GitHub** with `config.json` included as-is.
2. **New Railway project → Deploy from GitHub repo**, pick this repo.
   Railway auto-detects Python via `requirements.txt` and uses the
   `Procfile` (`worker: python main.py`) to start the bot.
3. **Set one variable** — in the service's **Variables** tab, add
   `BOT_TOKEN` with your bot's token. Nothing else needs to go there.
4. **Attach a Volume for persistence.** Railway's filesystem resets on every
   redeploy. Without a Volume, `guilds.json` (every server's tracked apps,
   channels, and ping roles) is wiped each time you push. To fix:
   - In the service, add a **Volume**, mount it at `/data`.
   - Set a `DATA_DIR` variable to `/data`.
   - The bot will then read/write `/data/guilds.json`, which survives
     redeploys.
5. Deploy. Check the Railway logs for `Logged in as ... | Slash commands
   synced` to confirm it's up.

## Security note

Make sure this repo is **private** if you're keeping `ACCESS_TOKEN` committed
— it's a real credential for Meta's GraphQL API, just not one that can touch
the Discord bot account directly. The Discord bot token that was in your
original config was shared in plaintext at one point; regenerate it in the
[Discord Developer Portal](https://discord.com/developers/applications) so
the old one stops working.
