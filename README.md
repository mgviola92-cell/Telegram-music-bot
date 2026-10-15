> Current playback mode: direct SoundCloud audio is enabled by default with a local download fallback. Render FFmpeg diagnostics found that the library's `reconnect_at_eof=1` stalled HLS decoding: both original and combined-header commands produced zero PCM in 15 seconds; disabling EOF reconnection produced 96,000 PCM bytes in 1.57 seconds. `DirectAudioStream` retains normal probing and PCM format but combines HTTP headers and disables EOF reconnection for the capture command. Real listener-side VC playback still requires confirmation. `MUSIC_STREAM_ONLY` remains off by default.

# Telegram VC Music Bot

Standalone Telegram voice-chat music bot. It is NOT part of the existing Game Bot project.

## MVP capabilities

- Assistant user account automatically joins the group on the first /play request when allowed by chat permissions.
- `/play Alan Walker Faded` tries SoundCloud immediately, then a rights-filtered Internet Archive fallback if necessary.
- During playback the Now Playing card has a progress bar, optional trusted SoundCloud cover art and a lyrics lookup link. Direct HTTPS audio is optional via `/stream on`; normal playback uses downloaded media.
- `/vplay` on a replied-to Telegram MP4 is supported; YouTube title/link video extraction is off by default.
- Reply to a Telegram audio message with `/play`, or a Telegram MP4/video message with `/vplay`.
- `/play https://...` and `/vplay https://...` for direct HTTPS media on explicitly allowed hosts only.
- Per-group playback queue, `/queue`, `/now`, `/skip`/`/next`, `/pause`, `/resume`, `/stop`/`/end`.\n- Admin-only `/shuffle`, `/remove <position>`, `/clear`, and `/loop` for the current track.\n- Interactive **now-playing inline panel** with pause/resume, skip, stop, loop, and queue buttons; non-admin users may view the queue but cannot change playback.\n- Private `/start`, `/help` and `/cmd` card with an Add to Group button.
- `/start`, `/help`, `/ping` and HTTP `/health` for Render/UptimeRobot.
- Automatic next track on stream end (best effort); when the queue runs out stay silently connected, without a new idle announcement.

**Default audio order:** SoundCloud (up to three well-matched results), then Internet Archive (only eligible CC BY / CC0 MP3 uploads with attribution), then Audius if separately configured with authorized API access. `YOUTUBE_ENABLED=0` by default: YouTube keeps returning a sign-in verification challenge from Render, which unnecessarily delayed songs. Set `YOUTUBE_ENABLED=1` only if you have a properly authorized workflow that can actually obtain source access. A YouTube link without permission now gives a quick explanation and is not replaced with a different recording. Spotify and Apple Music metadata do not grant full-audio streaming rights. A 15-minute cache (up to two local tracks) can speed up repeated songs; a non-matching SoundCloud upload is not guaranteed to be the original recording. Internet Archive includes independent and open-license music, not a guaranteed catalog of current Myanmar or international chart hits. GitHub unit tests and an Archive MP3 HTTP 206 check do not prove real-VC playback until a group test.

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

- `MAX_ACTIVE_GROUPS`: default 4 simultaneous voice streams, bounded at 8 in code. The number of groups that can add the bot is not limited by this setting.
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
- Video is CPU-intensive; Render Free has extremely limited CPU (observed service limit 0.15 CPU) and 512 MB RAM. Four concurrent calls is a trial cap, **not** a guarantee of smooth audio; lowering the cap may be necessary.
- Telegram file replies use HTTPS Bot API first with MTProto fallback. Downloaded Telegram, SoundCloud and open-license Archive files are temporary. Only share music you have permission to play.
- No automatic deploy: commits are deployed manually after GitHub workflow checks, and then VC is tested with users.
- The default `MAX_ACTIVE_GROUPS=4` is a trial cap, not evidence of stable concurrent streaming. Idle calls count against that cap; a new song request evicts the oldest idle group before denying service. Groups may install the bot without this concurrent-VC limit.

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
| Telegram MP4/video reply `/vplay` | Members | Experimental video. Title-based YouTube video extraction is off by default |
| `/queue`, `/now`, `/nowplaying` | Members | Show current and upcoming songs |
| `/pause`, `/resume`, `/skip`, `/next` | Group admins | Control active stream |
| `/stop`, `/end` | Group admins | Stop, leave call and reset queue |
| `/shuffle` | Group admins | Randomize upcoming songs only |
| `/remove 2` | Group admins | Remove second song from upcoming queue |
| `/clear` | Group admins | Empty upcoming queue, keep current song |
| `/loop` | Group admins | Toggle repeating the current song at natural end |
| `/radio on`, `/radio off`, `/radio status` | Group admins to toggle; everyone to view | Optional authorized radio when queue empties |
| `/idle on`, `/idle off`, `/idle status` | Group admins to toggle; everyone to view | Keep a silent VC session open after the queue ends, or revert to auto-leave |
| `/start`, `/help`, `/cmd` | Everyone | Music help and group-add button |

