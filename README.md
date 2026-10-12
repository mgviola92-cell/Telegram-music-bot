# Telegram VC Music Bot

Standalone Telegram voice-chat music bot. It is NOT part of the existing Game Bot project.

## MVP capabilities

- Assistant user account joins an existing group voice chat through PyTgCalls.
- `/play` as a reply to a Telegram MP3/audio message.
- `/vplay` as a reply to a Telegram MP4/video message (initial 360p experimental mode).
- `/play https://...` and `/vplay https://...` for direct HTTPS media on explicitly allowed hosts only.
- Per-group playback queue, `/queue`, `/skip`, `/pause`, `/resume`, `/stop`.
- `/start`, `/help`, `/ping` and HTTP `/health` for Render/UptimeRobot.
- Automatic next track on stream end (best effort); leave when the queue runs out.

Song-title search and arbitrary YouTube/Spotify link handling are not implemented in this first milestone. The `/vplay` route is not yet verified by a live group-call test.

## Render deployment

1. Create a separate Render Web Service in the Music Bot workspace.
2. Source repository: mgviola92-cell/Telegram-music-bot, branch main.
3. Runtime Python; compute Free for initial testing. Auto Deploy OFF.
4. Build command: `pip install -r requirements.txt`.
5. Start command: `python main.py`.
6. Set the secrets below under Render Environment. Never commit them.

Required Render environment variables:

- `API_ID`: Telegram developer API ID.
- `API_HASH`: Telegram developer API hash.
- `BOT_TOKEN`: token of the NEW Music Bot from BotFather.
- `SESSION_STRING`: exported Pyrogram login of your separate Telegram Assistant account.

Optional variables:

- `MAX_ACTIVE_GROUPS`: initial safety cap, default 2.
- `ALLOWED_STREAM_HOSTS`: comma-separated allowlist of exact trusted HTTPS audio hostnames; empty by default.
- `PORT`: set automatically by Render; the code listens on 0.0.0.0.

### Assistant session

The Assistant Telegram account must be a member of each group where music is requested.
It must have permission to join/speak in the active voice chat.

Create its session string on your own local computer (never inside GitHub):

1. Install Python 3.12 and project dependencies.
2. Run `python generate_session.py` and log into the separate Assistant account.
3. Copy the resulting private string directly into Render `SESSION_STRING`.
4. Delete any local copies you no longer need. Never share the string in chat.

## Health and UptimeRobot

Once deployed, the Web Service will have its own `https://<music-service>.onrender.com/health` URL.
Create a separate UptimeRobot HTTP monitor for this URL.
The existing Game Bot monitor watches only the Game Bot URL and does not automatically cover this new service.
Five-minute monitoring can help keep a free service responsive, but it does not guarantee uninterrupted voice streaming;
maintenance, resource limits, and restarts can still disconnect the assistant.

## Security and operational boundaries

- All code changes and deploys are scoped to this repository and Music Bot Render service.
- Game Bot code, database, UptimeRobot monitor, and Render service must not be changed.
- Render Free may restart and its filesystem is ephemeral; queues and temporary audio are in-memory/local only.
- This MVP limits queued tracks to 10 per group and Telegram audio/video messages to 25 MB; video is experimental and limited to one concurrent stream.
- Video is CPU-intensive; Render Free 0.1 CPU/512 MB cannot guarantee smooth video or multiple concurrent voice chats.
- Downloaded Telegram files are temporary; use only audio you are authorized to share.
- No automatic deployment until the new bot credentials are set and manual testing succeeds.
