# Karaoke: prepared, disabled pending authenticated live verification

The production bot remains on its last verified release. Karaoke is not enabled
and this branch is not a claim that either remote provider passed separation.

## Actual tests, 16 October 2026

A short synthetic English Flite voice mixed with two stereo synthesized tones was
uploaded (no user's recording or Telegram conversation was sent).

- MohamedRashad/Audio-Separator: upload and API submission worked, separation
  returned an upstream TypeError. This backend is not selected in the client.
- modulsx/BS-RoFormer-Leap-Audio-Separator: upload and public `separate` endpoint
  worked. GPU execution was refused: 180 seconds requested, 178 seconds of
  anonymous quota remaining. The service requested Hugging Face authentication.
  This is a quota rejection, not a completed separation or measured song latency.
- Modal: no account credentials or existing endpoint were available. Deployment,
  GPU execution, free-credit balance and zero-spend limits have NOT been verified.

Local tests exercise real audio preparation/validation, disposable source copies,
local HTTP upload/SSE/download, quota failures without retries, untrusted output
URLs, byte limits, admin-only UI, one independent job, and disabled-by-default UI.
They do not measure real GPU quality or full-song separation latency.

## Hugging Face setup

1. Use the owner's own Hugging Face account and access token. Add `HF_TOKEN` to
   the Music service's Render environment, not a repository file or chat message.
2. Keep `MUSIC_KARAOKE_ENABLED=0` until a successful authenticated short sample
   and representative full track have been decoded and compared.
3. Test `HFSeparator(token).separate(input_path, output_path)` using the approved
   public Space. The client forwards both Authorization and x-hf-authorization
   only to that exact host. Never rotate identities or retry a quota failure.
4. After successful tests, set `KARAOKE_PROVIDER=hf` and
   `MUSIC_KARAOKE_ENABLED=1`, merge/deploy this branch, and verify the bot startup.

## Separate Modal worker setup

The bot's Render requirements never include Torch, audio-separator, Gradio or Modal.
`karaoke_modal_worker.py` is deployed separately by the owner on Modal.

1. Create/login to the owner's Modal account. Verify remaining applicable free
   compute credits. Configure a Workspace net spend limit of $0 and a usage
   budget no higher than the remaining free allocation before deploying.
   Modal's default spend limit can permit paid usage: do not rely on defaults.
2. In a separate development environment install `modal` and `fastapi`, run
   `modal setup`, then `modal deploy karaoke_modal_worker.py`.
3. Create a Modal Proxy Auth token. The worker requires platform-level proxy
   authentication, one T4 container, a 180-second execution timeout, short
   scale-down, <=20 MB inputs and <=5-minute tracks. No public unauthenticated
   GPU endpoint is enabled. The exact worker/model version still needs a real
   deployment test before production use.
4. Store its endpoint in `KARAOKE_MODAL_URL` and proxy credentials in
   `KARAOKE_MODAL_KEY` / `KARAOKE_MODAL_SECRET` on Render.
5. Only after verifying the $0 spend cap, set
   `KARAOKE_MODAL_FREE_CONFIRMED=1`. Test the endpoint with a sample and a full
   track before setting `KARAOKE_PROVIDER=modal` and enabling Karaoke.

Select one provider explicitly. There is no automatic paid fallback or race of
GPU requests. These credentials/settings are never logged by the client.

## User flow and resource limits

A group admin presses Karaoke on the current song card. The manager snapshots
the exact currently playing file/HTTPS source, makes its own disposable copy,
and leaves the current song, queue and effects alone. HTTPS HLS preparation uses
stream copying rather than local AI inference. It can use network bandwidth and
some temporary disk/FFmpeg CPU only while this requested job runs; zero added
load during ordinary playback is not a claim of zero total resource use.

Only one job runs globally, at most six requests/hour in-process. Tracks are
limited to five minutes, uploads to 20 MiB, remote outputs to 64 MiB, and a job to
five minutes total. Temp folders are removed on success, failure and cancellation.
Completed results stay as the bot's own MP3 message with a Play button that queues
that file only when pressed. Eight result references are kept for one hour;
repeated clicks for the same exact current source reuse the existing ready result.

A cancellation stops local work and cleanup. A queued remote GPU task may still
consume its provider quota: cancellation is not a refund or guaranteed remote
termination. There are no automatic retries after quota errors/timeouts. Closing
Karaoke never stops ordinary music. Startup/registering the module performs no
network calls and shows no new button unless the feature and provider are configured.
