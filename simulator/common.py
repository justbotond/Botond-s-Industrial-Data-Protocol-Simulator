"""Shared helpers for simulator instances."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable


class CycleStats:
    """Fixed-rate update loop that tracks cycle statistics for the UI."""

    def __init__(self, name: str, interval_s: float, log: logging.Logger):
        self.name, self.interval, self.log = name, interval_s, log
        self.cycles = 0
        self.overruns = 0
        self.last_ms = 0.0
        self.max_ms = 0.0
        self.started = time.time()
        self._last_warn = 0.0

    async def run(self, step: Callable[[], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            t0 = loop.time()
            await step()
            self.cycles += 1
            self.last_ms = (loop.time() - t0) * 1000
            self.max_ms = max(self.max_ms, self.last_ms)
            next_t += self.interval
            delay = next_t - loop.time()
            if delay < 0:
                self.overruns += 1
                now = time.monotonic()
                if now - self._last_warn > 10:  # at most one warning per 10 s
                    self._last_warn = now
                    self.log.warning("%s: update cycle running late (%.0f ms behind, %d late cycles so far)",
                                     self.name, -delay * 1000, self.overruns)
                next_t = loop.time()
                delay = 0
            await asyncio.sleep(delay)

    def stats(self) -> dict:
        return {"cycles": self.cycles, "overruns": self.overruns, "last_cycle_ms": round(self.last_ms, 1),
                "max_cycle_ms": round(self.max_ms, 1), "interval_ms": int(self.interval * 1000),
                "uptime_s": int(time.time() - self.started)}
