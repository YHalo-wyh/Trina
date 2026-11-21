from __future__ import annotations

import unittest

from backoff import retry_delays
from headers import normalize_headers


class RuntimeUtilitiesTests(unittest.TestCase):
    def test_headers_are_trimmed_lowered_and_blank_names_ignored(self):
        self.assertEqual(
            normalize_headers([(" Content-Type ", "json"), ("   ", "drop"), ("X-ID", "7")]),
            {"content-type": "json", "x-id": "7"},
        )

    def test_duplicate_headers_keep_original_value_order(self):
        self.assertEqual(
            normalize_headers([("X-Tag", "first"), ("x-tag", "second"), (" X-TAG ", "third")]),
            {"x-tag": "first, second, third"},
        )

    def test_retry_delays_are_exponential_and_capped(self):
        self.assertEqual(retry_delays(0.5, 3.0, 5), [0.5, 1.0, 2.0, 3.0, 3.0])

    def test_retry_delays_handle_zero_attempts(self):
        self.assertEqual(retry_delays(1.0, 8.0, 0), [])


if __name__ == "__main__":
    unittest.main()
