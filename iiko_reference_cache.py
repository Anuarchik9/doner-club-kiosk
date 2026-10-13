"""Bounded, single-flight cache for iiko configuration, never orders or balances."""
import hashlib
import json
import math
import threading
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime

import requests


REFERENCE_FIELDS = {
    "/api/1/terminal_groups": "terminalGroups",
    "/api/1/payment_types": "paymentTypes",
    "/api/1/discounts": "discounts",
}


def credential_key(*values):
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


def _snapshot(response, data=None):
    """Keep response data without retaining the request's authorization header."""
    result = requests.Response()
    status = response.status_code
    result.status_code = status if isinstance(status, int) else (200 if response.ok else 502)
    if isinstance(response.headers, Mapping):
        result.headers.update(response.headers)
    result._content = (json.dumps(data, ensure_ascii=False).encode("utf-8")
                       if data is not None else response.content)
    result.encoding = "utf-8"
    result.url = response.url if isinstance(response.url, str) else "https://api-ru.iiko.services"
    return result


class ReferenceCache:
    def __init__(self, ttl=600, max_entries=64, clock=time.monotonic, wall_clock=time.time):
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self.wall_clock = wall_clock
        self._lock = threading.RLock()
        self._flights = [threading.Lock() for _ in range(32)]
        self.clear()

    def clear(self):
        with self._lock:
            self._entries = {}
            self._cooldowns = {}
            self._failures = {}
            self._calls = {}
            self._hits = {}

    def _retry_after(self, response):
        value = response.headers.get("Retry-After")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self.wall_clock()
            except (TypeError, ValueError, OverflowError):
                seconds = 60
        if not math.isfinite(seconds):
            seconds = 60
        return max(1, seconds)

    def _prune(self, now):
        for mapping in (self._entries, self._cooldowns, self._failures):
            for key, (until, _) in list(mapping.items()):
                if until <= now:
                    mapping.pop(key, None)
            while len(mapping) > self.max_entries:
                mapping.pop(min(mapping, key=lambda key: mapping[key][0]))

    def get(self, path, payload, credential, loader):
        if path not in REFERENCE_FIELDS:
            raise ValueError("Only iiko reference methods can be cached")
        key = (credential, path, json.dumps(payload, sort_keys=True, separators=(",", ":")))
        method_key = (credential, path)
        flight = self._flights[int(hashlib.sha256(repr(key).encode()).hexdigest(), 16) % len(self._flights)]
        with flight:
            now = self.clock()
            with self._lock:
                self._prune(now)
                entry = self._entries.get(key)
                if entry:
                    self._hits[path] = self._hits.get(path, 0) + 1
                    return _snapshot(entry[1])
                cooldown = self._cooldowns.get(method_key)
                if cooldown:
                    result = _snapshot(cooldown[1])
                    result.headers["Retry-After"] = str(max(1, math.ceil(cooldown[0] - now)))
                    return result
                failure = self._failures.get(key)
                if failure:
                    if isinstance(failure[1], type):
                        raise failure[1]("iiko reference data temporarily unavailable")
                    return _snapshot(failure[1])
                self._calls[path] = self._calls.get(path, 0) + 1

            try:
                response = loader()
                status = response.status_code
                if status == 429:
                    with self._lock:
                        self._cooldowns[method_key] = (self.clock() + self._retry_after(response), _snapshot(response))
                        self._prune(self.clock())
                    return response
                if not response.ok:
                    if isinstance(status, int) and status >= 500:
                        with self._lock:
                            self._failures[key] = (self.clock() + 30, _snapshot(response))
                            self._prune(self.clock())
                    return response
                data = response.json()
                # Malformed/partial responses must never become a valid payment mapping.
                if not isinstance(data, dict) or not isinstance(data.get(REFERENCE_FIELDS[path]), list):
                    raise ValueError("Incomplete iiko reference response")
            except (requests.Timeout, requests.ConnectionError, ValueError) as error:
                failure_type = (requests.Timeout if isinstance(error, requests.Timeout) else
                                requests.ConnectionError if isinstance(error, requests.ConnectionError) else ValueError)
                with self._lock:
                    self._failures[key] = (self.clock() + 30, failure_type)
                    self._prune(self.clock())
                raise

            with self._lock:
                self._entries[key] = (self.clock() + self.ttl, _snapshot(response, data))
                self._failures.pop(key, None)
                self._prune(self.clock())
            return _snapshot(response, data)

    def diagnostics(self):
        with self._lock:
            self._prune(self.clock())
            return {"ttlSeconds": self.ttl, "entries": len(self._entries),
                    "upstreamRequests": dict(self._calls), "cacheHits": dict(self._hits),
                    "activeCooldowns": len(self._cooldowns)}
