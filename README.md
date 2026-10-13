# Telegram VC Music Bot

Standalone Telegram voice-chat music bot. It is NOT part of the existing Game Bot project.

## MVP capabilities

- Assistant user account automatically joins the group on the first /play request when allowed by chat permissions.
- `/play Alan Walker Faded` searches and attempts to play a track (YouTube/yt-dlp).
- `/vplay <title>` searches for a video (experimental 360p).
- Reply to a Telegram audio message with `/play`, or a Telegram MP4/video message with `/vplay`.
- `/play https://...` and `/vplay https://...` for direct HTTPS media on explicitly allowed hosts only.
- Per-group playback queue, `/queue`, `/skip`, `/pause`, `/resume`, `/stop`.
- `/start`, `/help`, `/ping` and HTTP `/health` for Render/UptimeRobot.
- Automatic next track on stream end (best effort); leave when the queue runs out.

Song-title audio requests first try a SoundCloud search result and then YouTube if SoundCloud is unavailable. The matching SoundCloud upload can be a remix, cover, or otherwise different from the requested recording; the response includes the actual title and its source. YouTube may refuse requests from Render cloud IPs with a sign-in challenge, and SoundCloud may also fail. Spotify direct playback is not implemented. Real voice/video playback has not yet been verified by a live test; passing Python syntax checks does not confirm playback.

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

You do NOT need to add your assistant account manually to every group.
When a group member requests `/play`, the bot attempts to join the assistant account to that group automatically.
For a public group with a username, the assistant tries joining by username.
For a private group, make the music bot an administrator with **Invite Users** permission: it will create a one-use, temporary link for the assistant, join the assistant, and revoke that link.
If an assistant is banned or user auto-join is restricted, a group administrator must adjust those settings.

Telegram group Voice Chat must already be active; the bot cannot use bot-only API methods to create a call.
An administrator starts the Voice Chat. The assistant then joins the call automatically when `/play` succeeds.
If the bot has no invite permission in a private group, it explains what setting is missing.

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
- Downloaded Telegram and YouTube files are temporary. Only share media you have permission to play.
- No automatic deploy: commits are deployed manually after GitHub workflow checks, and then VC is tested with users.
- The default `MAX_ACTIVE_GROUPS=2` is a test cap, not evidence of stable concurrent streaming.
