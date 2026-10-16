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

Each of the four templates has its own Voice/Audio menu. Upload a voice message
or audio file up to 5 MB and 40 seconds. The bot validates and converts it to MP3,
then stores its own Telegram file ID with the settings. Decoded audio is bounded
in RAM; temporary conversion uses pipes. Failed retrieval falls back to saved TTS.

Choose TTS, the recording alone, or (for normal VC/group greetings with a recording
of at most 30 seconds) a dynamic name spoken by TTS followed by the recording.
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
