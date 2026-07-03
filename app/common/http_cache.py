"""In-memory HTTP response caches and small response-encoding helpers.

Extracted from ``drone_api.py`` so the caching primitives used by the request
handler live in one focused module. ``ExpiringLRUCache`` backs the image cache
(size- and TTL-bounded), ``ExpiringKeyCache`` is a lightweight TTL set used for
negative/"miss" caching, and the ``*_bytes``/``valid_segment`` helpers are the
tiny encoders the handler uses when writing responses.

Pure stdlib, no Drone-internal dependencies.
"""

import json
import time
from collections import OrderedDict
from threading import Lock
from typing import Dict, Optional


class ExpiringLRUCache:
    def __init__(self, ttl_seconds: int, max_items: int, max_bytes: int):
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = Lock()

    def _prune_expired_unlocked(self) -> None:
        now = time.time()
        expired_keys = [key for key, value in self._items.items() if value["expires_at"] <= now]
        for key in expired_keys:
            self.total_bytes -= self._items[key]["size"]
            del self._items[key]

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            self._prune_expired_unlocked()
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, data: bytes, meta: Optional[dict] = None) -> None:
        size = len(data)
        if size > self.max_bytes:
            return

        entry = {
            "data": data,
            "size": size,
            "meta": meta or {},
            "expires_at": time.time() + self.ttl_seconds,
        }

        with self._lock:
            old = self._items.pop(key, None)
            if old:
                self.total_bytes -= old["size"]

            self._items[key] = entry
            self._items.move_to_end(key)
            self.total_bytes += size

            while len(self._items) > self.max_items or self.total_bytes > self.max_bytes:
                _, oldest = self._items.popitem(last=False)
                self.total_bytes -= oldest["size"]


class ExpiringKeyCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: Dict[str, float] = {}
        self._lock = Lock()

    def has(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            expires_at = self._items.get(key)
            if not expires_at:
                return False
            if expires_at <= now:
                del self._items[key]
                return False
            return True

    def put(self, key: str) -> None:
        with self._lock:
            self._items[key] = time.time() + self.ttl_seconds


def json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, indent=2).encode("utf-8")


def html_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def valid_segment(value: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\x00" in value:
        raise ValueError("invalid path segment")
    return value
