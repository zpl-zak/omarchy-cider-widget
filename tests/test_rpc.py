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
    def test_python_startup_ignores_hostile_path_and_python_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "executed"
            hook = f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
            (root / "sitecustomize.py").write_text(hook)
            (root / "usercustomize.py").write_text(hook)
            (root / "json.py").write_text(hook + "raise RuntimeError('shadowed json')\n")
            fake = root / "python3"
            fake.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 99\n")
            fake.chmod(0o700)
            result = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", str(ROOT / "cider-rpc.py"), "invalid"],
                env={"PATH": temporary, "PYTHONPATH": temporary, "PYTHONHOME": temporary,
                     "PYTHONUSERBASE": temporary, "PYTHONSTARTUP": str(root / "sitecustomize.py")},
                cwd=temporary, capture_output=True, timeout=3,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_command")
            self.assertFalse(marker.exists())

    def test_credential_children_use_absolute_paths_and_session_only_environment(self):
        hostile = {
            "PATH": "/attacker", "PYTHONPATH": "/attacker", "LD_PRELOAD": "/attacker.so",
            "GIO_EXTRA_MODULES": "/attacker", "SYSTEMD_PAGER": "/attacker/pager",
            "UNRELATED_SECRET": "do-not-forward", "HOME": "/attacker",
            "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        }
        expected = {"PATH": "/usr/bin", "LC_ALL": "C", "XDG_RUNTIME_DIR": "/run/user/1000",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
        responses = [subprocess.CompletedProcess([], 0, b"OTHER=1\n", b""),
                     subprocess.CompletedProcess([], 0, b"test-token\n", b"")]
        with mock.patch.dict(os.environ, hostile, clear=True), mock.patch.object(
            RPC, "run_bounded_command", side_effect=responses
        ) as run:
            self.assertEqual(RPC.api_key(), "test-token")
        self.assertEqual([call.args[0][0] for call in run.call_args_list],
                         ["/usr/bin/systemctl", "/usr/bin/secret-tool"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], expected)

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
                env={},
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
                "flavor": "256",
            })
        self.assertEqual(track["id"], "123")
        self.assertEqual(track["title"], "Ego Brain")
        self.assertEqual(track["flavor"], "256")
        self.assertAlmostEqual(track["durationSec"], 201.907)
        self.assertEqual(track["artPath"], "/cache/safe.png")

    def test_playback_flavor_is_bounded_and_never_inferred_from_catalog(self):
        for flavor, expected in [(None, ""), ({}, ""), ("x" * 100, "x" * 32)]:
            track = RPC.normalize_track({"audioTraits": ["lossless"], "flavor": flavor})
            self.assertEqual(track["flavor"], expected)
        self.assertEqual(RPC.normalize_track({"audioTraits": ["lossless"]})["flavor"], "")

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

    @unittest.skipUnless(os.access(RPC.IMAGEMAGICK, os.X_OK), "ImageMagick is not installed")
    def test_artwork_is_materialized_as_bounded_local_png(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            RPC,
            "download_artwork",
            return_value=(png, "image/png"),
        ), mock.patch.dict(os.environ, {
            "PATH": temporary, "CIDER_API_KEY": "test-secret", "UNRELATED_SECRET": "another-secret",
            "LD_PRELOAD": "/attacker.so", "MAGICK_CONFIGURE_PATH": temporary,
            "MAGICK_CODER_MODULE_PATH": temporary, "HOME": temporary, "XDG_CONFIG_HOME": temporary,
        }), mock.patch.object(RPC, "run_bounded_command", wraps=RPC.run_bounded_command) as run:
            fake_magick = Path(temporary) / "magick"
            fake_magick.write_text("#!/bin/sh\nexit 99\n")
            fake_magick.chmod(0o700)
            path = RPC.materialize_artwork(
                {"url": "https://is1-ssl.mzstatic.com/image/{w}x{h}.png"},
                cache_root=Path(temporary),
            )
            self.assertTrue(path.endswith(".png"))
            self.assertNotIn("mzstatic.com", path)
            self.assertTrue(RPC.cached_png_is_safe(Path(path), 320))
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/magick")
            self.assertEqual(run.call_args.kwargs["env"], {
                "PATH": "/usr/bin", "LC_ALL": "C", "HOME": "/nonexistent", "XDG_CONFIG_HOME": "/nonexistent",
            })

    def test_missing_approved_magick_does_not_fall_back_to_path(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "executed"
            fake = Path(temporary) / "magick"
            fake.write_text(f"#!/bin/sh\n: > '{marker}'\n")
            fake.chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": temporary}), mock.patch.object(
                RPC, "IMAGEMAGICK", str(Path(temporary) / "missing")
            ), mock.patch.object(RPC, "download_artwork", return_value=(png, "image/png")):
                self.assertEqual(RPC.materialize_artwork(
                    {"url": "https://is1-ssl.mzstatic.com/image.png"}, cache_root=Path(temporary)
                ), "")
            self.assertFalse(marker.exists())

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
            ["/usr/bin/systemctl", "--user", "show-environment"],
            timeout=2.0,
            stdout_limit=RPC.MAX_MANAGER_OUTPUT_BYTES,
            env={"PATH": "/usr/bin", "LC_ALL": "C"},
        )

    def test_reads_key_from_login_keyring_after_environment_sources(self):
        manager = subprocess.CompletedProcess([], 0, b"OTHER=1\n", b"")
        keyring = subprocess.CompletedProcess([], 0, b"keyring-secret\n", b"")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC, "run_bounded_command", side_effect=[manager, keyring]
        ) as run_mock:
            self.assertEqual(RPC.api_key(), "keyring-secret")
        self.assertEqual(run_mock.call_args_list[1], mock.call(
            ["/usr/bin/secret-tool", "lookup", *RPC.KEYRING_ATTRIBUTES],
            timeout=2.0,
            stdout_limit=RPC.MAX_TOKEN_BYTES + 1,
            env={"PATH": "/usr/bin", "LC_ALL": "C"},
        ))


if __name__ == "__main__":
    unittest.main()
