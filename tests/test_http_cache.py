"""Tests for the in-memory HTTP caches and response-encoding helpers.

``common/http_cache.py`` backs the request handler's image cache (``ExpiringLRUCache``
— TTL + item-count + byte-budget bounded) and its negative/"miss" cache
(``ExpiringKeyCache``), plus the tiny ``*_bytes`` / ``valid_segment`` encoders. These
primitives were extracted untested; this locks their eviction, expiry, and byte
accounting so a future tweak can't silently unbound the cache or leak the byte total.
"""
import unittest
from unittest import mock

from app.common import http_cache
from app.common.http_cache import (
    ExpiringKeyCache,
    ExpiringLRUCache,
    html_bytes,
    json_bytes,
    valid_segment,
)


class _Clock:
    """Controllable stand-in for ``time.time()`` inside the module."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class ExpiringLRUCacheTests(unittest.TestCase):
    def test_put_get_roundtrip_and_byte_accounting(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=10, max_bytes=1000)
        cache.put("k", b"hello", meta={"content_type": "image/png"})
        entry = cache.get("k")
        self.assertEqual(entry["data"], b"hello")
        self.assertEqual(entry["size"], 5)
        self.assertEqual(entry["meta"], {"content_type": "image/png"})
        self.assertEqual(cache.total_bytes, 5)

    def test_missing_key_returns_none(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=10, max_bytes=1000)
        self.assertIsNone(cache.get("absent"))

    def test_oversized_item_is_rejected_not_stored(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=10, max_bytes=10)
        cache.put("big", b"x" * 20)
        self.assertIsNone(cache.get("big"))
        self.assertEqual(cache.total_bytes, 0)

    def test_evicts_by_item_count(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=2, max_bytes=10_000)
        cache.put("a", b"1")
        cache.put("b", b"2")
        cache.put("c", b"3")  # exceeds max_items -> oldest ("a") evicted
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))
        self.assertEqual(cache.total_bytes, 2)

    def test_get_refreshes_recency_so_lru_victim_is_least_recently_used(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=2, max_bytes=10_000)
        cache.put("a", b"1")
        cache.put("b", b"2")
        self.assertIsNotNone(cache.get("a"))  # touch "a" -> "b" is now the LRU victim
        cache.put("c", b"3")
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))

    def test_evicts_by_byte_budget(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=100, max_bytes=100)
        cache.put("a", b"x" * 60)
        cache.put("b", b"y" * 60)  # 120 > 100 budget -> evict oldest ("a")
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        self.assertEqual(cache.total_bytes, 60)

    def test_overwrite_same_key_updates_size_accounting(self):
        cache = ExpiringLRUCache(ttl_seconds=100, max_items=10, max_bytes=1000)
        cache.put("k", b"x" * 10)
        cache.put("k", b"y" * 30)
        self.assertEqual(cache.get("k")["data"], b"y" * 30)
        self.assertEqual(cache.total_bytes, 30)  # old 10 subtracted, not leaked

    def test_ttl_expiry_prunes_entry_and_bytes(self):
        clock = _Clock(1000.0)
        with mock.patch.object(http_cache.time, "time", clock):
            cache = ExpiringLRUCache(ttl_seconds=50, max_items=10, max_bytes=1000)
            cache.put("k", b"data")
            self.assertIsNotNone(cache.get("k"))
            clock.now = 1000.0 + 51  # past ttl
            self.assertIsNone(cache.get("k"))
            self.assertEqual(cache.total_bytes, 0)


class ExpiringKeyCacheTests(unittest.TestCase):
    def test_unknown_key_is_absent(self):
        self.assertFalse(ExpiringKeyCache(ttl_seconds=60).has("nope"))

    def test_put_then_has(self):
        cache = ExpiringKeyCache(ttl_seconds=60)
        cache.put("seen")
        self.assertTrue(cache.has("seen"))

    def test_expires_after_ttl(self):
        clock = _Clock(500.0)
        with mock.patch.object(http_cache.time, "time", clock):
            cache = ExpiringKeyCache(ttl_seconds=30)
            cache.put("seen")
            self.assertTrue(cache.has("seen"))
            clock.now = 500.0 + 31
            self.assertFalse(cache.has("seen"))
            self.assertNotIn("seen", cache._items)  # pruned on read


class EncodingHelperTests(unittest.TestCase):
    def test_valid_segment_accepts_normal_names(self):
        for value in ("game.zip", "snes", "Super Mario World.sfc"):
            self.assertEqual(valid_segment(value), value)

    def test_valid_segment_rejects_traversal_and_control(self):
        for value in ("", ".", "..", "a/b", "a\x00b"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    valid_segment(value)

    def test_json_bytes_roundtrips(self):
        import json

        out = json_bytes({"a": 1, "b": [2, 3]})
        self.assertIsInstance(out, bytes)
        self.assertEqual(json.loads(out.decode("utf-8")), {"a": 1, "b": [2, 3]})

    def test_html_bytes_is_utf8(self):
        self.assertEqual(html_bytes("<p>café</p>"), "<p>café</p>".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
