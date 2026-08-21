import time


class CooldownGate:
    """Per-key rate gate: allow() returns True at most once per window per key.
    In-memory, single-worker safe (no awaits between read and write), bounded
    memory via clear-on-overflow — same properties as the dict it replaces."""

    def __init__(self, window_s: float, max_entries: int = 5000) -> None:
        self.window_s = window_s
        self.max_entries = max_entries
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, float("-inf")) < self.window_s:
            return False
        if len(self._last) >= self.max_entries:
            self._last.clear()
        self._last[key] = now
        return True