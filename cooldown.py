import threading
import time


class CooldownCache:
    def __init__(self, seconds: int):
        self._cache: dict[str, float] = {}
        self._lock = threading.Lock()
        self._seconds = seconds

    def check_and_set(self, key: str) -> bool:
        """Returns True and records timestamp if the cooldown has elapsed."""
        now = time.time()
        with self._lock:
            if now - self._cache.get(key, 0) > self._seconds:
                self._cache[key] = now
                return True
        return False
