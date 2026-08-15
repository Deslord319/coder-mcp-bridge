from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from model_deployments import ModelDeploymentManager
from resource_leases import ResourceLeaseStore


class Result:
    returncode = 0
    stdout = ""
    stderr = ""


class Response:
    status = 200

    def close(self):
        pass


class ModelDeploymentManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "deployments.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({
                "deployments": {
                    "local/model": {
                        "start": ["modelctl", "start"],
                        "stop": ["modelctl", "stop"],
                        "healthUrl": "http://127.0.0.1:9999/health",
                        "startupTimeoutSeconds": 2,
                        "idleTimeoutSeconds": 0,
                    }
                }
            }, handle)
        self.commands = []
        self.healthy = False

        def run(argv, **_kwargs):
            self.commands.append(list(argv))
            if argv[-1] == "start":
                self.healthy = True
            elif argv[-1] == "stop":
                self.healthy = False
            return Result()

        def opener(_url, timeout=0):
            if not self.healthy:
                raise OSError("offline")
            return Response()

        lease_store = ResourceLeaseStore(os.path.join(self.temp.name, "leases.sqlite"))
        self.manager = ModelDeploymentManager(
            self.path, run=run, opener=opener, lease_store=lease_store
        )

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_unmatched_remote_model_is_ignored(self):
        self.assertIsNone(self.manager.acquire({"providerId": "deepseek", "modelId": "flash"}))
        self.assertEqual([], self.commands)

    def test_shared_leases_start_once_and_stop_after_last_release(self):
        model = {"providerId": "local", "modelId": "model"}
        first = self.manager.acquire(model)
        second = self.manager.acquire(model)
        self.assertEqual([["modelctl", "start"]], self.commands)
        first.release()
        time.sleep(0.03)
        self.assertEqual([["modelctl", "start"]], self.commands)
        second.release()
        deadline = time.monotonic() + 1
        while len(self.commands) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(["modelctl", "stop"], self.commands[-1])

    def test_new_lease_cancels_pending_idle_stop(self):
        self.manager.deployments["local/model"]["idleTimeoutSeconds"] = 1
        model = {"providerId": "local", "modelId": "model"}
        first = self.manager.acquire(model)
        first.release()
        second = self.manager.acquire(model)
        time.sleep(0.05)
        self.assertEqual([["modelctl", "start"]], self.commands)
        second.release()

    def test_other_bridge_process_lease_prevents_early_stop(self):
        model = {"providerId": "local", "modelId": "model"}
        first = self.manager.acquire(model)
        second_store = ResourceLeaseStore(os.path.join(self.temp.name, "leases.sqlite"))
        second_manager = ModelDeploymentManager(
            self.path,
            run=self.manager._run,
            opener=self.manager._opener,
            lease_store=second_store,
        )
        try:
            second = second_manager.acquire(model)
            second.release()
            time.sleep(0.08)
            self.assertEqual([["modelctl", "start"]], self.commands)
            first.release()
            deadline = time.monotonic() + 1
            while len(self.commands) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(["modelctl", "stop"], self.commands[-1])
        finally:
            second_manager.close()


if __name__ == "__main__":
    unittest.main()
