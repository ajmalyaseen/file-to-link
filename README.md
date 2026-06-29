# File-to-Link Telegram Bot (Pyrogram + FastAPI)

Turn Telegram files into direct, browser-friendly download links — and merge
series episodes into a single file with chapter markers. Built on **MTProto**
(Pyrogram), so it handles large files up to ~2 GB each, well past the 20 MB
public Bot API limit.

## What it does

**📁 Single file mode (default)** — instant, no download
```
User sends a video/document  -> bot copies it to a private log channel
                             -> generates an HMAC-signed link INSTANTLY (no wait)
                             -> when clicked, the server streams the file straight
                                from Telegram on demand (no server-side copy)
                             -> link expires after 24h
```
No disk used and no download wait — the file stays on Telegram (free storage)
and is streamed on demand, the same model the popular file-stream bots use.

**🎬 Series merge mode**
```
/newmerge                    -> start a merge session
send episodes one by one     -> each downloaded + confirmed ("Episode N received ✅")
/merge                       -> FFmpeg concat (-c copy) + chapter markers
                             -> uploaded to Cloudflare R2 (zero egress) if enabled,
                                else served from the VM
                             -> one HMAC-signed link (expires 24h)
                             -> temp episodes deleted, file cleaned up after 24h
/cancel                      -> abort session + delete temp files
```

## Commands

| Command | What it does |
| --- | --- |
| `/start` | Welcome + explains both modes |
| `/newmerge` | Start a new series merge session |
| `/merge` | Merge queued episodes and get a download link |
| `/cancel` | Cancel the session and delete temp files |
| _(send a file)_ | Single-file mode: instant download link |

## Tech stack

- **Pyrogram** (via `pyrofork`) — MTProto client; large transfers + on-demand streaming
- **FFmpeg** (`subprocess`) — concat demuxer merge + chapter metadata
- **FastAPI + Uvicorn** — instant streaming proxy + signed downloads
- **Cloudflare R2** (optional) — zero-egress storage for merged files
- **Telegram log channel** — free, permanent storage for single files
- **HMAC-SHA256 signed URLs** — security + self-contained expiry
- **asyncio** — bot and HTTP server run together in one process

## Project structure

```
file to link bot/
├── bot.py          # Pyrogram handlers (single-file streaming + merge mode)
├── merger.py       # FFmpeg merge + ffprobe durations + chapter markers
├── server.py       # FastAPI: /stream (on-demand) + /download (disk fallback)
├── r2.py           # Cloudflare R2 upload + presigned URLs (merged files)
├── utils.py        # HMAC sign/verify, stream tokens, cleanup, formatters
├── config.py       # loads .env, exposes settings + paths
├── Dockerfile      # container build (includes ffmpeg) for Koyeb/any host
├── DEPLOY.md       # Koyeb free-tier deployment guide
├── sessions/       # temp downloaded episodes per user (merge only)
├── output/         # merged output files (when R2 disabled)
├── .env            # secrets + config
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.10+ (3.13 supported via `pyrofork`)
- [FFmpeg + ffprobe](https://ffmpeg.org/download.html) on your PATH
- A bot token from [@BotFather](https://t.me/BotFather)
- `API_ID` / `API_HASH` from [my.telegram.org](https://my.telegram.org)

## Setup

```bash
pip install -r requirements.txt

# Configure
copy .env.example .env        # Windows (use cp on macOS/Linux)
# edit .env: set API_ID, API_HASH, BOT_TOKEN
# (a random SECRET_KEY is already generated in .env)

python bot.py
```

The bot and the download server start together. Talk to your bot in Telegram.

## How the signed links work

A link looks like:
```
http://your-host:8080/download/<filename>?token=<expires>.<hmac>
```
The token embeds the expiry timestamp and an HMAC-SHA256 signature over
`filename:expires` using `SECRET_KEY`. The server recomputes the signature with
a constant-time compare and rejects anything expired or tampered with. No
database needed — the token validates itself.

## Sharing links publicly

`BASE_URL=http://127.0.0.1:8080` only works on your own machine. To let others
download:

- Set `BASE_URL` to your server's public IP/domain, open `HTTP_PORT` in the firewall, **or**
- Put a reverse proxy (Caddy/Nginx) with HTTPS in front and set
  `BASE_URL=https://your-domain`.

## Notes & limits

- **Per-file cap:** Telegram allows ~2 GB per file (4 GB for Premium uploaders).
  The *merged* output can exceed that since it's served over your own HTTP link.
- **Merge needs matching codecs:** `-c copy` (no re-encode) is fast and lossless
  but requires episodes to share the same codecs/resolution. If they differ, the
  merge will fail with a clear message (re-encoding would be needed).
- **Chapters:** each episode becomes "Chapter 1, Chapter 2, ..." at the right
  timestamps, computed from `ffprobe` durations. Use a player like VLC/MKV to see them.
- **Cleanup:** single files and merged outputs auto-delete after `EXPIRY_SECONDS`
  (24h). Temp episodes are removed right after a successful merge or on `/cancel`.
- **Restart caveat:** cleanup is scheduled in-memory, so files created before a
  restart won't be auto-deleted by the timer (their links still expire via HMAC).