The inline player buttons are attached only to the active Now Playing message and ignore old/stale cards.

## YouTube verification behavior (October 2026)

- Render Logs repeatedly show `Sign in to confirm you're not a bot` on YouTube requests from the service. Successful metadata lookup **does not** mean that a downloadable or redistributable YouTube audio stream has been authorized. No change here guarantees original YouTube audio in Telegram VC.
- The documented YouTube IFrame API plays videos inside YouTube's embedded player. It does not supply standalone audio files for redistribution to Telegram calls.
- **YouTube extraction is disabled by default on Render (`YOUTUBE_ENABLED=0`).** If an authorized owner explicitly enables it and the provider returns two verification errors in ten minutes, the in-process `youtube_health.YouTubeGuard` pauses further YouTube attempts for 15 minutes. This preserves a faster fallback to eligible other audio providers without attempting to defeat the verification process. A direct YouTube URL during the cooldown reports it unavailable instead of silently using a different recording. After the cooldown, one normal YouTube attempt is permitted again.
- Verification cooldown is separate from the existing per-song cache, voice recovery and radio/idle behavior. It resets on service restart. Incorrect legacy `extractor_args.youtube.client` settings were removed.
- If a permitted alternate audio provider is also unavailable, the bot gives a clear source-unavailable reply and suggests playing the video through YouTube's own player or using an authorized Telegram audio attachment. Upload/share only media you have rights to use.

## Current music source strategy and speed notes (October 2026)

- **Original YouTube recordings are not guaranteed.** Render's cloud IP has received YouTube's sign-in verification response; code cannot promise to stream protected YouTube audio.
- For song-title audio requests, **SoundCloud is checked first**, then **CC BY / CC0 Internet Archive** audio. Audius is optional and only attempted with an explicitly configured API key. YouTube is not checked at all by default. Direct YouTube links show a clear explanation rather than substituting a different recording.
- Audius search succeeds in the live GitHub Runner probe, but **public stream attempts returned HTTP 403** without a configured official API key. The fallback is therefore **disabled by default**, not advertised as working. To try it with authorized Audius developer access, set `AUDIUS_API_KEY` privately in the Music Bot's Render environment; it enables Audius automatically. `AUDIUS_ENABLED=0` always disables it, `AUDIUS_ENABLED=1` enables it explicitly. Some tracks may still disallow third-party streaming. Never paste API keys in Telegram, GitHub source or chats.
- Each Audius response is checked against allowed HTTPS redirect hosts, maximum media length and size, and public stream flags; unsupported/unavailable content goes to the next provider.
- **TgCrypto 1.2.5** is a Python encryption speed-up for Pyrogram/MTProto, not a cure for YouTube verification and not a solution to Render Free audio CPU limits. The failed proof-token browser plugins were removed to keep the runtime lightweight.
- A short-lived two-song cache is available. The provider search and stream may still take time, and Render Free may sleep/restart despite periodic HTTP checks.


## Empty-queue VC and radio fallback (October 2026)

When the queue empties, **the assistant stays in its current group Voice Chat**.
It tries an approved live radio stream first, but only if the owner has privately configured
an authorized station URL. Without one, it uses a **15-minute looping silent WAV**.
This is a quiet VC waiting mode, **not** a real Myanmar radio station. Music requests
preempt idle immediately. **No idle status message is posted to the group.** `/stop` always clears the queue and leaves.

**Radio setup (Music Bot Render service only):** configure
`RADIO_STREAM_URL=https://<authorized-station-host>/<direct-audio-path>` and
`RADIO_STREAM_HOSTS=<authorized-station-host>` using the private Render
environment. The host must be an exact HTTPS hostname; the stream must be
authorized for redistribution to the Telegram voice chat. A station website
address is not necessarily a direct audio URL, and the ability to listen on a
website does not necessarily grant rebroadcast permission. No third-party
station stream is enabled by default. `/radio on` and `/radio off`
control automatic radio fallback per group; `/idle off` restores the old
auto-leave behavior for that group.

