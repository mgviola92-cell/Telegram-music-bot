# Music Bot owner controls

Open the Music Bot's **private chat** and bind the owner once with:

```
/owner <one-time setup code>
```

The code is provided privately during installation. The first successful claim
persists the numeric Telegram user identity; the code cannot replace an existing
owner. After binding, `/owner` or `/start` opens the owner menu. Other users and
forwarded/group callback buttons cannot change these settings.

## Greeting templates

Four independent templates apply to all groups:

- Normal voice-chat welcome.
- New group-member welcome.
- Full opening introduction for `/လူတောင်းပွဲ`.
- Short collective welcome for subsequent party arrival batches.

Select a template, choose Edit, and send the new text in private chat. Both Burmese
voices are synthesized before saving to check the speech duration. Normal/group
text may contain `{name}`. Opening and collective introductions cannot contain
name placeholders. Normal/group/batch text is limited to 300 characters and the
opening introduction to 600. All generated speech is bounded to 40 seconds; an
oversized message is rejected rather than intentionally cut off.

Each template has male/female MP3 previews and a reset-to-default button.
`/cancel` cancels text entry. The back/home buttons keep the owner menu accessible.
No group message or VC announcement is sent by the preview action.

The default voice can be changed in the owner menu. A voice explicitly selected
in a group's `/welcome` menu still takes precedence. Changing text does not
restart current music; subsequent welcome requests use the saved template.
Five-second joining/batching and existing speech ducking remain unchanged.

## Status and groups

Status shows current VC count, TTS readiness, and custom template count.
The paginated group list contains groups observed by this Music Bot, not an
assertion that Telegram provides a complete historical list of all installations.
It stores only group IDs and titles, up to 200 persisted groups.

## Persistence and recovery

`music_owner_store.py` stores one JSON document named
`mudyar_music_settings.json` in the **music assistant's private Saved Messages**,
captioned `MUDYAR_MUSIC_SETTINGS_V1`. This keeps settings across Render's ephemeral
filesystem resets without using the Game Bot's database. The setup code is never
written to that document. Voice previews are sent only to the owner's private
chat. No tokens or session strings are saved in the settings document.

Configure `MUSIC_OWNER_SETUP_CODE` with a random secret of at least 20 characters
before the first claim. A persisted owner identity always takes precedence over
that environment value. If the saved document cannot be loaded or validated,
owner controls fail closed; ordinary music playback continues. `/owner` can
retry loading at most once per minute.

`MUSIC_OWNER_PROBE_UNTIL` accepts an expiring Unix timestamp within 900 seconds
of startup for a deployment-only create/edit/read persistence roundtrip. It
never binds a real or test owner, changes greeting templates, or joins a VC.

## Recorded greetings

Each of the four templates has its own Voice/Audio menu and a pool of up to ten
recordings. Uploads append to that pool rather than replacing earlier recordings.
Every greeting chooses a random file from its own category, avoiding the last
successfully selected file in that chat when alternatives exist. Preview/delete
controls address individual files; removing all files restores TTS. Existing single
recordings migrate without changing owner identity or saved text. Upload a voice message
or audio file up to 5 MB and 40 seconds. The bot validates and converts it to MP3,
then stores its own Telegram file ID with the settings. Decoded audio is bounded
in RAM; temporary conversion uses pipes. Failed retrieval falls back to saved TTS.

Choose TTS, the recording alone, or (for normal VC/group greetings with recordings
of at most 30 seconds each) a dynamic name spoken by TTS followed by the recording.
Uploading a party introduction does not change normal VC greetings or their names.
Preview plays only in the owner's private chat. Removing a recording restores TTS.

## Owner Broadcast

Open Broadcast, send or forward one announcement, select known groups, and review.
The bot shows a private copy and the target count before the explicit Send button.
Only that confirmation starts delivery; opening menus and restarting never sends.
Text, photos, videos, audio, voice, documents and animations are supported when
Telegram permits copying. Recipients see a copy from this bot rather than a forward.

