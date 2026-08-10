# Creator Tree Bot

Discord bot: creators link their YouTube/TikTok straight from their Discord
**Connections** (Settings -> Connections), then run `/posted` whenever they
upload to grow their tree. Post again within 25 hours and it grows further;
go quiet past 25 hours and it drops back to level 1. Max level 1000.
Posting on both platforms the same day (two `/posted` calls) counts as two
separate growth events.

No YouTube or TikTok API keys needed — linking works entirely through
Discord's own OAuth2 "connections" scope, and growth is triggered manually
since Discord doesn't expose "this person just posted a video" data.

## 1. Discord application setup

At https://discord.com/developers/applications, create an application:

- **Bot** tab -> Reset Token -> copy it (you'll paste it into Railway as `BOT_TOKEN`)
- **OAuth2 -> General** -> copy Client ID and Client Secret (-> `CLIENT_ID` / `CLIENT_SECRET`)
- **OAuth2 -> URL Generator** -> scopes: `bot`, `applications.commands` ->
  permissions: at least "Send Messages", "Embed Links" -> use the generated
  URL to invite the bot to your server
- Leave **OAuth2 -> Redirects** open for now — you'll add the exact URL
  once you know your Railway domain (step 3).

## 2. Push this to GitHub

Commit everything except `.env` and `data.json` (already handled by
`.gitignore`) to a GitHub repo — that's what Railway will deploy from.

## 3. Deploy on Railway

1. Railway -> New Project -> **Deploy from GitHub repo** -> pick this repo.
   Railway will detect `requirements.txt` and the `Procfile` automatically.
2. Go to the service's **Settings -> Networking** and click **Generate
   Domain**. You'll get something like `https://your-app.up.railway.app`.
3. Go back to the Discord Developer Portal -> **OAuth2 -> Redirects** and
   add: `https://your-app.up.railway.app/discord-oauth-callback`
4. In Railway, go to the service's **Variables** tab and add:
   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | your bot token |
   | `CLIENT_ID` | your client ID |
   | `CLIENT_SECRET` | your client secret |
   | `REDIRECT_URI` | `https://your-app.up.railway.app/discord-oauth-callback` |
   | `ALERT_CHANNEL_ID` | the channel ID for alerts |

   Don't set `PORT` — Railway injects that automatically, and `config.py`
   already reads it.
5. Railway will redeploy with the new variables and the bot will come online.

### Persisting data.json

Railway's filesystem is ephemeral by default — anything written to disk
(like `data.json`, which stores linked accounts and tree levels) gets wiped
on every redeploy unless it's on a mounted volume. To keep it:

- Service -> **Settings -> Volumes** -> add a volume, mount it at e.g. `/data`
- Add a variable `DATA_FILE=/data/data.json`

Without this, tree progress resets whenever you push a new deploy.

## Local testing (optional)

```
cp .env.example .env
# fill in .env with real values
pip install -r requirements.txt
python bot.py
```
For the OAuth redirect to work locally you need a public HTTPS URL pointing
at your machine — `ngrok http 8080` works well; use the URL it gives you
for both `REDIRECT_URI` in `.env` and in the Discord Developer Portal.

## Commands

- `/link` — sends a button to authorize with Discord; the bot then reads
  your YouTube/TikTok directly from your Discord Connections and saves
  them. You need those connections added under Discord's own
  **Settings -> Connections** first.
- `/unlink platform:<YouTube|TikTok|Both>` — unlink
- `/posted platform:<YouTube|TikTok> [url]` — tell the bot you just
  uploaded, to grow your tree (also posts the alert to your alert channel)
- `/tree [user]` — check a tree's current level (defaults to yourself)

## Notes

- Discord bots can't read another user's Connections just from server
  membership — that data is only available via OAuth2 with the
  `connections` scope, which is why `/link` sends people through an
  authorize screen rather than pulling it automatically.
- Growth is manual (`/posted`) since Discord Connections only tell you an
  account is linked, not when someone uploads something new.
- The "stale reset" (dropping a quiet tree back to level 1) is checked
  whenever `/tree` is run, so it shows correctly even if nobody's used
  the bot in a while.
