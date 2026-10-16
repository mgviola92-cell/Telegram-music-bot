# MVSEP Karaoke verification

Hugging Face is not used by this implementation. No AI model or extra dependency
is installed on the Music Bot's Render service. Existing playback paths are unchanged.

`MVSEP_API_TOKEN` is read only inside Render. Never put it in Git, a message,
diagnostic output or a public health endpoint. The client uses the documented
MVSEP API, MDX-B Inst_HQ_3 (`sep_type=23`, `add_opt1=8`), MP3 output and `is_demo=0`.
The owner account must have premium spending disabled; existing unrelated queued
jobs are never canceled. There are no paid mirrors or quota retries.

An expiring setup probe can use `MVSEP_PROBE_DISABLE_PREMIUM=1` after informing
the owner that premium credits will be disabled for free-only use. It changes
only an explicitly enabled flag, calls the documented disable endpoint, and
re-reads the account before uploading. Normal Karaoke jobs never change account
preferences. Clear this setup flag with the probe flag after testing.

## Diagnostic phase

Set `MVSEP_PROBE_UNTIL` to a Unix timestamp up to 15 minutes in the future after
CI has passed. It enables one background synthetic 20-second voice/tone test
on startup. It waits for no current/idle/loading player and cancels itself if
playback begins. It sends no Telegram messages and never uses a user's recording.
The local temp folder is removed and our remote job is best-effort canceled/deleted.
No raw exception, upstream message, URL, token or account detail is logged.

A PASS means upload, separation, instrumental selection, bounded download, MP3
packet copy, duration validation and decoding worked. It does NOT prove a real
song's perceptual quality or full-song queue/latency. Enable the user-facing
player-card feature only after this verification and report its limitations.

## Resource bounds

- One request at a time, asynchronous network I/O, fixed five-second polling.
- Up to 20 MiB input and 32 MiB downloaded result, validated output <=20 MiB.
- Up to five-minute input; remote queue/processing timeout four minutes.
- MP3 response is packet-copied, not re-encoded. FFmpeg uses one thread.
- Files are streamed from disk rather than read wholly into RAM for upload.
- Plain `/play`, `/search`, effects and direct streaming do not invoke MVSEP.
- A Karaoke request still needs a complete input file and network/disk bandwidth.
  This is not a claim of zero additional load while Karaoke is requested.

## Live result

16 October: authenticated account/API and synthetic upload passed. The account's
premium-credit consumption was disabled and re-read as disabled. One 20.064-second
sample stayed waiting for four minutes and timed out before GPU processing.
No instrumental result or real-song quality has passed yet. A second isolated
diagnostic may wait up to ten minutes; normal user jobs retain four-minute remote
and five-minute total limits. The player-card UI is prepared but disabled by default.
After a technical remote PASS, set `MUSIC_KARAOKE_ENABLED=1` and
`MVSEP_PROBE_UNTIL=0`. This exposes a beta Karaoke button for group admins.
It takes the exact currently playing song, keeps playback running, and returns
the bot's own MP3 with an explicit Play button; it never swaps the current stream.
Only one global job, six requests/hour, 96 MiB RAM headroom and 128 MiB disk
headroom are enforced for Karaoke admission. Resource refusal never stops music.
Results are bounded to eight Telegram message references for one hour.
Existing Radio and music-card-only effects are preserved.
