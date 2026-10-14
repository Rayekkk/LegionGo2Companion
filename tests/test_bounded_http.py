# SPDX-License-Identifier: BSD-3-Clause
"""Offline network-isolation tests: own real processes, no HTTP or hardware."""
import asyncio
import gc
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error

import bounded_http as http

URL = "https://api.github.com/repos/Rayekkk/LegionGo2Companion/releases/latest"


def request(**changes):
    value = {"url": URL, "headers": {}, "ca": "", "policy": "metadata", "timeout": 1,
        "limit": 1024, "total_seconds": 2, "verify_flags": 0,
        "minimum_version": int(ssl.TLSVersion.TLSv1_2), "maximum_version": -1}
    return value | changes


class WorkerPolicyTests(unittest.TestCase):
    def response(self, data=b"valid", url=URL):
        response = io.BytesIO(data)
        response.status = 200
        response.geturl = lambda: url
        return response

    def fetch(self, response, **changes):
        body = io.BytesIO()
        with patch.object(http.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            result = http._fetch(request(**changes), body)
        return result, body.getvalue()

    def test_worker_streams_exact_bytes_and_requires_final_url_and_size(self):
        result, data = self.fetch(self.response())
        self.assertEqual((result, data), ({"kind": "ok", "url": URL}, b"valid"))
        for response in (self.response(b"x"*1025), self.response(url="https://foreign.invalid/file")):
            with self.assertRaises(ValueError): self.fetch(response)
            self.assertTrue(response.closed)

    def test_redirect_policy_is_shared_and_preserves_metadata_archive_and_helper_scopes(self):
        for policy in ("metadata", "archive", "helper"):
            handler = http._Redirect(URL, policy)
            req = http.urllib.request.Request(URL)
            for bad in ("http://github.com/file", "https://github.com.evil.test/file", "file:///etc/passwd",
                        "https://user:secret@github.com/file", "https://github.com:444/file"):
                stream = io.BytesIO()
                with self.assertRaises(ValueError): handler.redirect_request(req, stream, 302, "", {}, bad)
                self.assertTrue(stream.closed)
        asset = "https://release-assets.githubusercontent.com/release?token=public-test"
        with self.assertRaises(ValueError): http.checked_url(asset, URL, "metadata")
        self.assertEqual(http.checked_url(asset, URL, "archive"), asset)
        self.assertEqual(http.checked_url("https://codeload.github.com/repo", URL, "helper"),
                         "https://codeload.github.com/repo")

    def test_worker_enforces_post_read_deadline_before_spooling(self):
        class Delayed(io.BytesIO):
            def read1(self, size):
                clock[0] += 3
                return super().read(size)
        clock = [0]
        response = Delayed(b"late")
        response.status = 200
        response.geturl = lambda: URL
        output = io.BytesIO()
        with patch.object(http.urllib.request, "build_opener") as opener, \
                patch.object(http.time, "monotonic", side_effect=lambda: clock[0]):
            opener.return_value.open.return_value = response
            with self.assertRaises(TimeoutError): http._fetch(request(), output)
        self.assertEqual(output.getvalue(), b"")


class ProcessDeadlineTests(unittest.TestCase):
    def open(self, **changes):
        options = {"headers": {}, "context": ssl.create_default_context(), "timeout": 1,
            "limit": 1024, "total_seconds": 2, "policy": "metadata"}
        return http.open_http(URL, **(options | changes))

    def child(self, source, children):
        original = subprocess.Popen
        def launch(args, **kwargs):
            process = original([sys.executable, "-I", "-c", source], **kwargs)
            children.append(process)
            return process
        return patch.object(http.subprocess, "Popen", side_effect=launch)

    def test_real_child_output_is_spooled_and_http_errors_keep_status(self):
        children = []
        source = "import sys,json; p=json.load(sys.stdin); sys.stdout.buffer.write(b'complete'); sys.stderr.write(json.dumps({'kind':'ok','url':p['url']}))"
        with self.child(source, children), self.open() as response:
            self.assertEqual(response.geturl(), URL)
            self.assertEqual(response.read1(), b"complete")
        self.assertTrue(response.closed)
        self.assertEqual(children[0].poll(), 0)
        source = "import sys,json; json.load(sys.stdin); sys.stderr.write(json.dumps({'kind':'http','code':404})); sys.exit(1)"
        gc.collect()
        with patch.object(sys, 'unraisablehook') as unraisable:
            with self.child(source, []), self.assertRaises(urllib.error.HTTPError) as error:
                self.open()
            self.assertEqual(error.exception.code, 404)
            self.assertTrue(error.exception.closed)
            del error
            gc.collect()
        unraisable.assert_not_called()

    def test_wall_deadline_kills_and_reaps_only_its_stalled_helper(self):
        children = []
        with self.child("import time; time.sleep(30)", children):
            started = time.monotonic()
            with self.assertRaises(TimeoutError): self.open(total_seconds=.2)
        self.assertLess(time.monotonic()-started, 3)
        self.assertIsNotNone(children[0].poll())

    def test_pre_cancelled_request_never_spawns(self):
        with patch.object(http.subprocess, "Popen") as launch:
            with self.assertRaises(InterruptedError): self.open(cancel=lambda: True)
        launch.assert_not_called()

    def test_ryzenadj_cancellation_reaps_blocked_worker_without_writing_destination(self):
        import tdp_updater
        cancelled, entered, children = threading.Event(), threading.Event(), []
        original = subprocess.Popen
        def launch(args, **kwargs):
            process = original([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(process)
            entered.set()
            return process
        output = io.BytesIO()
        helper = tdp_updater.Updater(user_agent="test", log_prefix="test", plugin_dir=".", logger=Mock())
        def cancel_after_spawn():
            if entered.wait(3): cancelled.set()
        timer = threading.Thread(target=cancel_after_spawn)
        timer.start()
        try:
            with patch.object(http.subprocess, "Popen", side_effect=launch), self.assertRaises(InterruptedError):
                helper.download_to("https://github.com/test/helper", output, 3, cancel=cancelled.is_set)
        finally:
            entered.set();timer.join(3)
        self.assertEqual(output.getvalue(), b"")
        self.assertIsNotNone(children[0].poll())

    def test_plugin_unload_reaps_helper_held_inside_real_urllib_dns(self):
        import test_integration
        import main
        from companion_updates import CompanionUpdates
        async def run():
            children = []
            with tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                (directory/"package.json").write_text('{"version":"2.1.0"}')
                marker = directory/"dns-entered"
                source = ("import json,runpy,socket,sys,time; from pathlib import Path\n"
                    "def resolver(*args,**kwargs):\n"
                    f" Path({str(marker)!r}).write_text('entered')\n"
                    " time.sleep(30)\n"
                    " raise OSError('controlled DNS failure')\n"
                    "socket.getaddrinfo=resolver\n"
                    f"sys.argv=[{str(Path(http.__file__).resolve())!r},'--fetch']\n"
                    f"runpy.run_path({str(Path(http.__file__).resolve())!r},run_name='__main__')\n")
                plugin = main.Plugin()
                plugin._check_guard = AsyncMock()
                plugin._updates = CompanionUpdates(raw, raw, 1000, 1000)
                with self.child(source, children):
                    task = asyncio.create_task(plugin.updates_check())
                    try:
                        deadline = time.monotonic()+3
                        while not marker.exists() and time.monotonic() < deadline: await asyncio.sleep(.02)
                        self.assertTrue(marker.exists(), "real urllib did not enter the controlled OS DNS boundary")
                        await asyncio.wait_for(plugin._unload(), 2)
                        await asyncio.wait_for(task, 1)
                        self.assertFalse(plugin._update_jobs)
                        self.assertTrue(plugin._updates._closed.is_set())
                        self.assertIsNotNone(children[0].poll())
                    finally:
                        plugin._updates.close()
                        for child in children:
                            if child.poll() is None: child.kill()
                            child.wait(timeout=3)
                        await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())


if __name__ == "__main__": unittest.main()
