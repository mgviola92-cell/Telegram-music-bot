"""Creative Commons BY / CC0 audio fallback from the Internet Archive.

Only Archive items with a verified CC BY or CC0 metadata URL are eligible.
Archive search and item APIs are documented, do not need private credentials.
A public Archive upload is not proof of accuracy of its license label;
the bot attributes the uploader/artist and links to the item's page.
"""
import json
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger("telegram_music")
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_LENGTH_SECONDS = 600
MAX_SEARCH_DOCS = 5
MAX_ITEM_LOOKUPS = 3
MAX_JSON_BYTES = 1024 * 1024


def archive_https_url(raw: str) -> bool:
    try:
        p = urlsplit(raw)
        host = (p.hostname or "").lower().rstrip(".")
        return (
            p.scheme == "https" and p.port in (None, 443)
            and not p.username and not p.password
            and (host == "archive.org" or host.endswith(".archive.org"))
        )
    except ValueError:
        return False


class ArchiveRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not archive_https_url(newurl):
            raise ValueError("Unexpected Internet Archive redirect host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def allowed_license(raw: str) -> str | None:
    try:
        p = urlsplit(raw.strip())
        if (p.scheme not in {"https", "http"} or
                (p.hostname or "").lower() != "creativecommons.org"):
            return None
        path = p.path.lower().rstrip("/")
        if path in {"/licenses/by/4.0", "/licenses/by/3.0"}:
            return "CC BY " + path.rsplit("/", 1)[-1]
        if path == "/publicdomain/zero/1.0":
            return "CC0 1.0"
        return None
    except (ValueError, AttributeError):
        return None


def parse_duration(raw) -> float | None:
    if raw is None:
        return None
    try:
        s = str(raw)
        if ":" in s:
            parts = s.split(":")
            if len(parts) not in (2, 3) or not all(x.isdigit() for x in parts):
                return None
            length = 0.0
            for value in parts:
                length = length * 60 + int(value)
        else:
            length = float(s)
        return length if 1 <= length <= MAX_LENGTH_SECONDS else None
    except (ValueError, TypeError):
        return None


def read_json(url: str, *, opener, timeout: int = 10) -> dict:
    if not archive_https_url(url):
        raise ValueError("Disallowed Archive API URL")
    req = Request(url, headers={
        "Accept": "application/json", "User-Agent": "MudyarMusicBot/1.0"
    })
    with opener.open(req, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Archive API returned HTTP error")
        if not archive_https_url(response.geturl()):
            raise ValueError("Archive API redirected outside permitted hosts")
        payload = response.read(MAX_JSON_BYTES + 1)
        if len(payload) > MAX_JSON_BYTES:
            raise ValueError("Archive API response too large")
        result = json.loads(payload)
    return result if isinstance(result, dict) else {}


def archive_search(query: str, *, opener) -> list[dict]:
    """Query audio metadata only; avoid user-provided URLs and query syntax."""
    terms = re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)[:9]
    if not terms or len(query) > 120:
        return []
    # Exact item-title phrase first; search isn't broad enough to substitute
    # unrelated artist covers for the user's requested song.
    search = ('mediatype:audio AND licenseurl:http* '
              'AND NOT licenseurl:http*nc* '
              'AND NOT licenseurl:http*nd* '
              'AND NOT licenseurl:http*sa* AND title:"') + (
        " ".join(terms).replace('"', "")
    ) + '"'
    url = "https://archive.org/advancedsearch.php?" + urlencode({
        "q": search, "rows": MAX_SEARCH_DOCS,
        "fl[]": ["identifier", "title", "creator"], "output": "json",
    }, doseq=True)
    response = read_json(url, opener=opener)
    items = response.get("response", {})
    if not isinstance(items, dict):
        return []
    docs = items.get("docs", [])
    return docs[:MAX_SEARCH_DOCS] if isinstance(docs, list) else []


def matches_recording(filename: str, query: str, item_title: str, *, check=None,
                      artist: str = "") -> bool:
    title = Path(filename).stem.replace("_", " ").replace("-", " ")
    query_terms = set(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE))
    file_terms = set(re.findall(r"[^\W_]+", title.casefold(), flags=re.UNICODE))
    if not query_terms or not (query_terms & file_terms):
        return False
    if len(query_terms) >= 2 and len(query_terms & (file_terms | set(
        re.findall(r"[^\W_]+", (item_title + " " + artist).casefold(),
                   flags=re.UNICODE)
    ))) < max(2, len(query_terms) - 1):
        return False
    if check is not None and not check(title, query, artist):
        return False
    return True


