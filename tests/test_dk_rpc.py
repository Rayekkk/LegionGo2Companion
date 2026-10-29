# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/test_dk_rpc.py): change it there, then `kit sync`.
import asyncio
import inspect
import json
import logging
import unittest

import _harness  # noqa: F401
import dk_rpc

# Its own logger: a plugin may filter decky.logger (Ayaneo3Companion strips tracebacks of paths), which is not
# what these tests are about.
LOG = logging.getLogger("dk_rpc-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class RpcTest(unittest.TestCase):
    def test_answers_follow_the_contract(self):
        self.assertEqual(dk_rpc.ok(), {"ok": True})
        self.assertEqual(dk_rpc.ok({"a": 1}), {"ok": True, "state": {"a": 1}})
        self.assertEqual(dk_rpc.fail("No.", {"a": 1}), {"ok": False, "error": "No.", "state": {"a": 1}})

    def test_an_unexpected_exception_is_logged_and_never_reaches_the_panel(self):
        rpc = dk_rpc.boundary(LOG)

        class Plugin:
            @rpc
            async def broken(self, value):
                raise KeyError(value)

        with self.assertLogs(LOG, "ERROR") as logs:
            result = asyncio.run(Plugin().broken("x"))
        self.assertFalse(result["ok"])
        self.assertIn("KeyError", result["error"])
        self.assertNotIn("Traceback", result["error"])
        self.assertIn("Traceback", logs.output[0], "the log keeps the traceback")

    def test_the_panel_never_sees_a_path(self):
        rpc = dk_rpc.boundary(LOG)

        class Plugin:
            @rpc
            async def save(self):
                raise PermissionError(13, "Permission denied", "/home/alice/.config/x.json")

        with self.assertLogs(LOG, "ERROR") as logs:
            result = asyncio.run(Plugin().save())
        self.assertNotIn("alice", result["error"])
        self.assertIn("PermissionError", result["error"])
        self.assertIn("alice", logs.output[0], "the log keeps the details")

    def test_a_plugin_that_never_gets_ready_answers_instead_of_hanging(self):
        async def scenario():
            rpc = dk_rpc.boundary(LOG, asyncio.Event(), wait=0.05)

            class Plugin:
                @rpc
                async def get(self):
                    return dk_rpc.ok("state")

            return await Plugin().get()

        with self.assertLogs(LOG, "ERROR"):
            result = asyncio.run(scenario())
        self.assertFalse(result["ok"])
        self.assertIn("starting", result["error"])

    def test_a_shown_refusal_reaches_the_panel_as_it_is_without_a_traceback(self):
        rpc = dk_rpc.boundary(LOG, shown=(RuntimeError,))

        class Plugin:
            @rpc
            async def write(self, kind):
                raise RuntimeError("This device is not an AYANEO 3") if kind == "refusal" else KeyError("k")

        with self.assertLogs(LOG, "WARNING") as logs:
            result = asyncio.run(Plugin().write("refusal"))
        self.assertEqual(result, {"ok": False, "error": "This device is not an AYANEO 3"})
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIsNone(logs.records[0].exc_info, "a refusal is not a bug: no traceback")
        with self.assertLogs(LOG, "ERROR"):
            self.assertIn("Unexpected error (KeyError)", asyncio.run(Plugin().write("bug"))["error"])

    def test_only_the_plugins_own_subclasses_of_a_shown_type_are_refusals(self):
        class Busy(RuntimeError):  # the plugin's own refusal
            pass

        rpc = dk_rpc.boundary(LOG, shown=(RuntimeError, ValueError))

        class Plugin:
            @rpc
            async def call(self, error):
                raise error

        with self.assertLogs(LOG, "WARNING"):
            self.assertEqual(asyncio.run(Plugin().call(Busy("Sound is playing"))),
                             {"ok": False, "error": "Sound is playing"})
        for bug in (json.JSONDecodeError("Expecting value", "x", 0), NotImplementedError("todo"),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")):
            with self.subTest(bug=type(bug).__name__), self.assertLogs(LOG, "ERROR"):
                result = asyncio.run(Plugin().call(bug))
            self.assertEqual(result["error"],
                             f"Unexpected error ({type(bug).__name__}). Details are in the Decky log.")

    def test_a_method_that_keeps_failing_the_same_way_logs_once_until_it_succeeds(self):
        rpc = dk_rpc.boundary(LOG, shown=(RuntimeError,))
        answers = []

        class Plugin:
            @rpc
            async def get_state(self):
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return dk_rpc.ok(answer)

        async def polls():
            return [await Plugin().get_state() for _ in range(len(answers))]

        answers[:] = [KeyError("a"), KeyError("a"), KeyError("a"), RuntimeError("busy"), RuntimeError("busy"),
                      "fine", KeyError("a")]
        with self.assertLogs(LOG, "WARNING") as logs:
            results = asyncio.run(polls())
        self.assertEqual([r["ok"] for r in results], [False] * 5 + [True, False])
        self.assertEqual([r.levelname for r in logs.records], ["ERROR", "WARNING", "ERROR"],
                         "each change of failure once; after a success the same failure is news again")
        self.assertIsNotNone(logs.records[0].exc_info, "an unexpected error keeps its traceback")

    def test_the_method_keeps_its_name_and_signature(self):
        rpc = dk_rpc.boundary(LOG)

        class Plugin:
            @rpc
            async def set_value(self, value: int, game: str = "") -> dict:
                return dk_rpc.ok()

        self.assertEqual(Plugin.set_value.__name__, "set_value")
        self.assertEqual(list(inspect.signature(Plugin.set_value).parameters), ["self", "value", "game"])
        self.assertTrue(inspect.iscoroutinefunction(Plugin.set_value))

    def test_cancellation_is_not_swallowed(self):
        rpc = dk_rpc.boundary(LOG)

        class Plugin:
            @rpc
            async def cancelled(self):
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(Plugin().cancelled())

    def test_calls_wait_until_the_plugin_is_ready(self):
        async def scenario():
            ready = asyncio.Event()
            rpc = dk_rpc.boundary(LOG, ready)

            class Plugin:
                @rpc
                async def get(self):
                    return dk_rpc.ok("state")

            call = asyncio.ensure_future(Plugin().get())
            await asyncio.sleep(0)
            self.assertFalse(call.done())
            ready.set()
            return await call

        self.assertEqual(asyncio.run(scenario()), {"ok": True, "state": "state"})


if __name__ == "__main__":
    unittest.main()
