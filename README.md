# Telegram VC Music Bot

Standalone Telegram voice-chat music bot. It is NOT part of the existing Game Bot project.

## MVP capabilities

- Assistant user account automatically joins the group on the first /play request when allowed by chat permissions.
- `/play Alan Walker Faded` searches and attempts to play a track (YouTube/yt-dlp).
- `/vplay <title>` searches for a video (experimental 360p).
- Reply to a Telegram audio message with `/play`, or a Telegram MP4/video message with `/vplay`.
- `/play https://...` and `/vplay https://...` for direct HTTPS media on explicitly allowed hosts only.
- Per-group playback queue, `/queue`, `/now`, `/skip`/`/next`, `/pause`, `/resume`, `/stop`/`/end`.\n- Admin-only `/shuffle`, `/remove <position>`, `/clear`, and `/loop` for the current track.\n- Interactive **now-playing inline panel** with pause/resume, skip, stop, loop, and queue buttons; non-admin users may view the queue but cannot change playback.\n- Private `/start`, `/help` and `/cmd` card with an Add to Group button.
- `/start`, `/help`, `/ping` and HTTP `/health` for Render/UptimeRobot.
- Automatic next track on stream end (best effort); leave when the queue runs out.

Song-title audio requests now try YouTube search for an official recording first; if YouTube refuses access, the engine can optionally try Audius with configured developer API access, then tests up to three suitably matched SoundCloud results. Set `MUSIC_SOURCE_PRIORITY=soundcloud` to restore the original SoundCloud-first behavior. These sources may still fail due to provider checks, licensing/DRM, or server routing. A 15-minute local cache (two tracks, 36 MB overall and 24 MB per entry) can accelerate repeated songs on the same running instance; set `MUSIC_CACHE_ENABLED=0` to disable it. No login-cookies, proxy bypasses, or new third-party download APIs are installed. The matching SoundCloud upload can be a remix, cover, or otherwise different from the requested recording; the response includes the actual title and its source. YouTube may refuse requests from Render cloud IPs with a sign-in challenge, and SoundCloud may also fail. Spotify direct playback is not implemented. A Telegram MP3 reply and a SoundCloud song have been confirmed to play in a real group VC; the revised controls still require a live smoke test. Passing Python tests alone does not confirm all playback controls.

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
- This MVP limits queued tracks to 10 per group and Telegram audio/video messages to 20 MB (HTTPS Bot API getFile limit); video is experimental and limited to one concurrent stream.
- Video is CPU-intensive; Render Free 0.1 CPU/512 MB cannot guarantee smooth video or multiple concurrent voice chats.
- Telegram file replies are fetched through the HTTPS Bot API first, with a shorter MTProto fallback when the HTTP route is unavailable. Downloaded Telegram, SoundCloud and YouTube files are temporary. Only share media you have permission to play.
- No automatic deploy: commits are deployed manually after GitHub workflow checks, and then VC is tested with users.
- The default `MAX_ACTIVE_GROUPS=2` is a test cap, not evidence of stable concurrent streaming.

## Reference review and implementation boundaries (October 2026)

The music bot was compared with three public repositories. The controls, queue operations and group-admin permissions in this repository were **independently implemented** for the existing Render Free/pyrogrammod/PyTgCalls 2.3.3 stack. Do not copy their entire codebase or bundled images/fonts/cookies:

