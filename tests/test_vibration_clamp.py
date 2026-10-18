# SPDX-License-Identifier: BSD-3-Clause
"""Intensity levels are clamped to the driver's 0..3 range, for saved profiles and for live requests."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import support  # noqa: F401 - installs the fake decky module
import vibration_backend as vibe


class IntensityClampTests(unittest.TestCase):
    def test_saved_profile_levels_are_clamped(self):
        for raw, expected in ((9, 3), (4, 3), (3, 3), (0, 0), (-5, 0)):
            with self.subTest(raw=raw):
                profile = vibe._coerce_profile({vibe.PKEY_LEVEL: raw, vibe.PKEY_TP_INT: raw})
                self.assertEqual((profile[vibe.PKEY_LEVEL], profile[vibe.PKEY_TP_INT]), (expected, expected))

    def test_live_requests_are_clamped_before_they_are_stored(self):
        for method, key in (('set_intensity', vibe.PKEY_LEVEL), ('set_touchpad_intensity', vibe.PKEY_TP_INT)):
            for raw, expected in ((9, 3), (4, 3), (-5, 0)):
                with self.subTest(method=method, raw=raw), patch.object(vibe.Plugin, '_set_field', new_callable=AsyncMock) as store:
                    asyncio.run(getattr(vibe.Plugin(), method)(raw))
                    self.assertEqual(store.await_args.args[:2], (key, expected))


if __name__ == '__main__':
    unittest.main()
