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
