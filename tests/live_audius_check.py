"""One-shot public Audius API smoke check, without Telegram secrets."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import logging
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlencode
from audius_source import find_audius_tracks, fetch_audius_audio

logging.basicConfig(level=logging.INFO)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def inspect_stream_endpoint(track_id: str) -> None:
    endpoint = "https://api.audius.co/v1/tracks/" + track_id + "/stream?" + urlencode({"app_name": "mudyar-music"})
    try:
        with build_opener(NoRedirect()).open(Request(endpoint), timeout=10) as response:
            print("Audius stream initial status:", response.status, "mime:", response.headers.get("Content-Type"))
    except HTTPError as error:
        location = error.headers.get("Location", "")
        print("Audius stream initial HTTP:", error.code, "redirect_host:", urlsplit(location).hostname)
    except Exception as exc:
        print("Audius stream diagnostic error class:", type(exc).__name__)


if __name__ == "__main__":
    results = find_audius_tracks("ambient")
    print("Audius API reachable, results:", len(results))
    assert results, "Audius search returned no public tracks"
    track = next((
        row for row in results
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and row.get("is_stream_gated") is not True
        and row.get("is_streamable") is not False
        and (row.get("duration") or 0) > 0
        and (row.get("duration") or 0) < 580
    ), None)
    assert track, "No public streamable short track"
    title = str(track.get("title") or "")
    assert title
    print("Testing one publicly streamable Audius track (title length):", len(title))
    inspect_stream_endpoint(str(track["id"]))
    result = fetch_audius_audio(
        title, match=lambda title, query, artist: True
    )
    assert result is not None, "Audius stream unavailable with strict redirect rules"
    audio_file, label, seconds = result
    size = Path(audio_file).stat().st_size
    print("Audius streaming OK: bytes=", size, "seconds=", seconds)
    assert size > 1024
    Path(audio_file).unlink(missing_ok=True)
