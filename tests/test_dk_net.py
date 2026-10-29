# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/test_dk_net.py): change it there, then `kit sync`.
# One test (or more) per guarantee in dk_net.py's docstring. No test reaches the internet: the fetch logic runs
# in-process against fake responses, and the isolated process is exercised by requests that cannot succeed.
import hashlib
import http.client
import http.server
import io
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import _harness  # noqa: F401
import dk_net
import dk_process


class Response(io.BytesIO):
    def __init__(self, body, url="https://example.test/file", status=200, length="auto", fail_after=None,
                 content_range=None):
        super().__init__(body)
        self.url, self.status, self.fail_after = url, status, fail_after
        self.headers = {} if length is None else {"Content-Length": str(len(body) if length == "auto" else length)}
        if content_range:
            self.headers["Content-Range"] = content_range

    def geturl(self):
        return self.url

    def read1(self, size=-1):
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise http.client.IncompleteRead(b"", 10)
        return super().read1(size)


class Opener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout):
        return self.response


def request(**changes):
    return {"url": "https://example.test/file", "limit": 1000, "headers": {}, "deadline": 60, "timeout": 30,
            "ca": "", "hosts": None, **changes}


class UrlTest(unittest.TestCase):
    def test_only_plain_https_on_port_443(self):
        dk_net.check_url("https://example.test/a?b=c")
        dk_net.check_url("https://example.test:443/a")
        for bad in ("http://example.test/a", "file:///etc/passwd", "https://user:pw@example.test/",
                    "https://example.test:8443/", "https://example.test:0/", "https://example.test/a\nb", "https:///nohost", "ftp://x/",
                    "https://example.test/" + "a" * 9000, "https://example.test/a#frag", "https://example.test/#",
                    None):
            with self.assertRaises(dk_net.NetError, msg=bad):
                dk_net.check_url(bad)

    def test_hosts_limit_the_first_url(self):
        dk_net.check_url("https://builds.example.test/x", {"builds.example.test"})
        with self.assertRaises(dk_net.NetError):
            dk_net.check_url("https://evil.test/x", {"builds.example.test"})

    def test_every_redirect_is_checked_the_same_way(self):
        handler = dk_net._Redirect({"example.test"})
        for bad in ("http://example.test/x", "https://evil.test/x", "https://example.test:8443/x"):
            with self.assertRaises(dk_net.NetError):
                handler.redirect_request(mock.Mock(), None, 302, "Found", {}, bad)
        with self.assertRaisesRegex(dk_net.NetError, "not allowed"):
            dk_net._Redirect({"example.test"}, allowed=False).redirect_request(
                mock.Mock(), None, 302, "Found", {}, "https://example.test/other")
        narrow = dk_net._Redirect({"example.test", "cdn.example.test"}, allowed=["CDN.example.test"])
        followed = narrow.redirect_request(urllib.request.Request("https://example.test/a"), None, 302, "Found", {},
                                           "https://cdn.example.test/x")
        self.assertEqual(followed.full_url, "https://cdn.example.test/x")
        for bad in ("https://example.test/x", "https://other.test/x"):  # in hosts but not a redirect target / neither
            with self.assertRaises(dk_net.NetError, msg=bad):
                narrow.redirect_request(mock.Mock(), None, 302, "Found", {}, bad)
        with self.assertRaises(dk_net.NetError):  # a final URL that left the hosts (e.g. by a proxy) is refused too
            dk_net._fetch(request(hosts=["example.test"]), io.BytesIO(),
                          Opener(Response(b"x", url="https://evil.test/x")))