- [AsmSafone/MusicPlayer](https://github.com/AsmSafone/MusicPlayer), AGPL-3.0: lightweight queue, stream mode, quality levels, repeat, shuffle, radio, group-local controls and theme images.
- [CertifiedCoders/AnnieXMusic](https://github.com/CertifiedCoders/AnnieXMusic), MIT license in its repository: inline playback/settings panels, multiple assistants, admin rules, language support, thumbnails, seek/speed and extensive extras.
- [lakzexe/HasiiMusicBot](https://github.com/lakzexe/HasiiMusicBot), GPL-3.0: modular player, YouTube/Spotify metadata, radio, user authorization, queue/download caching, FFmpeg and Deno, auto-leave, thumbnails and inline controls.

Adapted **now**: group admin guards for mutating playback commands/buttons, group-bound inline player, one active control card per group, skip/next and stop/end aliases, current track repeat, shuffle next items, remove a queued item, clear upcoming queue, /now and branded Add to Group help.

Not yet adapted: Spotify or Apple Music audio playback (their metadata APIs do not grant streaming rights), playlists, seek, speed, multi-assistant scaling, persistent MongoDB settings, AI/entertainment/moderation plugins, high-resolution artwork. These need careful resource/permission review. The YouTube cloud verification challenge remains a separate upstream problem; none of the borrowed ideas guarantees bypassing it.

**Render Free limits:** no automatic 24/7 uptime or multi-group HD guarantee. FFmpeg/Deno/MongoDB, downloaded theme assets and heavy thumbnails are not added by this incremental update. Group queue, loop flag and currently playing state are in memory and reset on service restart.

### New controls

| Command | Permission | Description |
| --- | --- | --- |
| `/play title` or audio reply `/play` | Members | Search / stream or queue media |
| `/vplay title` or video reply `/vplay` | Members | Experimental video |
| `/queue`, `/now`, `/nowplaying` | Members | Show current and upcoming songs |
| `/pause`, `/resume`, `/skip`, `/next` | Group admins | Control active stream |
| `/stop`, `/end` | Group admins | Stop, leave call and reset queue |
| `/shuffle` | Group admins | Randomize upcoming songs only |
| `/remove 2` | Group admins | Remove second song from upcoming queue |
| `/clear` | Group admins | Empty upcoming queue, keep current song |
| `/loop` | Group admins | Toggle repeating the current song at natural end |
| `/start`, `/help`, `/cmd` | Everyone | Music help and group-add button |

The inline player buttons are attached only to the active Now Playing message and ignore old/stale cards.

## Current music source strategy and speed notes (October 2026)

- **Original YouTube recordings are not guaranteed.** Render's cloud IP has received YouTube's sign-in verification response; code cannot promise to stream protected YouTube audio.
- For song-title audio requests, search YouTube first, then optionally the documented **Audius** public streaming API, then SoundCloud matches. Explicit YouTube links remain YouTube-only, to avoid silently replacing the user's exact recording with a different upload.
- Audius search succeeds in the live GitHub Runner probe, but **public stream attempts returned HTTP 403** without a configured official API key. The fallback is therefore **disabled by default**, not advertised as working. To try it with authorized Audius developer access, set `AUDIUS_API_KEY` privately in the Music Bot's Render environment; it enables Audius automatically. `AUDIUS_ENABLED=0` always disables it, `AUDIUS_ENABLED=1` enables it explicitly. Some tracks may still disallow third-party streaming. Never paste API keys in Telegram, GitHub source or chats.
- Each Audius response is checked against allowed HTTPS redirect hosts, maximum media length and size, and public stream flags; unsupported/unavailable content goes to the next provider.
- **TgCrypto 1.2.5** is a Python encryption speed-up for Pyrogram/MTProto, not a cure for YouTube verification and not a solution to Render Free audio CPU limits. The failed proof-token browser plugins were removed to keep the runtime lightweight.
- A short-lived two-song cache is available. The provider search and stream may still take time, and Render Free may sleep/restart despite periodic HTTP checks.


## Playback stability improvements (October 2026)

- The source can attempt one VC reconnect after a stale join/play error, without creating a voice chat if the administrator has not opened one.
- PyTgCalls `StreamEnded` events are logged. If the song ends noticeably earlier than its known duration, a single recovery attempt reuses the existing local media file. Legitimate track endings still advance the queue; after it empties, the assistant leaves the VC.
- A lightweight in-process watchdog inspects the PyTgCalls active-call registry every 35 seconds. It attempts to recover a detached session once, and releases stale playback state if recovery fails, so a subsequent `/play` can start normally.
- `/play` also verifies stale per-chat state before appending to a queue when an old session disappeared. Voice-chat leave updates clear invalid state.
- Now-playing inline buttons acknowledge actions promptly to avoid Telegram `QUERY_ID_INVALID` during long downloads or reconnection.
- Local source and retry tests are offline: **successful GitHub Actions do not prove continuous live group-VC audio.** A real smoke test is still required after each deploy.

Render Free can restart or suspend instances and cannot guarantee uninterrupted streaming. Background watchdogs run only when the Render instance is awake; disk cache and in-memory queue disappear on restart.
