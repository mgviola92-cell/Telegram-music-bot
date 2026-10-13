"""One-shot public Audius API smoke check, without Telegram secrets."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audius_source import find_audius_tracks, fetch_audius_audio

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
    result = fetch_audius_audio(
        title, match=lambda title, query, artist: True
    )
    assert result is not None, "Audius stream unavailable with strict redirect rules"
    audio_file, label, seconds = result
    size = Path(audio_file).stat().st_size
    print("Audius streaming OK: bytes=", size, "seconds=", seconds)
    assert size > 1024
    Path(audio_file).unlink(missing_ok=True)
