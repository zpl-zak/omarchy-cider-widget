#!/usr/bin/env python3
"""Secret-safe, bounded adapter for Cider's localhost RPC API."""

from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import math
import os
import selectors
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_RPC_URL = "http://127.0.0.1:10767"
PLAYBACK_PATH = "/api/v1/playback"
REQUEST_TIMEOUT_SEC = 3.0
HELPER_DEADLINE_SEC = 8.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 4096
MAX_MANAGER_OUTPUT_BYTES = 256 * 1024
MAX_TOKEN_BYTES = 4096
MAX_QUEUE_ITEMS = 2000
MAX_AUDIO_TRAITS = 8
MAX_ARTWORK_URL_CHARS = 2048
MAX_ARTWORK_INPUT_BYTES = 1024 * 1024
MAX_ARTWORK_OUTPUT_BYTES = 512 * 1024
MAX_ARTWORK_DIMENSION = 4096
MAX_ARTWORK_PIXELS = 16_777_216
MAX_ARTWORK_CACHE_FILES = 128
ARTWORK_TIMEOUT_SEC = 2.0
ARTWORK_HOST_SUFFIXES = (".mzstatic.com",)
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
KEYRING_ATTRIBUTES = [
    "application",
    "omarchy-cider-widget",
    "credential",
    "api-key",
]

_command_deadline: float | None = None
_active_children: list[subprocess.Popen[bytes]] = []


@dataclass
class RpcFailure(Exception):
    code: str
    message: str
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message


@dataclass
class ProcessFailure(Exception):
    code: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return redirects as errors without constructing a second request."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to an already validated IP while keeping TLS SNI on the allowed host."""

    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def monotonic_deadline(seconds: float) -> float:
    deadline = time.monotonic() + seconds
    return min(deadline, _command_deadline) if _command_deadline is not None else deadline


def seconds_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RpcFailure("deadline_exceeded", "Cider helper exceeded its time limit", 4)
    return remaining


def bounded_message(value: Any, fallback: str) -> str:
    text = value if isinstance(value, str) else fallback
    text = " ".join(text.split())
    return text[:217] + "..." if len(text) > 220 else text


def bounded_string(value: Any, limit: int, fallback: str = "") -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        text = fallback
    text = text.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return text[:limit]


def bounded_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    if not math.isfinite(number):
        return fallback
    return min(maximum, max(minimum, number))


