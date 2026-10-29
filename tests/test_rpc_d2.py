# SPDX-License-Identifier: BSD-3-Clause
"""The D2 face of every RPC (DeckyKit STANDARD §2): {ok, error?, state?} for the Decky panel, no exception ever
reaches it, and the Desktop bridge turns D2 back into the native app's HTTP contract (200 {result} / 409 {error})."""
import json
import unittest
import unittest.mock
from unittest.mock import AsyncMock, Mock

import support
import main
from desktop_bridge import DesktopBridge, Refused
import test_desktop_bridge


class AdapterTests(unittest.TestCase):
    def test_a_payload_is_the_state_unchanged(self):
        for payload in ({'stored': True}, {'settings': {}}, True, False, [1], 'text'):
            self.assertEqual(main._d2(payload), {'ok': True, 'state': payload})
        self.assertEqual(main._d2(None), {'ok': True})

    def test_an_answer_already_in_d2_passes_unchanged(self):
        for answer in ({'ok': True, 'state': {'enabled': True}}, {'ok': True},
                       {'ok': False, 'error': 'Unsupported button or action.', 'state': {'enabled': False}},
                       {'ok': False, 'error': 'Refused.'}):
            self.assertIs(main._d2(answer), answer)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        plugin = main.Plugin()
        plugin._check_guard = AsyncMock()
        plugin._startup.set()  # RPCs without _main
        plugin._module_state = {name: {'enabled': True} for name in main.MODULE_NAMES}
        plugin._module_health = {name: {'ready': True} for name in main.MODULE_NAMES}
        return plugin

    def test_every_public_rpc_has_the_d2_face_over_its_guarded_call(self):
        public = [name for name, fn in vars(main.Plugin).items() if not name.startswith('_') and callable(fn)]
        self.assertTrue(public)
        for name in public:
            self.assertTrue(hasattr(getattr(main.Plugin, name), 'legacy'), name)

    async def test_a_refusal_written_for_users_is_shown_as_it_is(self):
        plugin = self.plugin()
        plugin._module_state['rgb'] = {'enabled': False}
        self.assertEqual(await plugin.rgb_get_status(),
                         {'ok': False, 'error': 'This module is disabled. Enable it in Manage modules.'})
        plugin._rgb.set_color = AsyncMock(side_effect=ValueError('Hue must be 0-360.'))
        plugin._module_state['rgb'] = {'enabled': True}
        self.assertEqual(await plugin.rgb_set_color(400, 50), {'ok': False, 'error': 'Hue must be 0-360.'})

    async def test_an_unexpected_exception_names_only_its_type(self):
        plugin = self.plugin()
        plugin._fan.get_state = AsyncMock(side_effect=KeyError('/home/deck/private'))
        with self.assertLogs('decky-test', 'ERROR') as logs:  # the logger main's boundary was given
            answer = await plugin.fan_get_state()
        self.assertEqual(answer, {'ok': False, 'error': 'Unexpected error (KeyError). Details are in the Decky log.'})
        # Logged with its frames (as a traceback, or as file/line frames once diagnostics filter the logger).
        self.assertIn('KeyError', '\n'.join(logs.output))

    async def test_payloads_and_unguarded_calls_answer_d2(self):
        plugin = self.plugin()
        plugin._fan.get_state = AsyncMock(return_value={'settings': {'revision': 3}})
        self.assertEqual(await plugin.fan_get_state(), {'ok': True, 'state': {'settings': {'revision': 3}}})
        version = await plugin.get_version()
        self.assertTrue(version['ok'])
        self.assertIn('version', version['state'])


class LoggedOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """_logged_rpc reads the verdict of a D2 answer (ok); a plain payload says neither."""
    async def outcomes(self, answer):
        events = []
        async def change(self):
            return answer
        with unittest.mock.patch.object(main.diagnostics, 'event', side_effect=lambda *a, **k: events.append((a[1], k))):
            self.assertIs(await main._logged_rpc(change, 'remap_set_enabled')(None), answer)
        return [(name, fields.get('outcome'), fields.get('reason')) for name, fields in events if name != 'rpc_started']

    async def test_answers_are_classified_by_their_verdict(self):
        for answer, expected in (
                ({'ok': True, 'state': {}}, ('rpc_completed', 'succeeded', None)),
                ({'ok': False, 'error': 'Busy.'}, ('rpc_failed', 'not_confirmed', 'backend_reported_failure')),
                # WiFi's D2 refusal keeps its code as the state's error.
                ({'ok': False, 'error': 'No WiFi.', 'state': {'error': 'no_wifi'}}, ('rpc_completed', 'refused', 'no_wifi')),
                ({'state': 1}, ('rpc_completed', 'returned', None)),
                ({'enabled': True}, ('rpc_completed', 'returned', None))):
            with self.subTest(answer=answer):
                self.assertEqual(await self.outcomes(answer), [expected])


class Fake:
    """Answers the way main.Plugin's D2 face does."""
    async def get_version(self):
        return {'ok': True, 'state': {'version': 'test'}}

    async def change(self, value):
        return ({'ok': True, 'state': {'saved': value}} if value == 'good'
                else {'ok': False, 'error': 'Refused for users.', 'state': {'enabled': False}}
                if value == 'with state' else {'ok': False, 'error': 'Refused for users.'})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    """The native app keeps its HTTP contract over D2."""
    request = test_desktop_bridge.DesktopBridgeTests.request

    async def asyncSetUp(self):
        self.bridge = DesktopBridge(Fake(), ['get_version', 'change'])
        await self.bridge.start()

    async def asyncTearDown(self):
        await self.bridge.close()

    async def answer(self, value):
        raw = await self.request('change', [value])
        head, _, body = raw.partition(b'\r\n\r\n')
        return head.split(b' ')[1], json.loads(body)

    async def test_d2_becomes_the_native_http_contract(self):
        self.assertEqual(await self.answer('good'), (b'200', {'result': {'saved': 'good'}}))
        # Every refusal is the 409 the app shows, also one that carries the state for the Decky panel.
        self.assertEqual(await self.answer('with state'), (b'409', {'error': 'Refused for users.'}))
        self.assertEqual(await self.answer('bare'), (b'409', {'error': 'Refused for users.'}))

    async def test_a_refusal_is_a_runtime_error_for_the_bridge_and_not_logged_twice(self):
        self.assertTrue(issubclass(Refused, RuntimeError))
        with self.assertRaises(Refused):
            await self.bridge.dispatch('change', ['bare'])


if __name__ == '__main__':
    unittest.main()