Progress shows sent, failed/unconfirmed and not attempted counts. Cancel stops
remaining groups; delivered announcements cannot be recalled by Cancel. Requests
are paced, short FloodWaits are respected, and uncertain deliveries are not retried.
The saved draft survives restart, but an interrupted delivery is never auto-resumed.

## Private song requests

In private chat, send `/play title`, `/song title`, `/search title`, or a song name.
The bot shows five related results per page with Previous/Next inline buttons, editing
the same message. Selecting a result delivers that specific track as the bot’s own MP3
with a Lyrics button; metadata lookup alone never downloads the audio or joins a VC.
Private search sessions expire after five minutes; only the requester can use their
buttons. Searches are bounded to two concurrent metadata lookups, and a new group
play request can preempt them. A new search replaces that user’s earlier session.
Files are limited to 20 MB and ten minutes. One private request runs at a time,
with a 15-second per-user interval. Group VC preparation takes priority and can
cancel a private lookup. Cached Telegram file IDs expire after one hour and the
cache is limited to 64 entries. Temporary source files are cleaned after delivery.
Strict stream-only mode intentionally disables file delivery. Existing group direct
HTTPS streaming and group source selection are unchanged.

## Join a VC without music

A group admin can open the group VC and send `/join`. The assistant joins with
a silent stream, enables normal VC greetings, and waits for subsequent arrivals.
It does not play an opening announcement or restart an existing song. `/stop`
leaves. `/welcome` opens the voice/style/mode settings and does not itself join.
`/လူတောင်းပွဲ` (`/partyvc`) joins if necessary, selects party mode, and speaks the
full introduction after five seconds. Normal welcomes wait five seconds and use
the member’s name according to the selected TTS/recording mode. Party arrivals
are collected into batches and use the short collective greeting. Existing
participants in the initial eight-second connection snapshot are not announced.

## On-demand lyrics

The player card, private MP3 and recognized-song MP3 have in-bot Lyrics buttons.
A click searches LRCLIB metadata with title/artist and duration checks, then opens
one paginated text message. Previous/Next edit that same text message; Close
deletes it. Only the requesting user can page or close their view. Lyrics retrieval
never runs during ordinary playback or song search. No match or a service failure
shows an external search button rather than unrelated text. Burmese coverage
depends on the provider's catalog. Results are bounded/cached in RAM; no lyrics
files are saved. `LYRICS_PROBE_UNTIL` is an expiring deployment-only lookup of a
fixed sample, logs only status/length and sends no Telegram messages.

## Fun audio effects

The player card's Effects menu exposes twelve exclusive presets:
Normal, Nightcore, Bassboost, Slowed, Radio, Echo, Chorus, Tremolo, Vibrato,
Wide Stereo, Chipmunk and Tiny Voice. Choose one effect at a time using the card buttons. Normal turns it off.
There are no standalone effect/quality/normalization commands or menus; `/radio`
is reserved for the live radio station picker, never the Radio audio preset.
Existing admin permissions and next-track application remain unchanged: selecting
an effect never restarts the current song. Normalize remains an independent option.

Radio narrows the frequency range; Echo adds a short reflection; Chorus adds a
light delayed layer; Tremolo varies loudness; Vibrato varies pitch. Wide Stereo
requires High (stereo) quality and is bypassed for mono qualities. Chipmunk raises
pitch and speed to 1.35x; Tiny Voice uses 1.25x with subtle vibrato. Progress and
playback-duration calculations use those same speed ratios. These are ordinary
live FFmpeg filters, not AI voice cloning. Meow conversion is not included.