class TlsTest(unittest.TestCase):
    def test_tls_is_always_verified(self):
        context = dk_net.tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(dk_net._anchors(), "trust anchors exist and are handed to the fetch process")

    def test_an_empty_system_store_falls_back_to_the_os_bundles(self):
        # Decky's frozen Python finds no system store on SteamOS: the OS bundle files are what it trusts then.
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bundle = os.path.join(TLS, "test-only-cert.pem")
        with mock.patch.object(dk_net, "_context", None), \
                mock.patch.object(dk_net.ssl, "create_default_context", return_value=empty), \
                mock.patch.object(dk_net, "CA_FILES", (os.path.join(TLS, "no-such-bundle.pem"), bundle)):
            context = dk_net.tls_context()
        self.assertIs(context, empty)
        self.assertGreaterEqual(context.cert_store_stats()["x509"], 1, "a missing bundle is skipped, the next loaded")
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_the_fetch_process_verifies_with_the_handed_anchors(self):
        built = []
        real = dk_net.urllib.request.HTTPSHandler

        def spy(context):
            built.append(context)
            return real(context=context)

        strict = int(dk_net.tls_context().verify_flags | ssl.VERIFY_X509_STRICT)
        with mock.patch.object(dk_net.urllib.request, "HTTPSHandler", side_effect=spy), \
                mock.patch.object(dk_net.urllib.request, "build_opener",
                                  return_value=Opener(Response(b"x"))):
            dk_net._fetch(request(ca=dk_net._anchors(), verify_flags=strict, minimum_version=int(
                ssl.TLSVersion.TLSv1_2), maximum_version=int(ssl.TLSVersion.MAXIMUM_SUPPORTED)), io.BytesIO())
        self.assertTrue(built[0].check_hostname)
        self.assertEqual(built[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(built[0].get_ca_certs())
        self.assertTrue(built[0].verify_flags & ssl.VERIFY_X509_STRICT, "the plugin's verify flags reach the child")
        self.assertEqual(built[0].minimum_version, ssl.TLSVersion.TLSv1_2)


class FetchTest(unittest.TestCase):
    def fetch(self, response, **changes):
        body = io.BytesIO()
        return dk_net._fetch(request(**changes), body, Opener(response)), body.getvalue()

    def test_a_good_body_is_written_whole(self):
        self.assertEqual(self.fetch(Response(b"payload")), ({"kind": "ok", "size": 7}, b"payload"))

    def test_size_limit_by_header_and_by_body(self):
        self.assertEqual(self.fetch(Response(b"x" * 10, length=5000), limit=1000)[0], {"kind": "large"})
        self.assertEqual(self.fetch(Response(b"x" * 1001, length=None), limit=1000)[0], {"kind": "large"})

    def test_a_cut_short_body_is_an_error(self):
        self.assertEqual(self.fetch(Response(b"abc", length=10))[0], {"kind": "short"})
        with self.assertRaises(http.client.IncompleteRead):  # reported as a network error by the process
            self.fetch(Response(b"abcdef", fail_after=0))

    def test_only_http_200_is_accepted(self):
        self.assertEqual(self.fetch(Response(b"partial", status=206))[0], {"kind": "http", "code": 206})

    def test_a_byte_range_is_exactly_that_part(self):
        part = {"range": [10, 16]}
        self.assertEqual(self.fetch(Response(b"payload", status=206, content_range="bytes 10-16/100"), **part),
                         ({"kind": "ok", "size": 7}, b"payload"))
        self.assertEqual(self.fetch(Response(b"x" * 100), **part)[0], {"kind": "range"}, "the whole file instead")
        self.assertEqual(self.fetch(Response(b"payload", status=206, content_range="bytes 0-6/100"), **part)[0],
                         {"kind": "range"}, "another part")
        self.assertEqual(self.fetch(Response(b"payload", status=206), **part)[0], {"kind": "range"},
                         "no Content-Range")
        self.assertEqual(self.fetch(Response(b"pay", status=206, length=None, content_range="bytes 10-16/100"),
                                    **part)[0], {"kind": "short"}, "fewer bytes than the range")
        self.assertEqual(self.fetch(Response(b"x", status=404), **part)[0], {"kind": "http", "code": 404})

    def test_the_deadline_covers_the_whole_body(self):
        with mock.patch.object(dk_net.time, "monotonic", side_effect=[0, 1000, 1000]):
            self.assertEqual(self.fetch(Response(b"x" * 10))[0], {"kind": "deadline"})

    def test_the_fetch_process_trusts_no_caller_with_its_bounds(self):
        for bad in ({"limit": 0}, {"limit": dk_net.MAX_BODY + 1}, {"deadline": float("inf")}, {"deadline": None},
                    {"deadline": dk_net.MAX_DEADLINE + 1}, {"range": [5, 4]}, {"range": [-1, 4]},
                    {"range": [0, 1000]}, {"range": [0, "9"]}, {"range": [1]}):
            with self.assertRaises(ValueError, msg=bad):
                self.fetch(Response(b"x"), **bad)
        self.assertEqual(self.fetch(Response(b"x"), deadline=1800)[0]["kind"], "ok", "45 MB on a slow link")

TLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tls")


class RealTlsTest(unittest.TestCase):
    """The real fetch process against a local HTTPS server: only the port rule is relaxed (in the child, by the
    test's own wrapper), everything else - TLS, anchors, spooling, the status line - is the shipped code."""

    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"served over TLS"
                wanted = self.headers.get("Range", "")
                if wanted.startswith("bytes="):  # one closed range, as a CDN answers it
                    first, last = (int(n) for n in wanted[6:].split("-"))
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(body)}")
                    body = body[first:last + 1]
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.join(TLS, "test-only-cert.pem"), os.path.join(TLS, "test-only-key.pem"))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        dk_process.PYTHON = sys.executable

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        dk_process.PYTHON = "/usr/bin/python3" if os.name != "nt" else sys.executable

    def child(self, anchors_file, **changes):
        with open(os.path.join(TLS, anchors_file), encoding="ascii") as f:
            anchors = f.read()
        url = f"https://127.0.0.1:{self.server.server_address[1]}/file"
        config = {"url": url, "limit": 1000, "headers": {}, "deadline": 30, "timeout": 10, "ca": anchors,
                  "hosts": None, "redirects": False, **changes}
        code = (f"import sys; sys.path.insert(0, {os.path.dirname(dk_net.__file__)!r}); import dk_net; "
                "dk_net.check_url = lambda url, hosts=None: url; raise SystemExit(dk_net._fetch_main())")
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as body, tempfile.TemporaryFile() as status:
            stdin.write(json.dumps(config).encode())
            stdin.seek(0)
            code_, _, _ = dk_process.run_helper(["-c", code], 30, stdin=stdin, stdout=body, stderr=status)
            body.seek(0)
            status.seek(0)
            return code_, body.read(), json.loads(status.read())

    def test_a_real_download_through_the_fetch_process(self):
        self.assertEqual(self.child("test-only-cert.pem"), (0, b"served over TLS", {"kind": "ok", "size": 15}))

    def test_a_real_byte_range_through_the_fetch_process(self):
        self.assertEqual(self.child("test-only-cert.pem", range=[4, 9], headers={"Range": "bytes=4-9"}),
                         (0, b"ed ove", {"kind": "ok", "size": 6}))
        self.assertEqual(self.child("test-only-cert.pem", range=[4, 9])[2], {"kind": "range"},
                         "the whole file instead of the part is refused")

    def test_a_server_the_anchors_do_not_trust_is_refused(self):
        code, body, status = self.child("other-ca.pem")
        self.assertEqual((code, body, status["kind"]), (1, b"", "network"))
        self.assertIn("CERTIFICATE_VERIFY_FAILED", status["reason"])


