from __future__ import annotations

import unittest

import app


class MapperDerivationTests(unittest.TestCase):
    def test_v1_test_vector(self) -> None:
        app_id = "app-00000000-0000-4000-8000-000000000001"
        self.assertEqual(
            app.derive_key_from_secret(bytes(32), app_id),
            "sk-EJ-FFUVNQsj-9lUcg5Epx4JKp-0naYCllRGb4bY7-8U",
        )

    def test_app_ids_are_not_interchangeable(self) -> None:
        secret = bytes(range(32))
        first = app.derive_key_from_secret(secret, "app-00000000-0000-4000-8000-000000000001")
        second = app.derive_key_from_secret(secret, "app-00000000-0000-4000-8000-000000000002")
        self.assertNotEqual(first, second)

    def test_key_has_litellm_prefix_and_expected_length(self) -> None:
        key = app.derive_key_from_secret(bytes(range(32)), "app-00000000-0000-4000-8000-000000000001")
        self.assertTrue(key.startswith("sk-"))
        self.assertGreaterEqual(len(key), 40)


if __name__ == "__main__":
    unittest.main()