Vocal Reduce is a live centre-reduction effect in the existing music-card Effects
picker, with no standalone command or external service. It keeps 10% of the
shared stereo centre and mixes the difference signal in the same polarity into
both output channels, so mono phone playback remains audible. Genuine mono
sources bypass the matrix; multichannel sources use FFmpeg's mono/stereo downmix.
All output qualities are supported.
It can also reduce centred instruments/bass; stereo vocals, reverb and backing
voices may remain. Dual-mono recordings can become quieter. This is not AI
stem separation or an MP3 generator. Normalize is independent and can increase
the remaining voice along with the music. Effects still start on the next track;
Normal restores the unfiltered path for subsequent tracks.

`MUSIC_EFFECTS_PROBE_UNTIL` enables an expiring deployment-only synthetic FFmpeg
benchmark. It decodes sixty seconds per preset, logs CPU/wall seconds, and does
not send messages or join voice chats. Cases run sequentially and stop before
starting another case if any VC is active/preparing. The figures measure filter
processing, not end-to-end network/Telegram load or a guarantee for four VCs.
`MUSIC_EFFECTS_PROBE_ONLY=vocal` limits that expiring probe to two ten-second
stereo cases (Vocal Reduce with Normalize OFF/ON), with no audio files or API calls.



## Card layout
Private `/owner` → ကဒ်အပြင်အဆင် changes the global brand, playing card and queue reply. Plain text only; placeholders: `{brand}`, `{title}`, `{duration}`, `{requester}`, `{position}`, `{status}`. Brand: 100 characters, templates: 400. Reset each independently. Settings share the existing private Saved Messages persistence and preserve greeting recordings/broadcast settings. New cards use the saved layout. No seekable Telegram timeline; static duration only, no periodic card edits. Missing SoundCloud artwork uses a bundled JPEG after audio has already started. All player controls and queue popup remain.


## Bandwidth savings
Photo cards reuse up to 32 of this bot's Telegram photo IDs. Private MP3 file IDs are kept for up to seven days (64 entries); repeated source audio is keyed by Telegram file_unique_id when available. Both caches store IDs, not media bytes, and reset on restart. Private search metadata is reused for 60 seconds / 32 queries.

An empty VC with a valid assistant-only participant snapshot is paused after three minutes and rechecked every 30 seconds. A JOIN event resumes it before welcome greeting handling; polling also recovers missed JOIN events. Manual admin pauses are never automatically resumed. Unknown snapshots / API errors do not trigger pause. Silent greeting input stays active when VC greetings are enabled. Audio quality, direct streaming, radio/autoplay settings, disk cache limits and source ordering remain unchanged. Native transport keepalive can still consume bandwidth while paused; no guaranteed percentage reduction.


Default artwork: `/owner` → ကဒ်အပြင်အဆင် → Default ပုံ. Send a compressed Telegram Photo up to 5 MB; the bot stores only its file_id in existing private Saved Messages settings. Preview, replace and reset are owner-only. Changes apply to future cards only when source artwork is missing. Invalid Telegram references fall back to bundled artwork. No photo download or extra Render disk cache.


Player / queue names now use Telegram mention entities (blue clickable names); owner templates remain plain text with escaped HTML. Pause/resume edits the existing player card with a quote naming the controlling admin. Closed/disconnected VC cleanup stays silent. Active VC /play skips redundant membership RPC; audio-frame polling is 250 ms with the same eight-second timeout and source fallback safety checks. First-time source resolution and Telegram connection latency still vary.


## Burmese home/status card
`/start` shows the configured default artwork, current brand and a compact Burmese runtime status. Help and refresh edit the same card; `/help` and `/cmd` retain the full existing command guide. `/owner` retains private controls, and its status page includes the same runtime measurements. CPU is sampled on demand since the previous sample, normalized to the Linux cgroup CPU quota when available (otherwise explicitly one-core basis); memory uses cgroup usage/limit or process RSS. Disk is the filesystem containing temporary media, not Render's billed storage quota. Uptime resets at restart. No background status polling, new package or external API; results are cached for five seconds. Public panels are bounded to 128 / one hour, requester-only, and refresh is rate-limited. No provider bandwidth quota is fabricated or exposed.