class ProcessTest(unittest.TestCase):
    def setUp(self):
        dk_process.PYTHON = sys.executable
        self.addCleanup(setattr, dk_process, "PYTHON", "/usr/bin/python3" if os.name != "nt" else sys.executable)

    def test_requests_run_in_their_own_process_and_fail_cleanly(self):
        with mock.patch.object(dk_net.urllib.request, "urlopen", side_effect=AssertionError("in the plugin")):
            with self.assertRaises(dk_net.NetError) as caught:
                dk_net.get("https://127.0.0.1/never", 100, hosts=None, deadline=10)
        self.assertNotIn("127.0.0.1", str(caught.exception), "messages carry no URL")

    def test_deadline_and_cancel_end_the_request(self):
        slow = mock.patch.object(dk_net, "_anchors", return_value="")
        with slow, mock.patch.object(dk_process, "run_helper", side_effect=TimeoutError):
            with self.assertRaisesRegex(dk_net.NetError, "too long"):
                dk_net.get("https://example.test/x", 100, hosts=None)
        with mock.patch.object(dk_process.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(dk_net.NetError, "cancelled"):
                dk_net.get("https://10.255.255.1/x", 100, hosts=None, cancel=lambda: True)
        popen.assert_not_called()  # a cancelled request starts no process
        start = time.monotonic()
        flips = iter([False, False, False, True])
        with self.assertRaisesRegex(dk_net.NetError, "cancelled"):
            dk_net.get("https://10.255.255.1/x", 100, hosts=None, cancel=lambda: next(flips, True))
        self.assertLess(time.monotonic() - start, 10)

    def test_a_fetch_process_that_cannot_start_is_a_net_error(self):
        with mock.patch.object(dk_process, "PYTHON", os.path.join(tempfile.gettempdir(), "no-such-python")):
            with self.assertRaisesRegex(dk_net.NetError, "could not start") as caught:
                dk_net.get("https://example.test/x", 100, hosts=None)
        self.assertNotIn(tempfile.gettempdir(), str(caught.exception), "an OSError's path holds the user's name")

    def test_bounds_and_hosts_are_required_up_front(self):
        with self.assertRaises(TypeError):
            dk_net.get("https://example.test/x", 100)  # every call says which hosts it trusts
        for bad in ({"deadline": float("inf")}, {"deadline": None}, {"deadline": 0}, {"byte_range": (5, 4)},
                    {"byte_range": (0, 100)}):
            with self.assertRaises(ValueError, msg=bad):
                dk_net.get("https://example.test/x", 100, hosts=None, **bad)

    def test_the_child_gets_a_user_agent_and_answers_map_to_messages(self):
        seen = {}

        def fake_helper(args, seconds, cancelled, *, stdin, stdout, stderr):
            seen.update(json.loads(stdin.read()))
            stderr.write(json.dumps(answer).encode())
            return 1, None

        for answer, message in (({"kind": "http", "code": 429}, "HTTP 429"),
                                ({"kind": "http", "code": 404}, "HTTP 404"),
                                ({"kind": "short"}, "dropped before the end"),
                                ({"kind": "network", "reason": "[Errno -3] Temporary failure in name resolution"},
                                 "name resolution"),
                                ({"kind": "large"}, "larger than expected")):
            with mock.patch.object(dk_process, "run_helper", side_effect=fake_helper):
                with self.assertRaisesRegex(dk_net.NetError, message):
                    dk_net.get("https://example.test/x", 100, hosts=None)
        self.assertTrue(seen["headers"]["User-Agent"])
        self.assertIn("verify_flags", seen)
        with mock.patch.object(dk_process, "run_helper", side_effect=fake_helper):
            answer = {"kind": "http", "code": 404}
            with self.assertRaises(dk_net.NetError) as caught:
                dk_net.get("https://example.test/x", 100, hosts=None)
            self.assertEqual(caught.exception.status, 404, "the updater tells 'no release' from a refusal")
            answer = {"kind": "short"}
            with self.assertRaises(dk_net.NetError) as caught:
                dk_net.get("https://example.test/x", 100, hosts=None)
            self.assertIsNone(caught.exception.status)

    def test_a_range_and_progress_reach_the_child_and_the_caller(self):
        seen, sizes = {}, []

        def fake_helper(args, seconds, cancelled, *, stdin, stdout, stderr):
            seen.update(json.loads(stdin.read()))
            stdout.write(b"abc")
            stdout.flush()
            self.assertFalse(cancelled())
            stdout.write(b"defg")
            stdout.flush()
            self.assertFalse(cancelled())
            stderr.write(json.dumps({"kind": "ok", "size": 7}).encode())
            return 0, None

        with mock.patch.object(dk_process, "run_helper", side_effect=fake_helper):
            data = dk_net.get("https://example.test/x", 7, hosts=None, byte_range=(10, 16), progress=sizes.append)
        self.assertEqual(data, b"abcdefg")
        self.assertEqual(seen["headers"]["Range"], "bytes=10-16")
        self.assertEqual(seen["range"], [10, 16])
        self.assertEqual(sizes, [3, 7])
        with mock.patch.object(dk_process, "run_helper", side_effect=fake_helper):
            with self.assertRaises(AssertionError):  # cancel still works next to progress
                dk_net.get("https://example.test/x", 7, hosts=None, progress=sizes.append, cancel=lambda: True)
        with mock.patch.object(dk_process, "run_helper", side_effect=lambda *a, **k: (
                k["stderr"].write(json.dumps({"kind": "range"}).encode()), (1, None))[1]):
            with self.assertRaisesRegex(dk_net.NetError, "requested part"):
                dk_net.get("https://example.test/x", 7, hosts=None, byte_range=(10, 16))

    def test_refusals_happen_before_any_process_starts(self):
        with mock.patch.object(dk_process, "run_helper") as helper:
            for url in ("http://example.test/x", "https://user@example.test/x"):
                with self.assertRaises(dk_net.NetError):
                    dk_net.get(url, 100, hosts=None)
            with self.assertRaises(dk_net.NetError):
                dk_net.get("https://evil.test/x", 100, hosts={"Example.test"})
        helper.assert_not_called()


def fake_run(content):
    def run(url, body, limit, headers, hosts, deadline, cancel, redirects, byte_range=None, progress=None):
        dk_net.check_url(url, hosts)
        body.write(content)
    return run


class ContentTest(unittest.TestCase):
    def setUp(self):
        self.data = b"payload" * 100
        self.sha = hashlib.sha256(self.data).hexdigest()

    def test_get_checks_size_and_pinned_hash(self):
        with mock.patch.object(dk_net, "_run", side_effect=fake_run(self.data)):
            self.assertEqual(dk_net.get("https://example.test/x", 10_000, self.sha.upper(), hosts=None), self.data)
            with self.assertRaises(dk_net.Mismatch):
                dk_net.get("https://example.test/x", 10_000, "0" * 64, hosts=None)
            with self.assertRaises(dk_net.TooLarge):
                dk_net.get("https://example.test/x", 10, self.sha, hosts=None)

    def test_download_publishes_only_a_complete_matching_file_through_dk_fs(self):
        folder = tempfile.mkdtemp()
        dest = os.path.join(folder, "file.bin")
        with open(dest, "wb") as f:
            f.write(b"old")
        with mock.patch.object(dk_net, "_run", side_effect=fake_run(self.data)):
            with self.assertRaises(dk_net.Mismatch):
                dk_net.download("https://example.test/x", dest, 10_000, "0" * 64, hosts=None)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), b"old", "dest untouched")
            with mock.patch("dk_fs.write_atomic", wraps=__import__("dk_fs").write_atomic) as publish:
                self.assertEqual(dk_net.download("https://example.test/x", dest, 10_000, self.sha, hosts=None),
                                 self.sha)
            publish.assert_called_once()  # atomic, fsynced, no symlinks as root: dk_fs's guarantees
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), self.data)
        with mock.patch.object(dk_net, "_run", side_effect=dk_net.NetError("the connection dropped")):
            with self.assertRaises(dk_net.NetError):
                dk_net.download("https://example.test/x", dest, 10_000, hosts=None)
        self.assertEqual(os.listdir(folder), ["file.bin"])

    def test_a_full_disk_is_a_net_error_and_dest_stays(self):
        folder = tempfile.mkdtemp()
        dest = os.path.join(folder, "file.bin")
        with open(dest, "wb") as f:
            f.write(b"old")
        full = OSError(28, "No space left on device", dest)
        with mock.patch.object(dk_net, "_run", side_effect=fake_run(self.data)), \
                mock.patch("dk_fs.write_atomic", side_effect=full):
            with self.assertRaisesRegex(dk_net.NetError, "No space left") as caught:
                dk_net.download("https://example.test/x", dest, 10_000, hosts=None)
        self.assertNotIn(folder, str(caught.exception), "no path in the message")
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"old")
        with mock.patch("tempfile.TemporaryFile", side_effect=full):
            with self.assertRaises(dk_net.NetError):
                dk_net.get("https://example.test/x", 100, hosts=None)

    def test_spool_hands_over_the_checked_body_and_keeps_nothing(self):
        folder = tempfile.mkdtemp()
        with mock.patch.object(dk_net, "_run", side_effect=fake_run(self.data)):
            with dk_net.spool("https://example.test/x", folder, 10_000, self.sha, hosts=None) as body:
                self.assertEqual(body.read(), self.data, "read from its start")
            with self.assertRaises(dk_net.Mismatch):
                dk_net.spool("https://example.test/x", folder, 10_000, "0" * 64, hosts=None)
            with self.assertRaises(dk_net.TooLarge):
                dk_net.spool("https://example.test/x", folder, 10, hosts=None)
        with mock.patch("tempfile.TemporaryFile", side_effect=OSError(28, "No space left on device", folder)):
            with self.assertRaisesRegex(dk_net.NetError, "No space left") as caught:
                dk_net.spool("https://example.test/x", folder, 10_000, hosts=None)
        self.assertNotIn(folder, str(caught.exception))
        self.assertEqual(os.listdir(folder), [], "an anonymous file: nothing on disk after close or failure")
        spools = []
        real = tempfile.TemporaryFile
        with mock.patch.object(dk_net, "_run", side_effect=fake_run(self.data)),                 mock.patch("tempfile.TemporaryFile", side_effect=lambda *a, **k: spools.append(k.get("dir")) or real(*a, **k)):
            dk_net.spool("https://example.test/x", folder, 10_000, hosts=None).close()
        self.assertEqual(spools, [folder], "in the caller's folder, not in /tmp")

    def test_the_body_is_spooled_beside_dest_not_in_tmp(self):
        folder = tempfile.mkdtemp()
        spools = []
        real = tempfile.TemporaryFile

        def spy(*a, **k):
            spools.append(k.get("dir"))
            return real(*a, **k)

        with mock.patch.object(dk_net, "_run", side_effect=fake_run(self.data)), \
                mock.patch("tempfile.TemporaryFile", side_effect=spy):
            dk_net.download("https://example.test/x", os.path.join(folder, "f.bin"), 10_000, hosts=None)
        self.assertEqual(spools, [os.path.abspath(folder)])


if __name__ == "__main__":
    unittest.main()
