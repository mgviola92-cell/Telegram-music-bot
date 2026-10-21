"""Optional authenticated NubCoders audio URL resolver.

The API token is obtained only from environment variables and never logged.
This module does not download audio and is inert unless explicitly called.
"""
import asyncio
import ipaddress
import json
import os
import socket
import urllib.request
from urllib.parse import urlencode, urlsplit

_API_URL = "https://api.nubcoders.com/stream"
_MAX_QUERY = 160


class NubCodersUnavailable(RuntimeError):
    pass


def usable_https_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not host):
            return False
        lowered = host.lower().rstrip(".")
        if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            return "." in lowered and not lowered.startswith(".")
        return address.is_global
    except ValueError:
        return False


def resolve_sync(query, *, token=None, opener=None):
    key = token if token is not None else os.getenv("NUBCODERS_API_TOKEN", "")
    if not key:
        raise NubCodersUnavailable("NubCoders token not configured")
    if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY:
        raise NubCodersUnavailable("Invalid song query")
    url = _API_URL + "?" + urlencode({"q": query.strip(), "mode": "audio", "redirect": "false"})
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "MudyarMusicBot/1.0",
    })
    transport = opener or urllib.request.build_opener()
    try:
        with transport.open(request, timeout=8) as response:
            if response.status != 200 or "json" not in response.headers.get("Content-Type", "").lower():
                raise NubCodersUnavailable("Non-JSON NubCoders response")
            payload = response.read(32 * 1024 + 1)
            if len(payload) > 32 * 1024:
                raise NubCodersUnavailable("Oversized API response")
        body = json.loads(payload)
        address = body.get("stream_url") or body.get("url") if isinstance(body, dict) else None
        if not usable_https_url(address):
            raise NubCodersUnavailable("No usable HTTPS audio URL")
        return address
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        raise NubCodersUnavailable(type(error).__name__) from None


async def resolve(query, *, token=None, opener=None):
    return await asyncio.to_thread(resolve_sync, query, token=token, opener=opener)
