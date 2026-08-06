from __future__ import annotations

import unittest

from backend_manager import BackendManager
from control_plane import ControlPlaneError


class FakeControl:
    def __init__(self, name):
        self.name = name
        self.runs = {}
        self.closed = False

    def start(self, _args):
        run_id = "run_%s_%s" % (self.name, len(self.runs) + 1)
        result = {"runId": run_id, "status": "running", "revision": 1}
        self.runs[run_id] = result
        return dict(result)

    def wait(self, run_id, **_kwargs):
        return dict(self.runs[run_id])

    def observe(self, run_id, **_kwargs):
        return dict(self.runs[run_id])

    def control(self, run_id, action, **_kwargs):
        return {**self.runs[run_id], "action": action}

    def recover(self, args):
        if args.get("adoptThreadId"):
            return self.start(args)
        return {"sessions": [], "count": 0}

    def branch(self, run_id, **_kwargs):
        return {"parentRunId": run_id, "threadId": "forked"}

    def context(self, run_id, **_kwargs):
        return {"runId": run_id, "context": {}}

    def close_run(self, run_id=None, **_kwargs):
        return {**self.runs[run_id], "status": "closed"}

    def owns(self, run_id):
        return run_id in self.runs

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, name, available=True):
        self.name = name
        self.available = available
        self.capabilities = {"prompt": True, "backend": name}
        self.control = FakeControl(name)

    def probe(self):
        return {"available": self.available, "version": "test"}

    def control_plane(self, **_kwargs):
        return self.control


class BackendManagerTest(unittest.TestCase):
    def setUp(self):
        self.backends = {
            name: FakeBackend(name) for name in ("zcode", "opencode", "pi")
        }
        self.manager = BackendManager(
            lambda: ("zcode", "bundle"),
            backends=self.backends,
            default_backend="zcode",
        )

    def tearDown(self):
        self.manager.close()

    def test_config_selects_once_and_start_uses_selection(self):
        configured = self.manager.configure({"action": "set", "backend": "pi"})
        self.assertEqual("pi", configured["selectedBackend"])
        self.assertEqual("task", configured["source"])

        first = self.manager.start({"prompt": "one"})
        second = self.manager.start({"prompt": "two"})
        self.assertEqual("pi", first["backend"])
        self.assertEqual("pi", second["backend"])

    def test_backend_switch_does_not_rebind_existing_run(self):
        self.manager.configure({"action": "set", "backend": "pi"})
        first = self.manager.start({"prompt": "one"})
        self.manager.configure({"action": "set", "backend": "opencode"})
        second = self.manager.start({"prompt": "two"})

        self.assertEqual("pi", self.manager.wait(first["runId"])["backend"])
        self.assertEqual("opencode", self.manager.wait(second["runId"])["backend"])

    def test_unavailable_selection_is_atomic(self):
        self.backends["pi"].available = False
        with self.assertRaises(ControlPlaneError) as caught:
            self.manager.configure({"action": "set", "backend": "pi"})
        self.assertEqual("backend_unavailable", caught.exception.code)
        self.assertEqual("zcode", self.manager.configure({"action": "get"})["selectedBackend"])

    def test_list_reports_capabilities_and_probe(self):
        listed = self.manager.configure({"action": "list"})
        self.assertEqual({"zcode", "opencode", "pi"}, set(listed["availableBackends"]))
        self.assertTrue(listed["availableBackends"]["pi"]["capabilities"]["prompt"])


if __name__ == "__main__":
    unittest.main()
