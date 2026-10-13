"""Scalable tag sets: N values that are regenerated on every tick by a generator."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

GENERATORS = ("random", "random_walk", "sine", "ramp", "counter", "constant", "square")
DATA_TYPES = ("float32", "float64", "int16", "uint16", "int32", "uint32", "bool")

INT_RANGES = {
    "int16": (-32768, 32767),
    "uint16": (0, 65535),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
}


@dataclass
class TagSpec:
    """One group of identically-generated tags (e.g. 40,000 random floats)."""

    name: str = "Tag"
    count: int = 100
    data_type: str = "float32"
    generator: str = "random"
    min: float = 0.0
    max: float = 100.0
    period_s: float = 60.0  # for sine / ramp / square
    step: float = 1.0  # for random_walk / counter

    def validate(self) -> None:
        if self.count < 1:
            raise ValueError(f"{self.name}: count must be >= 1")
        if self.generator not in GENERATORS:
            raise ValueError(f"{self.name}: generator must be one of {GENERATORS}")
        if self.data_type not in DATA_TYPES:
            raise ValueError(f"{self.name}: data_type must be one of {DATA_TYPES}")
        if self.max < self.min:
            raise ValueError(f"{self.name}: max must be >= min")


class TagSet:
    """Holds the current values of a TagSpec and advances them each tick."""

    def __init__(self, spec: TagSpec, seed: int | None = None):
        spec.validate()
        self.spec = spec
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()
        n = spec.count
        # per-tag phase offsets so sine/ramp tags aren't all identical
        self.phase = [self.rng.random() for _ in range(n)]
        lo, hi = spec.min, spec.max
        if spec.generator in ("counter", "constant"):
            self.values = [lo] * n
        else:
            self.values = [self.rng.uniform(lo, hi) for _ in range(n)]
        self.values = self._cast(self.values)

    def names(self) -> list[str]:
        width = len(str(self.spec.count - 1))
        return [f"{self.spec.name}{i:0{width}d}" for i in range(self.spec.count)]

    def tick(self) -> list:
        s, rng, n = self.spec, self.rng, self.spec.count
        lo, hi, span = s.min, s.max, s.max - s.min
        g = s.generator
        if g == "random":
            v = [lo + span * rng.random() for _ in range(n)]
        elif g == "random_walk":
            v = [min(hi, max(lo, x + rng.uniform(-s.step, s.step))) for x in self.values]
        elif g == "sine":
            t = (time.monotonic() - self.t0) / s.period_s
            mid, amp = (hi + lo) / 2, span / 2
            v = [mid + amp * math.sin(2 * math.pi * (t + p)) for p in self.phase]
        elif g == "ramp":
            t = (time.monotonic() - self.t0) / s.period_s
            v = [lo + span * ((t + p) % 1.0) for p in self.phase]
        elif g == "square":
            t = (time.monotonic() - self.t0) / s.period_s
            v = [hi if (t + p) % 1.0 < 0.5 else lo for p in self.phase]
        elif g == "counter":
            v = [lo if x + s.step > hi else x + s.step for x in self.values]
        else:  # constant
            v = self.values
        self.values = self._cast(v)
        return self.values

    def _cast(self, v: list) -> list:
        dt = self.spec.data_type
        if dt == "bool":
            mid = (self.spec.min + self.spec.max) / 2
            return [x > mid if not isinstance(x, bool) else x for x in v]
        if dt in INT_RANGES:
            a, b = INT_RANGES[dt]
            return [min(b, max(a, int(round(x)))) for x in v]
        return [float(x) for x in v]
