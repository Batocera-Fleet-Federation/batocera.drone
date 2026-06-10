import unittest

import app.drone_api as drone_api


class AuthBlockerTest(unittest.TestCase):
    def setUp(self):
        drone_api._AUTH_401_BUCKETS.clear()
        drone_api._AUTH_BLOCKED_IPS.clear()

    def tearDown(self):
        drone_api._AUTH_401_BUCKETS.clear()
        drone_api._AUTH_BLOCKED_IPS.clear()

    def test_blocks_after_threshold_within_window(self):
        ip = "203.0.113.5"
        t = 1000.0
        # 4 failures within 60s -> not yet blocked.
        for i in range(4):
            self.assertFalse(drone_api.record_unauthorized_response(ip, now=t + i))
            self.assertFalse(drone_api.is_ip_blocked(ip, now=t + i))
        # 5th failure within the window -> blocked.
        self.assertTrue(drone_api.record_unauthorized_response(ip, now=t + 4))
        self.assertTrue(drone_api.is_ip_blocked(ip, now=t + 4))

    def test_failures_outside_window_do_not_accumulate(self):
        ip = "203.0.113.6"
        # Space failures > 60s apart: the sliding window never reaches 5.
        for i in range(10):
            drone_api.record_unauthorized_response(ip, now=i * 61.0)
        self.assertFalse(drone_api.is_ip_blocked(ip, now=10 * 61.0))

    def test_block_expires_after_duration(self):
        ip = "203.0.113.7"
        for i in range(5):
            drone_api.record_unauthorized_response(ip, now=1000.0 + i)
        self.assertTrue(drone_api.is_ip_blocked(ip, now=1004.0))
        # Still blocked just before 5 minutes elapse...
        self.assertTrue(drone_api.is_ip_blocked(ip, now=1004.0 + 299))
        # ...and unblocked at/after the duration.
        self.assertFalse(drone_api.is_ip_blocked(ip, now=1004.0 + 300))

    def test_loopback_is_never_blocked(self):
        for ip in ("127.0.0.1", "::1"):
            for i in range(10):
                self.assertFalse(drone_api.record_unauthorized_response(ip, now=2000.0 + i))
            self.assertFalse(drone_api.is_ip_blocked(ip, now=2010.0))

    def test_already_blocked_ip_does_not_extend_on_more_401s(self):
        ip = "203.0.113.8"
        for i in range(5):
            drone_api.record_unauthorized_response(ip, now=1000.0 + i)
        # Further 401s while blocked return False (no re-trigger) and don't push out expiry.
        self.assertFalse(drone_api.record_unauthorized_response(ip, now=1100.0))
        self.assertFalse(drone_api.is_ip_blocked(ip, now=1004.0 + 300))


if __name__ == "__main__":
    unittest.main()