def get_archive_audio(
    query: str, *, cancel_signal: threading.Event | None = None, check=None,
    opener=None
) -> tuple[str, str, float] | None:
    """Return a licensed, downloadable MP3 file or None (never a YouTube track)."""
    if len(query) > 120 or not query.strip():
        return None
    opener = opener or build_opener(ArchiveRedirects())
    if cancel_signal is not None and cancel_signal.is_set():
        raise InterruptedError("Archive search cancelled")
    try:
        docs = archive_search(query, opener=opener)
    except Exception as exc:
        LOG.info("Archive search unavailable (%s)", type(exc).__name__)
        return None
    lookups = 0
    for item in docs:
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Archive search cancelled")
        if lookups >= MAX_ITEM_LOOKUPS:
            break
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("identifier") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,100}", identifier):
            continue
        lookups += 1
        try:
            metadata = read_json(
                "https://archive.org/metadata/" + quote(identifier, safe=""),
                opener=opener,
            )
        except Exception as exc:
            LOG.info("Archive metadata unavailable (%s)", type(exc).__name__)
            continue
        info = metadata.get("metadata")
        if not isinstance(info, dict):
            continue
        license_name = allowed_license(str(info.get("licenseurl") or ""))
        if not license_name or metadata.get("is_dark"):
            continue
        creator = info.get("creator") or ""
        if isinstance(creator, list):
            creator = ", ".join(str(x) for x in creator[:2])
        creator = str(creator)[:75]
        item_title = str(info.get("title") or "")[:90]
        files = metadata.get("files") or []
        if not isinstance(files, list):
            continue
        for entry in files[:75]:
            if cancel_signal is not None and cancel_signal.is_set():
                raise InterruptedError("Archive download cancelled")
            if not isinstance(entry, dict):
                continue
            filename = entry.get("name")
            if not isinstance(filename, str) or not filename.lower().endswith(".mp3"):
                continue
            if entry.get("source") not in ("original", "derivative"):
                continue
            if "/" in filename or "\\" in filename or filename.startswith("."):
                continue
            try:
                declared_size = int(entry.get("size") or 0)
            except (ValueError, TypeError):
                continue
            duration = parse_duration(entry.get("length"))
            if not (1024 <= declared_size <= MAX_AUDIO_BYTES and duration):
                continue
            if not matches_recording(
                filename, query, item_title, check=check, artist=creator
            ):
                continue
            url = "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(filename, safe="")
            directory = Path(tempfile.mkdtemp(prefix="mudyar_media_"))
            destination = directory / "archive.mp3"
            try:
                request = Request(url, headers={"User-Agent": "MudyarMusicBot/1.0"})
                with opener.open(request, timeout=12) as response:
                    if response.status != 200 or not archive_https_url(response.geturl()):
                        raise ValueError("Archive media request denied")
                    total = 0
                    with destination.open("wb") as output:
                        while True:
                            if cancel_signal is not None and cancel_signal.is_set():
                                raise InterruptedError("Archive stream cancelled")
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_AUDIO_BYTES:
                                raise ValueError("Archive media exceeded size cap")
                            output.write(chunk)
                if total < 1024:
                    raise ValueError("Archive returned empty audio")
                # Include uploader attribution and license in the track label.
                display = Path(filename).stem[:65]
                label = (
                    f"{display} — {creator[:35]} · Internet Archive "
                    f"({license_name}: https://archive.org/details/{identifier})"
                )
                LOG.info("Resolved Creative Commons Internet Archive audio (%s)", license_name)
                return str(destination), label, duration
            except InterruptedError:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            except Exception as exc:
                shutil.rmtree(directory, ignore_errors=True)
                LOG.info("Archive media unavailable (%s)", type(exc).__name__)
                continue
    return None
