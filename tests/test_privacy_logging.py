import unittest

from app.common.privacy_logging import pseudonymize_ip, sanitize_request_line


class PrivacyLoggingTests(unittest.TestCase):
    def test_ip_pseudonym_is_stable_within_day_without_disclosing_address(self) -> None:
        first = pseudonymize_ip("203.0.113.9", day="2026-07-28")
        second = pseudonymize_ip("203.0.113.9", day="2026-07-28")
        other = pseudonymize_ip("203.0.113.10", day="2026-07-28")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^ip#[0-9a-f]{12}$")
        self.assertNotIn("203.0.113.9", first)

    def test_request_line_drops_query_and_fragment(self) -> None:
        self.assertEqual(
            sanitize_request_line("GET /search?q=secret-token#results HTTP/1.1"),
            "GET /search HTTP/1.1",
        )

    def test_request_line_removes_control_characters(self) -> None:
        self.assertEqual(sanitize_request_line("GET /safe\r\nInjected: value HTTP/1.1"), "GET /safe Injected: value HTTP/1.1")


if __name__ == "__main__":
    unittest.main()