**Capacity:** unlimited installed groups, default 4 simultaneous group VCs.
A silent or radio idle session uses a real VC slot, so a new group's song can
displace the longest-idle group. The bot notifies that group when an idle VC
is released. The oldest idle stream is evicted rather than rejecting a song.
Only songs in use are guaranteed preference, and Render Free may not support
four concurrent calls at acceptable quality. The cap can be adjusted by
`MAX_ACTIVE_GROUPS` (hard maximum 8) after performance testing.
Restarting Render clears group-specific toggle settings.

## Streaming startup latency (October 2026)

- Real VC logs have confirmed `direct=True` followed by playback success. This does not imply instant startup: search, media URL resolution and Telegram/FFmpeg initialization still take time.
- Search results (SoundCloud track pages, not expiring signed audio URLs) are kept for ten minutes, bounded to 48 queries. Repeat searches resolve fresh media tokens without repeating the track search; failed resolutions invalidate the entry.
- Direct audio uses the standard PyTgCalls/FFmpeg probing settings. Reduced probing was reverted after a real-VC report of silent playback; a successful calls.play response does not establish audible sound. Playback failures retain the local-media fallback.
- Queue preparation uses the same direct-stream setting as `/play`, rather than always downloading the next song.

## Compact now-playing UI and automatic remote streaming (October 2026)

- The single active Now Playing card shows **elapsed / duration**, a short Unicode progress bar and a decorative sound-wave pulse. A single message/caption is edited roughly every **25 seconds**; no recurring new messages are sent. Pausing freezes the displayed elapsed timer; resuming continues it. This is a time estimate, not sample-accurate audio analysis.
- When SoundCloud's metadata includes artwork from an approved `sndcdn.com` HTTPS image host, the bot can show it as a **single photo with caption**. If the image is missing, fails, or Telegram cannot fetch it promptly, the panel falls back to plain text. The UI is posted asynchronously **after** VC playback begins, so artwork failure does not delay hearing music.
- The **🎤 Lyrics ↗** button opens a Genius lyrics-search page. Copyrighted lyrics are **not copied or auto-posted into the Telegram group**. Availability/accuracy depends on the external site.
- Optional **direct HTTPS playback** uses a publicly playable, non-DRM, validated SoundCloud CDN URL with PyTgCalls `MediaStream(url, headers=...)`. It does *not* download the entire song into memory; FFmpeg buffers chunks. This is consistent with PyTgCalls' remote-stream examples, but it is not guaranteed faster than downloads, and expiry, redirects, restrictions, missing headers or network quality can still cause a failure. If PyTgCalls rejects the URL immediately, the bot retries through its existing local-file download path.
- **Direct streaming defaults OFF for each group**, including after Render restarts, following real-VC failures. Normal `/play` uses local download/cache. Admins can use `/stream off` to switch future requests to local download, or `/stream on` to re-enable it; manual overrides last until Render restarts. One-track-ahead prefetch follows the group streaming mode: it resolves a direct stream when enabled, with local download fallback when necessary.
- Source URLs must use the official public HTTPS SoundCloud media host family. The streaming resolver refuses unrelated/private-network hosts, blocked/premium/DRM audio and does not pass cookies or authorization headers. Copyright and creator permissions still apply.
- The cosmetic UI does **not** change the 4-VC Render Free concurrency cap or remove the platform's performance limits.

## Faster skip and queue transitions (October 2026)

- `/skip`, `/next` and the inline Skip button now acknowledge immediately and switch audio via a background task. Duplicate skip requests are ignored while a switch is in progress; the worker only skips the track that was active when the request arrived.
- While a song is playing, the bot **prefetches at most one next queued audio track** in the background. On skip or natural completion, the playback worker uses the prepared media file instead of running SoundCloud search and download after the skip. The next track must have been queued early enough for prefetch to complete to get the full speed benefit.
- `/remove`, `/shuffle`, `/clear`, `/stop` and player changes discard or replace obsolete prepared media and clean temporary files. `MUSIC_PREFETCH_ENABLED=0` disables prefetch if Render Free memory/disk/network limits prove restrictive.
- Bot event logging now separates **SoundCloud search latency** and **provider download latency** without storing users' song names in logs. This allows precise tuning of slow first-time `/play` requests. Initial uncached songs still require network search/download and may take many seconds. Prefetch does not guarantee instant playback if the next song was just queued.
- The existing 4-group concurrent VC cap, silent idle, per-group queue, license-filtered sources and original music button permissions are unchanged.

## Faster playback and additional license-safe source (October 2026)

