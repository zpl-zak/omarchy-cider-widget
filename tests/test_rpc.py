#!/usr/bin/env python3

import importlib.util
import json
import os
import sys
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

    def test_normalizes_now_playing(self):
        track = RPC.normalize_track({
            "name": "Ego Brain",
            "artistName": "System Of A Down",
            "albumName": "Steal This Album!",
            "durationInMillis": 201907,
            "currentPlaybackTime": 42.5,
            "playParams": {"id": "123", "kind": "song"},
            "artwork": {"url": "https://example.test/{w}x{h}.jpg"},
            "audioTraits": ["lossless"],
        })
        self.assertEqual(track["id"], "123")
        self.assertEqual(track["title"], "Ego Brain")
        self.assertAlmostEqual(track["durationSec"], 201.907)
        self.assertEqual(track["artUrl"], "https://example.test/320x320.jpg")

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

        with mock.patch.object(RPC, "rpc_request", side_effect=fake_request):
            payload = RPC.queue_payload(1)

        self.assertEqual(payload["currentQueueIndex"], 1)
        self.assertEqual([item["id"] for item in payload["upNext"]], ["next-1"])
        self.assertEqual(payload["upNext"][0]["queueIndex"], 3)
        self.assertEqual(payload["upNext"][0]["skipCount"], 1)

    def test_action_allowlist_and_payloads(self):
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return {"status": "ok"}

        with mock.patch.object(RPC, "rpc_request", side_effect=fake_request):
            RPC.action_payload("volume", "0.65")
            RPC.action_payload("next", None)

        self.assertEqual(calls[0], ("POST", "/api/v1/playback/volume", {"volume": 0.65}))
        self.assertEqual(calls[1], ("POST", "/api/v1/playback/next", None))
        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("clearQueue", None)

    def test_queue_actions_use_documented_playback_endpoints(self):
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return {"status": "ok"}

        with mock.patch.object(RPC, "rpc_request", side_effect=fake_request):
            RPC.action_payload("queueMove", "4", "3")
            RPC.action_payload("queueRemove", "4")
            payload = RPC.action_payload("skipTo", "3")

        self.assertEqual(calls[0], (
            "POST",
            "/api/v1/playback/queue/move-to-position",
            {"startIndex": 4, "destinationIndex": 3, "returnQueue": False},
        ))
        self.assertEqual(calls[1], (
            "POST",
            "/api/v1/playback/queue/remove-by-index",
            {"index": 4},
        ))
        self.assertEqual(calls[2:], [
            ("POST", "/api/v1/playback/next", None),
            ("POST", "/api/v1/playback/next", None),
            ("POST", "/api/v1/playback/next", None),
        ])
        self.assertEqual(payload, {"action": "skipTo", "steps": 3})

        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("queueRemove", "0")
        with self.assertRaises(RPC.RpcFailure):
            RPC.action_payload("skipTo", "21")

    def test_missing_key_error_does_not_echo_secret_material(self):
        manager = mock.Mock(returncode=0, stdout="SOME_OTHER_VALUE=1\n")
        keyring = mock.Mock(returncode=1, stdout="")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC.subprocess, "run", side_effect=[manager, keyring]
        ):
            with self.assertRaises(RPC.RpcFailure) as context:
                RPC.api_key()
        encoded = json.dumps({"code": context.exception.code, "message": context.exception.message})
        self.assertEqual(context.exception.code, "missing_api_key")
        self.assertNotIn("apptoken", encoded.lower())

    def test_reads_key_from_user_manager_without_printing_it(self):
        manager = mock.Mock(returncode=0, stdout="OTHER=1\nCIDER_API_KEY=manager-secret\n")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC.subprocess, "run", return_value=manager
        ) as run_mock:
            self.assertEqual(RPC.api_key(), "manager-secret")
        run_mock.assert_called_once_with(
            ["systemctl", "--user", "show-environment"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def test_reads_key_from_login_keyring_after_environment_sources(self):
        manager = mock.Mock(returncode=0, stdout="OTHER=1\n")
        keyring = mock.Mock(returncode=0, stdout="keyring-secret\n")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            RPC.subprocess, "run", side_effect=[manager, keyring]
        ) as run_mock:
            self.assertEqual(RPC.api_key(), "keyring-secret")
        self.assertEqual(run_mock.call_args_list[1], mock.call(
            ["secret-tool", "lookup", *RPC.KEYRING_ATTRIBUTES],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ))


if __name__ == "__main__":
    unittest.main()
