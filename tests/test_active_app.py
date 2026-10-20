import unittest

import support  # Install isolated Decky stubs before backend imports.
from active_app import normalise_app_id


class NormaliseAppIdTests(unittest.TestCase):
    def test_accepts_steam_ids_and_short_names(self):
        self.assertEqual(normalise_app_id('123'), '123')
        self.assertEqual(normalise_app_id(123), '123')
        self.assertEqual(normalise_app_id('my-game_1.0'), 'my-game_1.0')
        self.assertEqual(normalise_app_id('0'), '0')
        self.assertEqual(normalise_app_id('a' * 64), 'a' * 64)

    def test_rejects_unsafe_or_wrongly_typed_ids(self):
        for value in ('a' * 65, 'a b', '../x', 'x\n', '12\x00', 1.5, True, b'1', [], {}):
            with self.subTest(value=value):
                self.assertIsNone(normalise_app_id(value))
                self.assertIsNone(normalise_app_id(value, allow_empty=True))

    def test_empty_is_refused_unless_allowed(self):
        for value in (None, ''):
            self.assertIsNone(normalise_app_id(value))
            self.assertEqual(normalise_app_id(value, allow_empty=True), '')

    def test_default_app_can_be_refused(self):
        self.assertIsNone(normalise_app_id('0', allow_default=False))
        self.assertIsNone(normalise_app_id(0, allow_default=False))
        self.assertEqual(normalise_app_id('1', allow_default=False), '1')


if __name__ == '__main__':
    unittest.main()