- The user's Render logs on Oct 14 show repeated YouTube sign-in verification failures. Rather than wasting time on a failing extract attempt for **every request**, YouTube is disabled by default, while `yt-dlp` remains installed for SoundCloud extraction. No cookies, fake sessions, alternate accounts, bypasses, or unlicensed extraction are added.
- Open-license Internet Archive music is a **genuine alternate catalog**: public metadata search returned HTTP 200, and a CC BY 3.0 MP3 Range request returned HTTP 206 from an Archive-operated CDN. The bot only accepts items whose metadata explicitly advertises CC BY 3.0/4.0 or CC0 1.0 and provides track/uploader attribution. A license label supplied by an uploader cannot fully establish rights; respect any takedown or dispute.
- Stream matching requires at least some requested title terms in the file and cross-checks creator/title. `ARCHIVE_ENABLED=0` disables this fallback if the API becomes slow; otherwise it is used only **after SoundCloud candidates fail**.
- Jamendo's developer API test key currently returns **suspended application (code 11)** in live probes; the API was not enabled as a production source. Audius still needs a valid developer-access key/permission. Adding provider names without working API access does not make mainstream licensed tracks playable.
- When a song ends with an empty queue, a muted VC stream keeps the assistant present but posts **no idle chat notification**. Group toggle `/idle on|off` still controls the waiting behavior.
- `/vplay` is still available for an authorized Telegram MP4 reply, but YouTube-video-by-title playback requires explicit access and is off by default.

## Playback stability improvements (October 2026)

- The source can attempt one VC reconnect after a stale join/play error, without creating a voice chat if the administrator has not opened one.
- PyTgCalls `StreamEnded` events are logged. If the song ends noticeably earlier than its known duration, a single recovery attempt reuses the existing local media file. Legitimate track endings still advance the queue; after it empties, the assistant switches to radio (only when explicitly configured) or a quiet idle loop instead of leaving the VC. An explicit `/stop` still leaves.
- A lightweight in-process watchdog inspects the PyTgCalls active-call registry every 35 seconds. It attempts to recover a detached session once, and releases stale playback state if recovery fails, so a subsequent `/play` can start normally.
- `/play` also verifies stale per-chat state before appending to a queue when an old session disappeared. Voice-chat leave updates clear invalid state.
- Now-playing inline buttons acknowledge actions promptly to avoid Telegram `QUERY_ID_INVALID` during long downloads or reconnection.
- Local source and retry tests are offline: **successful GitHub Actions do not prove continuous live group-VC audio.** A real smoke test is still required after each deploy.

Render Free can restart or suspend instances and cannot guarantee uninterrupted streaming. Background watchdogs run only when the Render instance is awake; disk cache and in-memory queue disappear on restart.

## Audio presence and microphone checks

Audio playback and recovery require an actual audio stream (`MediaStream.Flags.REQUIRED`), so missing audio cannot silently produce a player card with no microphone source. A direct URL that fails audio detection uses the existing local download fallback without an unnecessary VC reconnect. After starting a song, the bot checks its assistant participant: admin mute is shown in the player card, local capture is unmuted, and a zero volume is restored to 100. An admin mute is never overridden. These checks and a successful play response do not prove that a listener heard sound; confirm in a real group VC.

After an audio play request, the loader waits up to eight seconds for the native outgoing media clock to advance. A direct stream that is accepted but supplies no frames triggers the local fallback once; the same failure on local audio produces an explicit error. Clock progress proves decoder frame flow, not listener-side audibility. Stop button requests and completions are logged for restart tracing.

Optional diagnostic: `MUSIC_STREAM_PROBE_QUERY` resolves that named track on startup and compares three FFmpeg commands, each reading only a half-second PCM sample into RAM with a 15-second deadline. It does not join a VC. Errors redact URLs/tokens. Keep this variable empty during normal operation.


### LyBot fallback for named audio

`/play <song name>` keeps the existing direct SoundCloud path. With `LYBOT_ENABLED=1`
(the default), named audio resolution waits up to 12 seconds for the primary
provider, then tries the verified `@LyBot` using the existing assistant account.
The LyBot request and transfer have a 28-second deadline; a busy bridge waits at
most 2 seconds for its single request lock. Cancellation cleans partial media.
No sign-in challenge, subscription, or payment button is handled automatically.
Explicit YouTube URLs, videos and strict stream-only mode retain their existing behavior.

LyBot audio uses a bounded temporary local file (20 MiB / 10 minutes maximum),
then the normal FFmpeg/VC player. This fallback is not disk-free direct streaming.
A 15-minute cache reuses the existing shared 2-file / 36 MiB cache budget.
A matching cached fallback skips another search. No bot token or session is logged.
Set `LYBOT_ENABLED=0` to disable this fallback. The opt-in startup diagnostic
`MUSIC_BOT_PROBE_UNTIL` is off at `0`, and only accepts a timestamp within 15 minutes.
