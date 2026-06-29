# Deploying to Koyeb (free tier)

This bot runs the Telegram client **and** an HTTP server in one process, so it
fits Koyeb's "Web Service" model (Koyeb needs something listening on `$PORT`).
The included `Dockerfile` installs **ffmpeg**, which Koyeb's buildpacks don't
provide.

## 1. Create a private log channel (recommended)

1. In Telegram, create a **private channel**.
2. Add your bot as an **admin** (needs "Post messages").
3. Get the channel id (forward a message to [@username_to_id_bot] or use a
   "get chat id" bot). It looks like `-1001234567890`.
4. You'll set this as `LOG_CHANNEL_ID`.

Incoming files are copied here, so download links keep working even if the user
deletes their message. Storage is free (Telegram holds the files).

## 2. Push the code to GitHub

Commit everything **except** `.env` and `*.session` (already in `.gitignore`).

```bash
git init
git add .
git commit -m "File-to-link bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## 3. Create the Koyeb service

1. Go to https://app.koyeb.com → **Create Web Service** → **GitHub** → pick your repo.
2. Builder: **Dockerfile** (Koyeb auto-detects it).
3. Instance: **Free**.
4. **Exposed port:** `8080` (Koyeb routes HTTPS on your public domain to it).
   Koyeb also injects `$PORT`; the app reads it automatically.
5. Add the **environment variables** below.
6. Deploy. Koyeb gives you a public URL like
   `https://your-app-org.koyeb.app`.

## 4. Environment variables (Koyeb dashboard)

| Key | Value |
| --- | --- |
| `API_ID` | from my.telegram.org |
| `API_HASH` | from my.telegram.org |
| `BOT_TOKEN` | from @BotFather |
| `LOG_CHANNEL_ID` | `-100...` (your private channel) |
| `SECRET_KEY` | long random hex (`python -c "import secrets; print(secrets.token_hex(32))"`) |
| `BASE_URL` | your Koyeb URL, e.g. `https://your-app-org.koyeb.app` |
| `EXPIRY_SECONDS` | `86400` |

Optional (merged-file storage on R2):

| Key | Value |
| --- | --- |
| `R2_ENABLED` | `true` |
| `R2_ACCOUNT_ID` | Cloudflare account id |
| `R2_ACCESS_KEY_ID` | R2 API token key |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET` | bucket name |

> **Important:** after the first deploy, copy the Koyeb URL into `BASE_URL` and
> redeploy, so generated links point to the public domain (not 127.0.0.1).

## 5. Test

Open your bot in Telegram, send `/start`, then send a file. You should get an
`https://your-app-org.koyeb.app/stream/...` link that downloads in any browser.

## Koyeb free-tier caveats

- **Bandwidth:** the free tier has limited monthly egress. Streaming large files
  to many users can exceed it — watch your usage. (Merged files on R2 don't count
  against Koyeb bandwidth.)
- **Disk is ephemeral and small:** fine for single-file streaming (no disk used).
  Big `/merge` jobs need temp disk for all episodes + output — large merges may
  not fit on the free instance. Use R2 and modest batch sizes.
- **Sleeping:** if the free instance sleeps on inactivity, the bot stops polling.
  Keep it warm by pinging your `BASE_URL` (e.g. a free uptime monitor hitting
  `https://your-app-org.koyeb.app/` every few minutes).
- **Sessions:** the `.session` file is ephemeral; the bot just re-logs in with the
  bot token on each deploy. That's fine.
