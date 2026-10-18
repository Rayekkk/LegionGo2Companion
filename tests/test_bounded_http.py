# SPDX-License-Identifier: BSD-3-Clause
"""Offline network-isolation tests: own real processes, no HTTP or hardware."""
import asyncio
import gc
import io
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import support  # noqa: F401  (decky stub for the tdp helper tests)
import bounded_http as http
import tdp_updater
import tdp_backend as tdp

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
        return patch.object(http.subprocess, "Popen", side_effect=support.stalled_child_launcher(source, children))

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
        spawn = support.stalled_child_launcher(children=children)
        def launch(args, **kwargs):
            process = spawn(args, **kwargs)
            entered.set()
            return process
        output = io.BytesIO()
        helper = tdp_updater.Updater(user_agent="test")
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
        import support
        import main
        from companion_updates import CompanionUpdates
        async def run():
            children = []
            with tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                (directory/"package.json").write_text('{"version":"2.1.0"}')
                marker = directory/"dns-entered"
                source = support.dns_stall_source(marker)
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
                        support.reap(children)
                        await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())


class HelperRedirectTests(unittest.TestCase):
    def open_offline(self, helper, transport, contexts):
        # Execute the worker's real urllib opener/redirect policy in-process;
        # replace only its HTTPS transport and the parent process boundary.
        # These guards prevent a stale mock from making a real request again.
        import bounded_http
        import ssl
        real_build = urllib.request.build_opener
        def build(*handlers):
            context = next(value._context for value in handlers
                           if isinstance(value, urllib.request.HTTPSHandler))
            contexts.append(context)
            return real_build(transport, *handlers[1:])
        def fetch(url, **options):
            context = options['context']
            configuration = dict(url=url, headers=options['headers'], policy=options['policy'],
                timeout=options['timeout'], limit=options['limit'], total_seconds=options['total_seconds'],
                ca=''.join(ssl.DER_cert_to_PEM_cert(cert) for cert in context.get_ca_certs(binary_form=True)),
                verify_flags=int(context.verify_flags), minimum_version=int(context.minimum_version),
                maximum_version=int(context.maximum_version))
            body = io.BytesIO()
            try:
                result = bounded_http._fetch(configuration, body)
                body.seek(0)
                return bounded_http.Response(body, result['url'])
            except BaseException:
                body.close()
                raise
        with patch.object(tdp_updater, 'open_http', side_effect=fetch), \
                patch.object(bounded_http.urllib.request, 'build_opener', side_effect=build), \
                patch.object(bounded_http.subprocess, 'Popen', side_effect=AssertionError('Unexpected helper process')), \
                patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected network DNS')), \
                patch('socket.create_connection', side_effect=AssertionError('Unexpected network connection')):
            return helper.open_url('https://github.com/test/asset', 3)

    def test_intermediate_foreign_redirect_is_never_opened(self):
        opened = []
        class OfflineHttps(urllib.request.HTTPSHandler):
            def https_open(self, request):
                opened.append(request.full_url)
                headers = Message()
                headers['Location'] = 'https://foreign.example/asset'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
                return response
        helper = tdp_updater.Updater(user_agent='test')
        with self.assertRaises(ValueError):
            self.open_offline(helper, OfflineHttps(), [])
        self.assertEqual(opened, ['https://github.com/test/asset'])

    def test_redirect_boundary_is_checked_before_followup_request(self):
        import bounded_http
        import ssl
        helper = tdp_updater.Updater(user_agent='test')
        allowed = 'https://release-assets.githubusercontent.com/asset?token=fixture'
        opened, contexts = [], []
        class OfflineHttps(urllib.request.HTTPSHandler):
            def https_open(self, request):
                opened.append(request.full_url)
                headers = Message()
                if request.full_url == allowed:
                    response = urllib.response.addinfourl(io.BytesIO(b'verified-test-body'), headers, allowed, 200)
                    response.msg = 'OK'
                else:
                    headers['Location'] = allowed
                    response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                    response.msg = 'Found'
                return response
        with self.open_offline(helper, OfflineHttps(), contexts) as response:
            self.assertEqual(response.geturl(), allowed)
            self.assertEqual(response.read(), b'verified-test-body')
        self.assertEqual(opened, ['https://github.com/test/asset', allowed])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)
        self.assertSetEqual(set(contexts[0].get_ca_certs(binary_form=True)),
                            set(helper.ssl_context().get_ca_certs(binary_form=True)))
        handler = bounded_http._Redirect('https://github.com/test/asset', 'helper')
        request = urllib.request.Request('https://github.com/test/asset')
        for url in ('http://github.com/test/asset', 'https://example.org/asset',
                    'https://user:password@github.com/asset', 'https://github.com:8443/asset'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        self.assertEqual(handler.redirect_request(request, None, 302, 'Found', {}, allowed).full_url, allowed)

    def test_final_response_boundary_still_closes_invalid_endpoint(self):
        import bounded_http
        helper = tdp_updater.Updater(user_agent='test')
        response = bounded_http.Response(io.BytesIO(b'untrusted'), 'https://example.org/asset')
        with patch.object(tdp_updater, 'open_http', return_value=response), \
                patch.object(bounded_http.subprocess, 'Popen', side_effect=AssertionError('Unexpected helper process')), \
                patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected network DNS')), \
                self.assertRaises(ValueError):
            helper.open_url('https://github.com/test/asset', 3)
        self.assertTrue(response.closed)


class HelperDownloadTests(unittest.TestCase):
    def helper(self):
        return tdp_updater.Updater(user_agent='test')

    def test_total_deadline_stops_a_stream_that_keeps_delivering_data(self):
        clock = [0]
        class SlowResponse(io.BytesIO):
            def read1(self, _size):
                clock[0] += 29
                return b'x'
            def read(self, _size):
                raise AssertionError('read1 must avoid a full-buffer blocking read')
        response, output = SlowResponse(), io.BytesIO()
        helper = self.helper()
        with patch.object(helper, 'open_url', return_value=response) as opened, \
                patch.object(tdp_updater.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(TimeoutError, 'total time limit'):
                helper.download_to(tdp.RYZENADJ_URL, output, timeout=30)
        opened.assert_called_once_with(tdp.RYZENADJ_URL, timeout=tdp_updater.READ_TIMEOUT_SECONDS,
                                       cancel=None, deadline=tdp_updater.MAX_DOWNLOAD_SECONDS)
        self.assertEqual(output.getvalue(), b'xxxx')
        self.assertTrue(response.closed)

    def test_cancelled_read_is_rejected_before_writing_the_returned_chunk(self):
        cancel = threading.Event()
        class Response(io.BytesIO):
            def read1(self, _size):
                cancel.set()
                return b'partial'
        output = io.BytesIO()
        helper = self.helper()
        with patch.object(helper, 'open_url', return_value=Response()):
            with self.assertRaisesRegex(InterruptedError, 'cancelled'):
                helper.download_to(tdp.RYZENADJ_URL, output, timeout=30, cancel=cancel.is_set)
        self.assertEqual(output.getvalue(), b'')


if __name__ == "__main__": unittest.main()
