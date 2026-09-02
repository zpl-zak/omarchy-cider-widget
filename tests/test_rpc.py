#!/usr/bin/env python3

import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cider_rpc", ROOT / "cider-rpc.py")
RPC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = RPC
SPEC.loader.exec_module(RPC)


class CiderRpcTests(unittest.TestCase):
    def test_rpc_url_is_limited_to_loopback(self):
        with mock.patch.dict(os.environ, {"CIDER_RPC_URL": "http://localhost:10767"}, clear=False):
            self.assertEqual(RPC.rpc_base_url(), "http://localhost:10767")

        with mock.patch.dict(os.environ, {"CIDER_RPC_URL": "https://example.com"}, clear=False):
            with self.assertRaises(RPC.RpcFailure) as context:
                RPC.rpc_base_url()
            self.assertEqual(context.exception.code, "invalid_rpc_url")

        with mock.patch.dict(os.environ, {"CIDER_RPC_URL": "http://localhost:10767"}, clear=False), mock.patch.object(
            RPC.socket,
            "getaddrinfo",
            return_value=[(RPC.socket.AF_INET, RPC.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 10767))],
        ):
            with self.assertRaises(RPC.RpcFailure):
                RPC.rpc_base_url()

    def test_rpc_redirect_is_rejected_without_following(self):
        target = "https://attacker.invalid/steal"

        class RedirectingOpener:
            def open(self, request, timeout):
                self.request = request
                raise RPC.urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": target}, None)

        opener = RedirectingOpener()
        with mock.patch.object(RPC.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(RPC.RpcFailure) as context:
                RPC.rpc_request("GET", "/api/v1/playback/now-playing", key="top-secret", base_url=RPC.DEFAULT_RPC_URL)
        self.assertEqual(context.exception.code, "redirect_rejected")
        self.assertEqual(opener.request.full_url, RPC.DEFAULT_RPC_URL + "/api/v1/playback/now-playing")
        self.assertNotEqual(opener.request.full_url, target)

        real_opener = RPC.urllib.request.build_opener(
            RPC.urllib.request.ProxyHandler({}),
            RPC.NoRedirectHandler(),
        )
        redirect_handlers = [
            handler for handler in real_opener.handlers
            if isinstance(handler, RPC.urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsInstance(redirect_handlers[0], RPC.NoRedirectHandler)

    def test_streaming_response_uses_total_monotonic_deadline(self):
        class Response:
            headers = {}

            def read1(self, _limit):
                return b"x"

        with mock.patch.object(RPC.time, "monotonic", side_effect=[0.0, 0.1, 0.6]):
            with self.assertRaises(RPC.RpcFailure) as context:
                RPC.read_response(Response(), 0.5, 100)
        self.assertEqual(context.exception.code, "deadline_exceeded")

    def test_bounded_subprocess_stops_on_excess_output(self):
        with self.assertRaises(RPC.ProcessFailure) as context:
            RPC.run_bounded_command(
                [sys.executable, "-c", "import os; os.write(1, b'x' * 8192)"],
                timeout=2,
                stdout_limit=64,
            )
        self.assertEqual(context.exception.code, "output_too_large")
        self.assertEqual(RPC._active_children, [])

    def test_normalizes_now_playing(self):
        with mock.patch.object(RPC, "materialize_artwork", return_value="/cache/safe.png"):
            track = RPC.normalize_track({
                "name": "Ego Brain",
                "artistName": "System Of A Down",
                "albumName": "Steal This Album!",
                "durationInMillis": 201907,
                "currentPlaybackTime": 42.5,
                "playParams": {"id": "123", "kind": "song"},
                "artwork": {"url": "https://is1-ssl.mzstatic.com/image/{w}x{h}.jpg"},
                "audioTraits": ["lossless"],
            })
        self.assertEqual(track["id"], "123")
        self.assertEqual(track["title"], "Ego Brain")
        self.assertAlmostEqual(track["durationSec"], 201.907)
        self.assertEqual(track["artPath"], "/cache/safe.png")

    def test_normalized_schema_caps_strings_arrays_and_numbers(self):
        with mock.patch.object(RPC, "materialize_artwork", return_value=""):
            track = RPC.normalize_track({
                "name": "x" * 1000,
                "durationInMillis": 999_999_999,
                "currentPlaybackTime": 999_999,
                "audioTraits": ["y" * 100] * 30,
            })
        self.assertEqual(len(track["title"]), 512)
        self.assertEqual(track["durationSec"], 86400)
        self.assertEqual(track["positionSec"], 86400)
        self.assertEqual(len(track["audioTraits"]), RPC.MAX_AUDIO_TRAITS)
        self.assertTrue(all(len(value) == 32 for value in track["audioTraits"]))

    def test_artwork_allowlist_rejects_credentials_private_hosts_and_bad_schemes(self):
        valid = RPC.allowed_artwork_url("https://is1-ssl.mzstatic.com/image/{w}x{h}.jpg", 320)
        self.assertEqual(valid, "https://is1-ssl.mzstatic.com/image/320x320.jpg")
        self.assertEqual(RPC.allowed_artwork_url("http://is1-ssl.mzstatic.com/image.jpg", 320), "")
        self.assertEqual(RPC.allowed_artwork_url("https://user@is1-ssl.mzstatic.com/image.jpg", 320), "")
        self.assertEqual(RPC.allowed_artwork_url("https://127.0.0.1/image.jpg", 320), "")
        self.assertEqual(RPC.allowed_artwork_url("https://mzstatic.com/image.jpg", 320), "")

        private = [(RPC.socket.AF_INET, RPC.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(RPC.socket, "getaddrinfo", return_value=private):
            self.assertEqual(RPC.public_addresses("is1-ssl.mzstatic.com"), [])

    def test_image_dimensions_reject_oversized_png(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (5000).to_bytes(4, "big") + (5000).to_bytes(4, "big")
        dimensions = RPC.image_dimensions(png, "image/png")
        self.assertEqual(dimensions, (5000, 5000))
        self.assertFalse(RPC.valid_dimensions(dimensions, RPC.MAX_ARTWORK_DIMENSION))

    @unittest.skipUnless(RPC.shutil.which("magick"), "ImageMagick is not installed")
    def test_artwork_is_materialized_as_bounded_local_png(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            RPC,
            "download_artwork",
            return_value=(png, "image/png"),
        ):
            path = RPC.materialize_artwork(
                {"url": "https://is1-ssl.mzstatic.com/image/{w}x{h}.png"},
                cache_root=Path(temporary),
            )
            self.assertTrue(path.endswith(".png"))
            self.assertNotIn("mzstatic.com", path)
            self.assertTrue(RPC.cached_png_is_safe(Path(path), 320))

    def test_serialized_output_has_a_hard_byte_cap(self):
        stream = io.BytesIO()
        stdout = mock.Mock(buffer=stream)
        payload = {"rows": ["x" * 10_000] * 20}
        with mock.patch.object(RPC.sys, "stdout", stdout):
            RPC.emit(payload)
        encoded = stream.getvalue()
        self.assertLessEqual(len(encoded), RPC.MAX_OUTPUT_BYTES + 1)
        self.assertEqual(json.loads(encoded)["error"]["code"], "output_too_large")

    def test_queue_starts_after_current_track(self):
        now = {"info": {"name": "Current", "playParams": {"id": "current"}}}
        queue = [
            {"id": "history", "attributes": {"name": "History"}},
            {"id": "current", "attributes": {"name": "Current"}},
            {"id": "next-1", "index": 3, "attributes": {"name": "Next One", "artistName": "Artist"}},
            {"id": "next-2", "index": 4, "attributes": {"name": "Next Two"}},
        ]

        def fake_request(method, path, body=None):
            return queue if path.endswith("/queue") else now

        with mock.patch.object(RPC, "materialize_artwork", return_value=""):
            payload = RPC.queue_payload(1, fake_request)

        self.assertEqual(payload["currentQueueIndex"], 1)
        self.assertEqual([item["id"] for item in payload["upNext"]], ["next-1"])
        self.assertEqual(payload["upNext"][0]["queueIndex"], 2)
        self.assertEqual(payload["upNext"][0]["skipCount"], 1)

    def test_queue_cardinality_is_bounded(self):
        def fake_request(_method, path, _body=None):
            return [{}] * (RPC.MAX_QUEUE_ITEMS + 1) if path.endswith("/queue") else {"info": {}}

        with self.assertRaises(RPC.RpcFailure) as context:
            RPC.queue_payload(20, fake_request)
        self.assertEqual(context.exception.code, "invalid_response")

    def test_action_allowlist_and_payloads(self):
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return {"status": "ok"}

        RPC.action_payload("volume", "0.65", request=fake_request)
        RPC.action_payload("next", None, request=fake_request)

        self.assertEqual(calls[0], ("POST", "/api/v1/playback/volume", {"volume": 0.65}))
        self.assertEqual(calls[1], ("POST", "/api/v1/playback/next", None))
        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("clearQueue", None)
        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("volume", "1.5", request=fake_request)

    def test_queue_actions_use_zero_based_playback_indices(self):
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return {"status": "ok"}

        RPC.action_payload("queueMove", "4", "3", fake_request)
        RPC.action_payload("queueRemove", "0", request=fake_request)
        payload = RPC.action_payload("skipTo", "3", request=fake_request)

        self.assertEqual(calls[0], (
            "POST",
            "/api/v1/playback/queue/move-to-position",
            {"startIndex": 4, "destinationIndex": 3, "returnQueue": False},
        ))
        self.assertEqual(calls[1], (
            "POST",
            "/api/v1/playback/queue/remove-by-index",
            {"index": 0},
        ))
        self.assertEqual(calls[2:], [
            ("POST", "/api/v1/playback/next", None),
            ("POST", "/api/v1/playback/next", None),
            ("POST", "/api/v1/playback/next", None),
        ])
        self.assertEqual(payload, {"action": "skipTo", "steps": 3})

        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("queueRemove", "-1")
        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("skipTo", "21")

    def test_missing_key_error_does_not_echo_secret_material(self):
        manager = subprocess.CompletedProcess([], 0, b"SOME_OTHER_VALUE=1\n", b"")
        keyring = subprocess.CompletedProcess([], 1, b"", b"")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC, "run_bounded_command", side_effect=[manager, keyring]
        ):
            with self.assertRaises(RPC.RpcFailure) as context:
                RPC.api_key()
        encoded = json.dumps({"code": context.exception.code, "message": context.exception.message})
        self.assertEqual(context.exception.code, "missing_api_key")
        self.assertNotIn("apptoken", encoded.lower())

    def test_reads_key_from_user_manager_without_printing_it(self):
        manager = subprocess.CompletedProcess([], 0, b"OTHER=1\nCIDER_API_KEY=manager-secret\n", b"")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC, "run_bounded_command", return_value=manager
        ) as run_mock:
            self.assertEqual(RPC.api_key(), "manager-secret")
        run_mock.assert_called_once_with(
            ["systemctl", "--user", "show-environment"],
            timeout=2.0,
            stdout_limit=RPC.MAX_MANAGER_OUTPUT_BYTES,
        )

    def test_reads_key_from_login_keyring_after_environment_sources(self):
        manager = subprocess.CompletedProcess([], 0, b"OTHER=1\n", b"")
        keyring = subprocess.CompletedProcess([], 0, b"keyring-secret\n", b"")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC, "run_bounded_command", side_effect=[manager, keyring]
        ) as run_mock:
            self.assertEqual(RPC.api_key(), "keyring-secret")
        self.assertEqual(run_mock.call_args_list[1], mock.call(
            ["secret-tool", "lookup", *RPC.KEYRING_ATTRIBUTES],
            timeout=2.0,
            stdout_limit=RPC.MAX_TOKEN_BYTES + 1,
        ))


if __name__ == "__main__":
    unittest.main()