def validate_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        return ""
    if "\r" in token or "\n" in token or "\x00" in token:
        return ""
    return token


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the complete child session and reap its leader."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError):
        group_exists = False
    else:
        group_exists = True
    if group_exists:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def run_bounded_command(
    command: list[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int = MAX_STDERR_BYTES,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Stream capped child output with a monotonic deadline and group cleanup."""

    deadline = monotonic_deadline(timeout)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as error:
        raise ProcessFailure("unavailable") from error

    _active_children.append(process)
    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, (stdout, stdout_limit))
    selector.register(process.stderr, selectors.EVENT_READ, (stderr, stderr_limit))

    try:
        while selector.get_map():
            try:
                remaining = seconds_remaining(deadline)
            except RpcFailure as error:
                raise ProcessFailure("deadline_exceeded") from error
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                target, limit = key.data
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if len(target) + len(chunk) > limit:
                    raise ProcessFailure("output_too_large")
                target.extend(chunk)

        try:
            return_code = process.wait(timeout=seconds_remaining(deadline))
        except (subprocess.TimeoutExpired, RpcFailure) as error:
            raise ProcessFailure("deadline_exceeded") from error
        return subprocess.CompletedProcess(command, return_code, bytes(stdout), bytes(stderr))
    except BaseException:
        terminate_process_group(process)
        raise
    finally:
        selector.close()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
        if process in _active_children:
            _active_children.remove(process)


def stop_active_children(signum: int, _frame: Any) -> None:
    for process in list(_active_children):
        terminate_process_group(process)
    os._exit(128 + signum)


def enforce_hard_deadline(deadline: float, cancelled: threading.Event) -> None:
    """End the helper even if a library call stops returning progress."""

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for process in list(_active_children):
                terminate_process_group(process)
            os._exit(124)
        if cancelled.wait(remaining):
            return


def rpc_base_url() -> str:
    raw = os.environ.get("CIDER_RPC_URL", DEFAULT_RPC_URL).strip() or DEFAULT_RPC_URL
    if len(raw) > 256:
        raise RpcFailure("invalid_rpc_url", "CIDER_RPC_URL is too long", 2)
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in LOOPBACK_HOSTS:
        raise RpcFailure(
            "invalid_rpc_url",
            "CIDER_RPC_URL must use HTTP and a loopback host",
            2,
        )
    try:
        port = parsed.port
    except ValueError:
        raise RpcFailure("invalid_rpc_url", "CIDER_RPC_URL has an invalid port", 2) from None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RpcFailure("invalid_rpc_url", "CIDER_RPC_URL contains unsupported parts", 2)
    if parsed.path.rstrip("/"):
        raise RpcFailure("invalid_rpc_url", "CIDER_RPC_URL must not contain a path", 2)
    if host == "localhost":
        try:
            addresses = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise RpcFailure("invalid_rpc_url", "localhost did not resolve", 2) from None
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_loopback for item in addresses):
            raise RpcFailure("invalid_rpc_url", "localhost must resolve only to loopback addresses", 2)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"http://{netloc}"


def api_key() -> str:
    value = validate_token(os.environ.get("CIDER_API_KEY", ""))
    if not value:
        try:
            completed = run_bounded_command(
                ["systemctl", "--user", "show-environment"],
                timeout=2.0,
                stdout_limit=MAX_MANAGER_OUTPUT_BYTES,
            )
        except ProcessFailure:
            completed = None
        if completed and completed.returncode == 0:
            output = completed.stdout.decode("utf-8", "replace")
            for line in output.splitlines():
                if line.startswith("CIDER_API_KEY="):
                    value = validate_token(line.partition("=")[2])
                    break
    if not value:
        try:
            completed = run_bounded_command(
                ["secret-tool", "lookup", *KEYRING_ATTRIBUTES],
                timeout=2.0,
                stdout_limit=MAX_TOKEN_BYTES + 1,
            )
        except ProcessFailure:
            completed = None
        if completed and completed.returncode == 0:
            value = validate_token(completed.stdout.decode("utf-8", "replace"))
    if not value:
        raise RpcFailure("missing_api_key", "Cider API key is not configured", 3)
    return value


def read_response(response: Any, deadline: float, byte_limit: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > byte_limit:
                raise RpcFailure("response_too_large", "Cider RPC response exceeded 1 MiB")
        except (TypeError, ValueError):
            raise RpcFailure("invalid_response", "Cider RPC returned an invalid Content-Length") from None

    raw = bytearray()
    while True:
        remaining = seconds_remaining(deadline)
        socket_object = getattr(response, "fp", None)
        socket_object = getattr(getattr(socket_object, "raw", None), "_sock", None)
        if socket_object is not None:
            socket_object.settimeout(min(remaining, REQUEST_TIMEOUT_SEC))
        chunk = response.read1(min(64 * 1024, byte_limit + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > byte_limit:
            raise RpcFailure("response_too_large", "Cider RPC response exceeded 1 MiB")
        seconds_remaining(deadline)
    return bytes(raw)


def rpc_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    base_url: str | None = None,
) -> Any:
    token = key if key is not None else api_key()
    base = base_url if base_url is not None else rpc_base_url()
    deadline = monotonic_deadline(REQUEST_TIMEOUT_SEC)
    url = base + path
    payload = None
    headers = {"Accept": "application/json", "apptoken": token}
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())
    try:
        with opener.open(request, timeout=seconds_remaining(deadline)) as response:
            raw = read_response(response, deadline, MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise RpcFailure("redirect_rejected", "Cider RPC redirects are not allowed") from None
        if error.code in (401, 403):
            raise RpcFailure("unauthorized", "Cider rejected the app token", 3) from None
        raise RpcFailure("http_error", f"Cider RPC returned HTTP {error.code}") from None
    except RpcFailure:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RpcFailure("unavailable", "Cider RPC is not reachable on localhost:10767", 4) from None

    if not raw.strip():
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RpcFailure("invalid_response", "Cider RPC returned invalid JSON") from None


def make_requester() -> Callable[[str, str, dict[str, Any] | None], Any]:
    key = api_key()
    base_url = rpc_base_url()

    def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return rpc_request(method, path, body, key=key, base_url=base_url)

    return request


def artwork_cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(configured) if configured and os.path.isabs(configured) else Path.home() / ".cache"
    return base / "omarchy-cider-widget" / "artwork"


def allowed_artwork_url(value: Any, size: int) -> str:
    if not isinstance(value, str) or len(value) > MAX_ARTWORK_URL_CHARS:
        return ""
    candidate = value.replace("{w}", str(size)).replace("{h}", str(size))
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or port not in (None, 443):
        return ""
    if parsed.username or parsed.password or parsed.fragment or not parsed.path.startswith("/"):
        return ""
    if not any(host.endswith(suffix) and host != suffix[1:] for suffix in ARTWORK_HOST_SUFFIXES):
        return ""
    return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))


def public_addresses(host: str) -> list[str]:
    try:
        resolved = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addresses: list[str] = []
    for item in resolved:
        address = item[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except (TypeError, ValueError):
            return []
        if not parsed.is_global:
            return []
        normalized = str(parsed)
        if normalized not in addresses:
            addresses.append(normalized)
    return addresses


def download_artwork(url: str, deadline: float) -> tuple[bytes, str]:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    addresses = public_addresses(host)
    if not addresses:
        raise RpcFailure("invalid_artwork", "Artwork host did not resolve to public addresses")
    target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
    last_error: Exception | None = None

    for address in addresses:
        connection = PinnedHTTPSConnection(host, address, min(ARTWORK_TIMEOUT_SEC, seconds_remaining(deadline)))
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "image/jpeg,image/png",
                    "Connection": "close",
                    "User-Agent": "omarchy-cider-widget/1",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise RpcFailure("invalid_artwork", "Artwork redirects are not allowed")
            if response.status != 200:
                raise RpcFailure("invalid_artwork", "Artwork server returned an error")
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"image/jpeg", "image/png"}:
                raise RpcFailure("invalid_artwork", "Artwork type is not allowed")
            data = read_response(response, deadline, MAX_ARTWORK_INPUT_BYTES)
            return data, content_type
        except RpcFailure:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            last_error = error
        finally:
            connection.close()
    raise RpcFailure("invalid_artwork", "Artwork host is unavailable") from last_error


def image_dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    if content_type == "image/png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", data[16:24])
    if content_type != "image/jpeg" or len(data) < 4 or data[:2] != b"\xff\xd8":
        return None

    offset = 2
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if offset + 2 > len(data):
            return None
        segment_length = struct.unpack(">H", data[offset:offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(data):
            return None
        if marker in start_of_frame:
            if segment_length < 7:
                return None
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += segment_length
    return None


def valid_dimensions(dimensions: tuple[int, int] | None, maximum: int) -> bool:
    if not dimensions:
        return False
    width, height = dimensions
    return (
        0 < width <= maximum
        and 0 < height <= maximum
        and width * height <= MAX_ARTWORK_PIXELS
    )


def cached_png_is_safe(path: Path, maximum: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > MAX_ARTWORK_OUTPUT_BYTES:
            return False
        with path.open("rb") as source:
            header = source.read(24)
        return valid_dimensions(image_dimensions(header, "image/png"), maximum)
    except OSError:
        return False


def trim_artwork_cache(root: Path, keep: Path) -> None:
    try:
        entries = sorted(
            (entry for entry in root.glob("*.png") if entry != keep),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
        for entry in entries[MAX_ARTWORK_CACHE_FILES - 1:]:
            entry.unlink(missing_ok=True)
    except OSError:
        pass


def materialize_artwork(artwork: Any, size: int = 320, cache_root: Path | None = None) -> str:
    if not isinstance(artwork, dict):
        return ""
    requested_size = 128 if size <= 128 else 320
    url = allowed_artwork_url(artwork.get("url"), requested_size)
    if not url:
        return ""

    root = cache_root or artwork_cache_root()
    cache_key = hashlib.sha256(f"{requested_size}\0{url}".encode("utf-8")).hexdigest()
    target = root / f"{cache_key}.png"
    if cached_png_is_safe(target, requested_size):
        try:
            target.touch()
        except OSError:
            pass
        return str(target)

    deadline = monotonic_deadline(ARTWORK_TIMEOUT_SEC)
    input_path: Path | None = None
    output_path: Path | None = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        data, content_type = download_artwork(url, deadline)
        if not valid_dimensions(image_dimensions(data, content_type), MAX_ARTWORK_DIMENSION):
            return ""
        suffix = ".png" if content_type == "image/png" else ".jpg"
        input_fd, input_name = tempfile.mkstemp(prefix="input-", suffix=suffix, dir=root)
        input_path = Path(input_name)
        with os.fdopen(input_fd, "wb") as output:
            output.write(data)
        output_fd, output_name = tempfile.mkstemp(prefix="output-", suffix=".png", dir=root)
        os.close(output_fd)
        output_path = Path(output_name)

        magick = shutil.which("magick")
        if not magick:
            return ""
        child_environment = dict(os.environ)
        child_environment.pop("CIDER_API_KEY", None)
        completed = run_bounded_command(
            [
                magick,
                "-limit", "memory", "32MiB",
                "-limit", "map", "64MiB",
                "-limit", "disk", "0",
                "-limit", "thread", "1",
                "-limit", "time", "2",
                str(input_path),
                "-auto-orient",
                "-thumbnail", f"{requested_size}x{requested_size}>",
                "-strip",
                f"png:{output_path}",
            ],
            timeout=min(2.5, seconds_remaining(deadline)),
            stdout_limit=1024,
            stderr_limit=MAX_STDERR_BYTES,
            env=child_environment,
        )
        if completed.returncode != 0 or not cached_png_is_safe(output_path, requested_size):
            return ""
        os.chmod(output_path, 0o600)
        os.replace(output_path, target)
        output_path = None
        trim_artwork_cache(root, target)
        return str(target)
    except (OSError, ProcessFailure, RpcFailure):
        return ""
    finally:
        for path in (input_path, output_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def item_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    direct = item.get("id")
    if direct is not None:
        return bounded_string(direct, 256)
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    play_params = attributes.get("playParams")
    if isinstance(play_params, dict) and play_params.get("id") is not None:
        return bounded_string(play_params["id"], 256)
    return ""


def normalize_track(info: Any) -> dict[str, Any] | None:
    if not isinstance(info, dict) or not info:
        return None
    play_params = info.get("playParams") if isinstance(info.get("playParams"), dict) else {}
    duration = bounded_number(info.get("durationInMillis"), 0, 0, 86_400_000) / 1000.0
    position = bounded_number(info.get("currentPlaybackTime"), 0, 0, 86_400)
    remaining = bounded_number(info.get("remainingTime"), 0, 0, 86_400)
    if duration <= 0 and position + remaining > 0:
        duration = min(86_400, position + remaining)
    position = min(position, duration) if duration > 0 else position
    raw_traits = info.get("audioTraits")
    traits = []
    if isinstance(raw_traits, list):
        for value in raw_traits[:MAX_AUDIO_TRAITS]:
            trait = bounded_string(value, 32)
            if trait:
                traits.append(trait)
    return {
        "id": bounded_string(play_params.get("id") or info.get("id"), 256),
        "type": bounded_string(play_params.get("kind") or "song", 32, "song"),
        "title": bounded_string(info.get("name"), 512),
        "artist": bounded_string(info.get("artistName"), 512),
        "album": bounded_string(info.get("albumName"), 512),
        "artPath": materialize_artwork(info.get("artwork")),
        "durationSec": duration,
        "positionSec": position,
        "inLibrary": info.get("inLibrary") is True,
        "inFavorites": info.get("inFavorites") is True,
        "audioTraits": traits,
    }


def normalize_queue_item(item: Any, queue_index: int, skip_count: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    title = bounded_string(attributes.get("name"), 512)
    identifier = item_id(item)
    if not title and not identifier:
        return None
    return {
        "id": identifier,
        "type": bounded_string(item.get("type") or "song", 32, "song"),
        "queueIndex": min(MAX_QUEUE_ITEMS - 1, max(0, queue_index)),
        "skipCount": min(20, max(1, skip_count)),
        "title": title or "Unknown track",
        "artist": bounded_string(attributes.get("artistName"), 512),
        "album": bounded_string(attributes.get("albumName"), 512),
        "artPath": "",
        "durationSec": bounded_number(attributes.get("durationInMillis"), 0, 0, 86_400_000) / 1000.0,
    }


def status_payload(request: Callable[..., Any] | None = None) -> dict[str, Any]:
    request = request or rpc_request
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
        futures = {name: executor.submit(request, "GET", path) for name, path in endpoints.items()}
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
        "volume": bounded_number(volume.get("volume"), 0, 0, 1),
        "shuffleMode": round(bounded_number(shuffle.get("value"), bounded_number(info.get("shuffleMode"), 0, 0, 1), 0, 1)),
        "repeatMode": round(bounded_number(repeat.get("value"), bounded_number(info.get("repeatMode"), 0, 0, 2), 0, 2)),
        "autoplay": autoplay.get("value") is True,
        "fetchedAtMs": min(9_007_199_254_740_991, max(0, int(time.time() * 1000))),
    }


def queue_payload(limit: int, request: Callable[..., Any] | None = None) -> dict[str, Any]:
    request = request or rpc_request
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        now_future = executor.submit(request, "GET", f"{PLAYBACK_PATH}/now-playing")
        queue_future = executor.submit(request, "GET", f"{PLAYBACK_PATH}/queue")
        now = now_future.result()
        queue = queue_future.result()

    if not isinstance(queue, list) or len(queue) > MAX_QUEUE_ITEMS:
        raise RpcFailure("invalid_response", "Cider returned an invalid or oversized queue")
    info = now.get("info") if isinstance(now, dict) and isinstance(now.get("info"), dict) else {}
    play_params = info.get("playParams") if isinstance(info.get("playParams"), dict) else {}
    current_id = bounded_string(play_params.get("id") or info.get("id"), 256)

    current_index = -1
    for index, item in enumerate(queue):
        if current_id and item_id(item) == current_id:
            current_index = index

    start = current_index + 1 if current_index >= 0 else 0
    up_next = []
    artworks = []
    for index, item in enumerate(queue[start:], start=start):
        normalized = normalize_queue_item(item, index, index - current_index)
        if normalized:
            up_next.append(normalized)
            attributes = item.get("attributes") if isinstance(item, dict) else None
            artworks.append(attributes.get("artwork") if isinstance(attributes, dict) else None)
        if len(up_next) >= limit:
            break

    if up_next:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(up_next))) as executor:
            futures = [executor.submit(materialize_artwork, artwork, 128) for artwork in artworks]
            for index, future in enumerate(futures):
                try:
                    up_next[index]["artPath"] = future.result()
                except Exception:
                    up_next[index]["artPath"] = ""

    return {
        "currentId": current_id,
        "currentQueueIndex": current_index,
        "queueLength": len(queue),
        "upNext": up_next,
        "fetchedAtMs": min(9_007_199_254_740_991, max(0, int(time.time() * 1000))),
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


def integer_in_range(raw_value: str | None, label: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw_value or "")
    except ValueError:
        value = minimum - 1
    if value < minimum or value > maximum:
        raise RpcFailure("invalid_argument", f"{label} must be between {minimum} and {maximum}", 2)
    return value


def action_payload(
    name: str,
    raw_value: str | None,
    raw_second_value: str | None = None,
    request: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    request = request or rpc_request
    if name in ACTION_PATHS:
        suffix, body = ACTION_PATHS[name]
    elif name == "volume":
        try:
            value = float(raw_value or "")
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value < 0 or value > 1:
            raise RpcFailure("invalid_argument", "Volume must be between 0 and 1", 2)
        suffix, body = "/volume", {"volume": value}
    elif name == "seek":
        try:
            value = float(raw_value or "")
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value < 0 or value > 86_400:
            raise RpcFailure("invalid_argument", "Seek position must be a valid number of seconds", 2)
        suffix, body = "/seek", {"position": value}
    elif name == "queueMove":
        start_index = integer_in_range(raw_value, "Queue start index", 0, MAX_QUEUE_ITEMS - 1)
        destination_index = integer_in_range(raw_second_value, "Queue destination index", 0, MAX_QUEUE_ITEMS - 1)
        suffix, body = "/queue/move-to-position", {
            "startIndex": start_index,
            "destinationIndex": destination_index,
            "returnQueue": False,
        }
    elif name == "queueRemove":
        index = integer_in_range(raw_value, "Queue index", 0, MAX_QUEUE_ITEMS - 1)
        suffix, body = "/queue/remove-by-index", {"index": index}
    elif name == "skipTo":
        steps = integer_in_range(raw_value, "Skip count", 1, 20)
        for _ in range(steps):
            request("POST", PLAYBACK_PATH + "/next")
        return {"action": name, "steps": steps}
    else:
        raise RpcFailure("invalid_action", "Unsupported Cider action", 2)

    request("POST", PLAYBACK_PATH + suffix, body)
    return {"action": name}


def bounded_output_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return 0
        return min(9_007_199_254_740_991, max(-9_007_199_254_740_991, value))
    if isinstance(value, list):
        return [bounded_output_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {
            bounded_string(key, 64): bounded_output_value(item, depth + 1)
            for key, item in list(value.items())[:64]
            if isinstance(key, str)
        }
    return None


def emit(payload: dict[str, Any]) -> None:
    safe_payload = bounded_output_value(payload)
    encoded = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps({
            "ok": False,
            "error": {"code": "output_too_large", "message": "Cider helper output exceeded its limit"},
        }, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def parse_limit(raw: str | None) -> int:
    try:
        value = int(raw or "8")
    except ValueError:
        value = 8
    return min(20, max(1, value))


def main(argv: list[str]) -> int:
    global _command_deadline
    _command_deadline = time.monotonic() + HELPER_DEADLINE_SEC
    deadline_cancelled = threading.Event()
    threading.Thread(
        target=enforce_hard_deadline,
        args=(_command_deadline, deadline_cancelled),
        daemon=True,
    ).start()
    command = argv[1] if len(argv) > 1 else "status"
    try:
        if command == "status":
            data = status_payload(make_requester())
        elif command == "queue":
            data = queue_payload(parse_limit(argv[2] if len(argv) > 2 else None), make_requester())
        elif command == "action":
            if len(argv) < 3:
                raise RpcFailure("invalid_action", "Missing Cider action", 2)
            data = action_payload(
                argv[2],
                argv[3] if len(argv) > 3 else None,
                argv[4] if len(argv) > 4 else None,
                make_requester(),
            )
        else:
            raise RpcFailure("invalid_command", "Use status, queue, or action", 2)
    except RpcFailure as error:
        emit({"ok": False, "error": {"code": error.code, "message": bounded_message(error.message, "Cider request failed")}})
        return error.exit_code
    except Exception:
        emit({"ok": False, "error": {"code": "internal_error", "message": "Cider helper failed"}})
        return 1
    finally:
        deadline_cancelled.set()
        _command_deadline = None

    emit({"ok": True, "data": data})
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_active_children)
    signal.signal(signal.SIGINT, stop_active_children)
    raise SystemExit(main(sys.argv))
