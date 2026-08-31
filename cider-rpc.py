#!/usr/bin/env python3
"""Small, secret-safe adapter for Cider's localhost RPC API."""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_RPC_URL = "http://127.0.0.1:10767"
PLAYBACK_PATH = "/api/v1/playback"
REQUEST_TIMEOUT_SEC = 3.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
KEYRING_ATTRIBUTES = [
    "application",
    "omarchy-cider-widget",
    "credential",
    "api-key",
]


@dataclass
class RpcFailure(Exception):
    code: str
    message: str
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message


def rpc_base_url() -> str:
    raw = os.environ.get("CIDER_RPC_URL", DEFAULT_RPC_URL).strip() or DEFAULT_RPC_URL
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise RpcFailure(
            "invalid_rpc_url",
            "CIDER_RPC_URL must use HTTP and a loopback host",
            2,
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RpcFailure("invalid_rpc_url", "CIDER_RPC_URL contains unsupported parts", 2)
    path = parsed.path.rstrip("/")
    if path:
        raise RpcFailure("invalid_rpc_url", "CIDER_RPC_URL must not contain a path", 2)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def api_key() -> str:
    value = os.environ.get("CIDER_API_KEY", "").strip()
    if not value:
        try:
            completed = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed and completed.returncode == 0:
            for line in completed.stdout.splitlines():
                if line.startswith("CIDER_API_KEY="):
                    value = line.partition("=")[2].strip()
                    break
    if not value:
        try:
            completed = subprocess.run(
                ["secret-tool", "lookup", *KEYRING_ATTRIBUTES],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed and completed.returncode == 0:
            value = completed.stdout.strip()
    if not value:
        raise RpcFailure(
            "missing_api_key",
            "Cider API key is not configured",
            3,
        )
    return value


def bounded_message(value: Any, fallback: str) -> str:
    text = " ".join(str(value or fallback).split())
    return text[:217] + "…" if len(text) > 220 else text


def rpc_request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    key = api_key()
    url = rpc_base_url() + path
    payload = None
    headers = {"Accept": "application/json", "apptoken": key}
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RpcFailure("unauthorized", "Cider rejected the app token", 3) from None
        raise RpcFailure("http_error", f"Cider RPC returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RpcFailure("unavailable", "Cider RPC is not reachable on localhost:10767", 4) from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise RpcFailure("response_too_large", "Cider RPC response exceeded 4 MiB")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RpcFailure("invalid_response", "Cider RPC returned invalid JSON") from None


def as_number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def artwork_url(artwork: Any, size: int = 320) -> str:
    if not isinstance(artwork, dict):
        return ""
    value = artwork.get("url")
    if not isinstance(value, str):
        return ""
    return value.replace("{w}", str(size)).replace("{h}", str(size))


def item_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    direct = item.get("id")
    if direct is not None:
        return str(direct)
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    play_params = attributes.get("playParams")
    if isinstance(play_params, dict) and play_params.get("id") is not None:
        return str(play_params["id"])
    return ""


def normalize_track(info: Any) -> dict[str, Any] | None:
    if not isinstance(info, dict) or not info:
        return None
    play_params = info.get("playParams") if isinstance(info.get("playParams"), dict) else {}
    duration = as_number(info.get("durationInMillis")) / 1000.0
    position = as_number(info.get("currentPlaybackTime"))
    remaining = as_number(info.get("remainingTime"))
    if duration <= 0 and position + remaining > 0:
        duration = position + remaining
    traits = info.get("audioTraits")
    return {
        "id": str(play_params.get("id") or info.get("id") or ""),
        "type": str(play_params.get("kind") or "song"),
        "title": str(info.get("name") or ""),
        "artist": str(info.get("artistName") or ""),
        "album": str(info.get("albumName") or ""),
        "artUrl": artwork_url(info.get("artwork")),
        "url": str(info.get("url") or ""),
        "durationSec": max(0.0, duration),
        "positionSec": max(0.0, position),
        "inLibrary": info.get("inLibrary") is True,
        "inFavorites": info.get("inFavorites") is True,
        "audioTraits": [str(value) for value in traits] if isinstance(traits, list) else [],
    }


def normalize_queue_item(item: Any, queue_index: int, skip_count: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    title = str(attributes.get("name") or "")
    identifier = item_id(item)
    if not title and not identifier:
        return None
    return {
        "id": identifier,
        "type": str(item.get("type") or "song"),
        "queueIndex": queue_index,
        "skipCount": skip_count,
        "title": title or "Unknown track",
        "artist": str(attributes.get("artistName") or ""),
        "album": str(attributes.get("albumName") or ""),
        "artUrl": artwork_url(attributes.get("artwork"), 128),
        "durationSec": max(0.0, as_number(attributes.get("durationInMillis")) / 1000.0),
    }


def status_payload() -> dict[str, Any]:
    endpoints = {
        "now": f"{PLAYBACK_PATH}/now-playing",
        "playing": f"{PLAYBACK_PATH}/is-playing",
        "volume": f"{PLAYBACK_PATH}/volume",
        "shuffle": f"{PLAYBACK_PATH}/shuffle-mode",
        "repeat": f"{PLAYBACK_PATH}/repeat-mode",
        "autoplay": f"{PLAYBACK_PATH}/autoplay",
    }
    results: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        futures = {name: executor.submit(rpc_request, "GET", path) for name, path in endpoints.items()}
        for name, future in futures.items():
            results[name] = future.result()

    now = results["now"] if isinstance(results["now"], dict) else {}
    playing = results["playing"] if isinstance(results["playing"], dict) else {}
    volume = results["volume"] if isinstance(results["volume"], dict) else {}
    shuffle = results["shuffle"] if isinstance(results["shuffle"], dict) else {}
    repeat = results["repeat"] if isinstance(results["repeat"], dict) else {}
    autoplay = results["autoplay"] if isinstance(results["autoplay"], dict) else {}
    info = now.get("info") if isinstance(now.get("info"), dict) else {}

    return {
        "connected": True,
        "playing": playing.get("is_playing") is True,
        "track": normalize_track(info),
        "volume": min(1.0, max(0.0, as_number(volume.get("volume"), 0.0))),
        "shuffleMode": int(as_number(shuffle.get("value"), info.get("shuffleMode", 0))),
        "repeatMode": int(as_number(repeat.get("value"), info.get("repeatMode", 0))),
        "autoplay": autoplay.get("value") is True,
        "fetchedAtMs": int(time.time() * 1000),
    }


def queue_payload(limit: int) -> dict[str, Any]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        now_future = executor.submit(rpc_request, "GET", f"{PLAYBACK_PATH}/now-playing")
        queue_future = executor.submit(rpc_request, "GET", f"{PLAYBACK_PATH}/queue")
        now = now_future.result()
        queue = queue_future.result()

    if not isinstance(queue, list):
        raise RpcFailure("invalid_response", "Cider returned an invalid queue")
    info = now.get("info") if isinstance(now, dict) and isinstance(now.get("info"), dict) else {}
    current = normalize_track(info)
    current_id = str(current.get("id") or "") if current else ""

    current_index = -1
    for index, item in enumerate(queue):
        if current_id and item_id(item) == current_id:
            current_index = index

    start = current_index + 1 if current_index >= 0 else 0
    up_next = []
    for index, item in enumerate(queue[start:], start=start):
        normalized = normalize_queue_item(item, index, index - current_index)
        if normalized:
            up_next.append(normalized)
        if len(up_next) >= limit:
            break

    return {
        "currentId": current_id,
        "currentQueueIndex": current_index,
        "queueLength": len(queue),
        "upNext": up_next,
        "fetchedAtMs": int(time.time() * 1000),
    }


ACTION_PATHS = {
    "play": ("/play", None),
    "pause": ("/pause", None),
    "playPause": ("/playpause", None),
    "next": ("/next", None),
    "previous": ("/previous", None),
    "toggleShuffle": ("/toggle-shuffle", None),
    "toggleRepeat": ("/toggle-repeat", None),
    "toggleAutoplay": ("/toggle-autoplay", None),
}


def integer_in_range(
    raw_value: str | None,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(raw_value or "")
    except ValueError:
        value = minimum - 1
    if value < minimum or value > maximum:
        raise RpcFailure(
            "invalid_argument",
            f"{label} must be between {minimum} and {maximum}",
            2,
        )
    return value


def action_payload(
    name: str,
    raw_value: str | None,
    raw_second_value: str | None = None,
) -> dict[str, Any]:
    if name in ACTION_PATHS:
        suffix, body = ACTION_PATHS[name]
    elif name == "volume":
        value = as_number(raw_value, math.nan)
        if not math.isfinite(value) or value < 0 or value > 1:
            raise RpcFailure("invalid_argument", "Volume must be between 0 and 1", 2)
        suffix, body = "/volume", {"volume": value}
    elif name == "seek":
        value = as_number(raw_value, math.nan)
        if not math.isfinite(value) or value < 0 or value > 86400:
            raise RpcFailure("invalid_argument", "Seek position must be a valid number of seconds", 2)
        suffix, body = "/seek", {"position": value}
    elif name == "queueMove":
        start_index = integer_in_range(raw_value, "Queue start index", 0, 100_000)
        destination_index = integer_in_range(raw_second_value, "Queue destination index", 0, 100_000)
        suffix, body = "/queue/move-to-position", {
            "startIndex": start_index,
            "destinationIndex": destination_index,
            "returnQueue": False,
        }
    elif name == "queueRemove":
        index = integer_in_range(raw_value, "Queue index", 0, 100_000)
        suffix, body = "/queue/remove-by-index", {"index": index}
    elif name == "skipTo":
        steps = integer_in_range(raw_value, "Skip count", 1, 20)
        for _ in range(steps):
            rpc_request("POST", PLAYBACK_PATH + "/next")
        return {"action": name, "steps": steps}
    else:
        raise RpcFailure("invalid_action", "Unsupported Cider action", 2)

    rpc_request("POST", PLAYBACK_PATH + suffix, body)
    return {"action": name}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def parse_limit(raw: str | None) -> int:
    try:
        value = int(raw or "8")
    except ValueError:
        value = 8
    return min(20, max(1, value))


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    try:
        if command == "status":
            data = status_payload()
        elif command == "queue":
            data = queue_payload(parse_limit(argv[2] if len(argv) > 2 else None))
        elif command == "action":
            if len(argv) < 3:
                raise RpcFailure("invalid_action", "Missing Cider action", 2)
            data = action_payload(
                argv[2],
                argv[3] if len(argv) > 3 else None,
                argv[4] if len(argv) > 4 else None,
            )
        else:
            raise RpcFailure("invalid_command", "Use status, queue, or action", 2)
    except RpcFailure as error:
        emit({"ok": False, "error": {"code": error.code, "message": bounded_message(error.message, "Cider request failed")}})
        return error.exit_code
    except Exception:
        emit({"ok": False, "error": {"code": "internal_error", "message": "Cider helper failed"}})
        return 1

    emit({"ok": True, "data": data})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
