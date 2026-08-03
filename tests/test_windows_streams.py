#!/usr/bin/env python3
"""Regression tests for Windows Electron subprocess output handling."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server  # noqa: E402


class WindowsStreamNormalizationTest(unittest.TestCase):
    def _process(self, stdout, stderr, returncode=0):
        proc = mock.Mock()
        proc.communicate.return_value = (stdout, stderr)
        proc.returncode = returncode
        return proc

    @mock.patch("server.subprocess.Popen")
    def test_none_stderr_does_not_break_successful_result(self, popen):
        expected = {
            "response": "MCP_ZCODE_OK",
            "sessionId": "sess_windows_stream_test",
        }
        popen.return_value = self._process(json.dumps(expected), None)

        actual = server.run_zcode(
            "test",
            zcode_bin="ZCode.exe",
            zcode_bundle="zcode.cjs",
        )

        self.assertEqual(actual, expected)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")

    @mock.patch("server.subprocess.Popen")
    def test_none_stdout_becomes_structured_parse_error(self, popen):
        popen.return_value = self._process(None, "")

        with self.assertRaises(server.SessionError) as raised:
            server.run_zcode(
                "test",
                zcode_bin="ZCode.exe",
                zcode_bundle="zcode.cjs",
            )

        self.assertEqual(raised.exception.code, "parse_error")

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
