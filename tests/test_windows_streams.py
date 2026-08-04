#!/usr/bin/env python3
"""Cross-platform transport and UTF-8 regression tests."""

import io
import os
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

import server
from zcode_protocol import ZCodeProtocolClient, resolve_runtime_model


class ProtocolTransportTest(unittest.TestCase):
    @mock.patch("zcode_protocol.subprocess.Popen")
    def test_app_server_uses_utf8_and_removes_inherited_model_override(self, popen):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = io.StringIO()
        proc.stdout = []
        proc.stderr = []
        popen.return_value = proc

        with mock.patch.dict(os.environ, {"ZCODE_MODEL": "wrong/model"}):
            client = ZCodeProtocolClient("ZCode.exe", "zcode.cjs")
            client.start()
            client.close()

        kwargs = popen.call_args.kwargs
        self.assertEqual("utf-8", kwargs["encoding"])
        self.assertEqual("replace", kwargs["errors"])
        self.assertNotIn("ZCODE_MODEL", kwargs["env"])
        self.assertEqual("1", kwargs["env"]["ELECTRON_RUN_AS_NODE"])
        if os.name != "nt":
            self.assertTrue(kwargs["start_new_session"])

    def test_new_start_schema_exposes_explicit_native_model_ref(self):
        model = server.ZCODE_START_SCHEMA["properties"]["model"]
        self.assertEqual(["providerId", "modelId"], model["required"])
        self.assertFalse(model["additionalProperties"])
        alternatives = server.ZCODE_START_SCHEMA["oneOf"]
        self.assertEqual(["prompt"], alternatives[0]["required"])
        self.assertEqual(["goal"], alternatives[1]["required"])
        goal_modes = server.ZCODE_START_SCHEMA["allOf"][0]["then"]["properties"]["mode"]["enum"]
        self.assertNotIn("plan", goal_modes)

    def test_cold_resume_runtime_model_uses_local_secret_and_reasoning_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            database_path = os.path.join(directory, "db.sqlite")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "model": {"main": "deepseek-1/deepseek-v4-flash"},
                    "provider": {"deepseek-1": {
                        "name": "DeepSeek", "kind": "openai-compatible",
                        "options": {"apiKey": "secret", "baseURL": "https://example.invalid"},
                        "models": {"deepseek-v4-flash": {
                            "reasoning": {"enabled": True, "variants": ["high", "max"], "defaultVariant": "high"},
                            "limit": {"context": 1000000, "output": 384000},
                        }},
                    }},
                }, handle)
            connection = sqlite3.connect(database_path)
            connection.execute("create table local_setting (key text primary key, value text)")
            connection.execute("insert into local_setting values ('reasoningLevel', ?)", ('{"level":"max"}',))
            connection.commit()
            connection.close()

            runtime = resolve_runtime_model(
                config_path=config_path, settings_db_path=database_path
            )

        self.assertEqual({"providerId": "deepseek-1", "modelId": "deepseek-v4-flash"}, runtime["model"])
        self.assertEqual("max", runtime["thoughtLevel"])
        self.assertEqual({"source": "inline", "value": "secret"}, runtime["provider"]["apiKey"])
        self.assertEqual(1000000, runtime["provider"]["models"][0]["contextWindow"])

    def test_log_handles_surrogate_without_default_code_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bridge.log")
            with mock.patch.object(server, "STDERR_LOG", path):
                server._log("invalid surrogate: \udca6")
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        self.assertIn(r"\udca6", content)


if __name__ == "__main__":
    unittest.main()
