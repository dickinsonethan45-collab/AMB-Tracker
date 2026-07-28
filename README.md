# AMB Tracker (ReTracker)

Discord bot that polls the Meta/Oculus GraphQL API for live/dev build version
changes and posts embeds per-guild.

## Config: env vars vs config.json

The bot reads every setting through a small helper that checks an environment
variable first and falls back to `config.json` if the env var isn't set. That
means the same `main.py` runs unchanged locally and on Railway:

| Env var                  | config.json key      | Required | Default                    |
|---------------------------|-----------------------|----------|------------------------------|
| `BOT_TOKEN`                | `BotToken`            | yes      | —                             |
| `ACCESS_TOKEN`             | `ACCESS_TOKEN`        | no       | `""`                          |
| `DOC_ID`                   | `DocID`               | no       | `6771539532935162`            |
| `OWNER_IDS`                | `OwnerIDs`             | no       | `[]` (comma-separated in env) |
| `CHECK_INTERVAL`           | `CheckInterval`        | no       | `60`                          |
| `CONFIRMATIONS_REQUIRED`   | `ConfirmationsRequired`| no       | `2`                           |
| `DATA_DIR`                 | —                      | no       | repo folder                   |

`config.json` and `.env` are both gitignored — never commit real tokens.

## Local dev

```bash
cp .env.example config.json   # then fill in real values, keep the JSON format
pip install -r requirements.txt
python main.py
```

(`.env.example` shows the values in `KEY=value` form for reference — for
`config.json` itself use the original JSON key names shown in the table
above, e.g. `"BotToken"` not `"BOT_TOKEN"`.)

## Deploying: GitHub → Railway

1. **Push to GitHub.** Confirm `config.json` and `guilds.json` are *not* in
   the repo (check `git status` — `.gitignore` already excludes them).
2. **New Railway project → Deploy from GitHub repo**, pick this repo.
   Railway auto-detects Python via `requirements.txt` and uses the
   `Procfile` (`worker: python main.py`) to start the bot.
3. **Set variables** — in the service's **Variables** tab, add at minimum
   `BOT_TOKEN`, plus `ACCESS_TOKEN`, `OWNER_IDS`, etc. as needed (see table
   above). This is what you saw as "Shared Variable / Raw Editor / New
   Variable" — paste them there, not in a checked-in file.
4. **Attach a Volume for persistence.** Railway's filesystem resets on every
   redeploy. Without a Volume, `guilds.json` (every server's tracked apps,
   channels, and ping roles) is wiped each time you push. To fix:
   - In the service, add a **Volume**, mount it at `/data`.
   - Set the `DATA_DIR` variable to `/data`.
   - The bot will then read/write `/data/guilds.json`, which survives
     redeploys.
5. Deploy. Check the Railway logs for `Logged in as ... | Slash commands
   synced` to confirm it's up.

## Security note

The bot token and Oculus access token that were in your original
`config.json` were shared in plaintext at one point — regenerate the Discord
bot token in the [Discord Developer Portal](https://discord.com/developers/applications)
before/after this migration so the old one stops working.
