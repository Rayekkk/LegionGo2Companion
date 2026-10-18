# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGoTDP

"""Bounded, verified HTTPS download of the pinned RyzenAdj helper."""

import time
from bounded_http import checked_url as _http_url, open_http, tls_context

MAX_DOWNLOAD_BYTES = 48 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 120
READ_TIMEOUT_SECONDS = 3


def checked_url(url: str) -> str:
    """Reject anything that is not an https URL on a known GitHub host."""
    return _http_url(url, url, "helper")


class Updater:
    def __init__(self, *, user_agent: str):
        self.user_agent = user_agent

    def ssl_context(self):
        return tls_context()

    def open_url(self, url: str, timeout: int, headers: dict | None = None, *, cancel=None, deadline=None):
        """Verified HTTPS with a parent deadline that also covers OS DNS."""
        checked_url(url)
        remaining = MAX_DOWNLOAD_SECONDS if deadline is None else deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError("RyzenAdj download exceeded its total time limit")
        response = open_http(url, headers=headers or {"User-Agent": self.user_agent},
            context=self.ssl_context(), timeout=timeout, limit=MAX_DOWNLOAD_BYTES,
            total_seconds=min(MAX_DOWNLOAD_SECONDS, remaining), policy="helper", cancel=cancel)
        try:
            # Retain a final-response check alongside the pre-request hop guard.
            checked_url(response.geturl())
        except Exception:
            response.close()
            raise
        return response

    def download_to(self, url: str, out, timeout: int, cancel=None) -> int:
        """Stream a URL with size/time ceilings and cooperative cancellation.

        Returns the number of bytes written. The caller is responsible for
        removing a partial file, since a truncated archive that looks complete
        is worse than no archive at all.
        """
        written = 0
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
        def check():
            if cancel is not None and cancel():
                raise InterruptedError("RyzenAdj download was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("RyzenAdj download exceeded its total time limit")
        check()
        with self.open_url(url, timeout=min(timeout, READ_TIMEOUT_SECONDS), cancel=cancel, deadline=deadline) as resp:
            reader = getattr(resp, "read1", resp.read)
            while True:
                check()
                chunk = reader(64 * 1024)
                check()
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise ValueError("download exceeded the size limit")
                out.write(chunk)
        return written
