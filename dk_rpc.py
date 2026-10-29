# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/backend/dk_rpc.py): change it there, then `kit sync`.

"""The RPC contract (DeckyKit D2): every method the panel calls returns {"ok": bool, "error"?: str, "state"?: ...}
and no exception ever reaches the frontend.

    _ready = asyncio.Event()
    rpc = dk_rpc.boundary(decky.logger, _ready)

    class Plugin:
        async def _main(self):
            try: ...
            finally: _ready.set()   # also after a failure: calls then answer instead of waiting

        @rpc
        async def get_state(self): return dk_rpc.ok(state)

Sources: FSR4Injector/DeckyReshade main.py (every RPC waits for _ready), DeckyEQ deckyeq/storage.py (errors shown
to users name no path), DeckyLS (first version of the contract), Ayaneo3Companion quality_runtime.py (refusals
raised as exceptions whose text is for the user; a polled call that keeps failing logs once, not every few
seconds); the other plugins had three different error shapes, which D2 replaces.
Guarantees: a decorated method runs only after `ready` is set, and answers fail() instead of hanging when it is not
set within `wait` seconds; an unexpected exception is logged with its traceback and becomes fail() with a short
message that carries only the exception's type (str(e) may hold a path, and so the user's name); an exception of
a `shown` type (the plugin's own refusals, written for users: e.g. RuntimeError("This device is not an AYANEO 3"))
or of the plugin's own subclass of one becomes fail(str(e)) and one warning line without a traceback, while
Python's own subclasses (JSONDecodeError, UnicodeDecodeError, NotImplementedError...) stay unexpected errors; a method that keeps failing the same way logs it
once until it succeeds again (the panel polls); fail() and ok() build the only two shapes; asyncio.CancelledError is not swallowed (Decky's unload must still cancel); the method
keeps its name and signature for Decky's dispatch and for DeckyKit's overview.
Deliberately not here: argument validation (each method checks its own input), timeouts of the work itself (a
method that can hang uses dk_process/dk_net deadlines).
"""

import asyncio
import functools
import sys

WAIT = 60  # seconds a call waits for _main


def ok(state=None, **extra):
    """A successful answer, with the new state when there is one."""
    return {"ok": True, **({"state": state} if state is not None else {}), **extra}


def fail(error, state=None, **extra):
    """A refused or failed call; error is shown to the user (English, no traceback, no path)."""
    return {"ok": False, "error": str(error), **({"state": state} if state is not None else {}), **extra}


def _refusal(error, shown):
    """A shown type itself, or a subclass the plugin defined; a subclass from Python itself means a bug."""
    kind = type(error)
    return kind in shown or (isinstance(error, shown)
                             and kind.__module__.partition(".")[0] not in sys.stdlib_module_names)


def boundary(logger, ready=None, wait=WAIT, shown=()):
    """Decorator for the public RPC methods: waits for ready (an asyncio.Event set at the end of _main), then turns
    an unexpected exception into fail() and logs it with its traceback. shown: exception types whose message is
    written for the user (answered as it is, logged without a traceback)."""
    shown = tuple(shown)

    def wrap(method):
        last = [None]  # the failure logged last: a method that keeps failing the same way logs it once

        def log(level, text, key, **details):
            if last[0] != key:
                last[0] = key
                level(text, **details)

        @functools.wraps(method)
        async def call(self, *args, **kwargs):
            if ready is not None and not ready.is_set():
                try:
                    await asyncio.wait_for(ready.wait(), wait)
                except asyncio.TimeoutError:
                    logger.error(f"{method.__name__}: the plugin did not finish starting within {wait} s")
                    return fail("The plugin is still starting or failed to start. Details are in the Decky log.")
            try:
                result = await method(self, *args, **kwargs)
            except Exception as e:  # the RPC boundary: logged with its traceback, never sent on to the panel
                if _refusal(e, shown):
                    log(logger.warning, f"{method.__name__} refused: {e}", (type(e), str(e)))
                    return fail(e)
                log(logger.error, f"{method.__name__} failed", (type(e), str(e)), exc_info=True)
                return fail(f"Unexpected error ({type(e).__name__}). Details are in the Decky log.")
            last[0] = None
            return result
        return call
    return wrap
