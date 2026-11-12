"""Согласование часов Quest (домен mono) и ПК (perf_counter) по NTP-подобному обмену.

Модель: mono = pc + theta(pc),  theta(pc) = a + b * (pc - pc0).
Оценка по нижней огибающей RTT: в каждом окне берётся обмен с минимальным RTT
(минимальная очередь в сети/планировщике), по этим точкам строится МНК-прямая.
Неопределённость: RTT_min/2 (граница асимметрии канала) и разброс остатков прямой.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass
class SyncSample:
    t1: float  # ПК, отправка запроса, с
    t2: float  # Quest mono, приём, с
    t3: float  # Quest mono, отправка ответа, с
    t4: float  # ПК, приём ответа, с

    @property
    def rtt(self) -> float:
        return (self.t4 - self.t1) - (self.t3 - self.t2)

    @property
    def offset(self) -> float:
        """theta: mono - pc."""
        return ((self.t2 - self.t1) + (self.t3 - self.t4)) / 2.0

    @property
    def pc_mid(self) -> float:
        return (self.t1 + self.t4) / 2.0


@dataclass
class ClockModel:
    a: float  # смещение theta в pc0, с
    b: float  # дрейф, с/с
    pc0: float
    n_samples: int
    n_used: int
    rtt_min: float
    rtt_median: float
    residual_std: float
    span_s: float

    def theta(self, pc: float) -> float:
        return self.a + self.b * (pc - self.pc0)

    def mono_to_pc(self, mono: float) -> float:
        # mono = pc + a + b*(pc - pc0)  =>  pc = (mono - a + b*pc0) / (1 + b)
        return (mono - self.a + self.b * self.pc0) / (1.0 + self.b)

    @property
    def uncertainty_s(self) -> float:
        return self.rtt_min / 2.0 + 2.0 * self.residual_std

    def to_dict(self) -> dict:
        return {
            "offset_mono_minus_pc_s": self.a,
            "drift_ppm": self.b * 1e6,
            "pc0_s": self.pc0,
            "n_samples": self.n_samples,
            "n_used": self.n_used,
            "rtt_min_ms": self.rtt_min * 1e3,
            "rtt_median_ms": self.rtt_median * 1e3,
            "residual_std_ms": self.residual_std * 1e3,
            "uncertainty_ms": self.uncertainty_s * 1e3,
            "span_s": self.span_s,
        }


def fit(samples: list[SyncSample], window_s: float = 2.0) -> ClockModel | None:
    good = [s for s in samples if s.rtt >= 0 and math.isfinite(s.offset)]
    if not good:
        return None
    good.sort(key=lambda s: s.pc_mid)
    rtts = [s.rtt for s in good]
    start = good[0].pc_mid
    span = good[-1].pc_mid - start

    # Нижняя огибающая RTT по окнам
    best: dict[int, SyncSample] = {}
    for s in good:
        k = int((s.pc_mid - start) // window_s)
        if k not in best or s.rtt < best[k].rtt:
            best[k] = s
    used = list(best.values())

    pc0 = start
    xs = [s.pc_mid - pc0 for s in used]
    ys = [s.offset for s in used]
    if len(used) >= 3 and span >= 2 * window_s:
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
        a = my - b * mx
    else:
        b = 0.0
        a = min(used, key=lambda s: s.rtt).offset
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    rstd = statistics.pstdev(resid) if len(resid) > 1 else 0.0
    return ClockModel(a, b, pc0, len(good), len(used), min(rtts), statistics.median(rtts), rstd, span)


@dataclass
class OvrMonoRelation:
    """Связь домена ovr (SampleTimeStamp, GetTimeInSeconds) с mono (Stopwatch) по парам из HAND_FRAME."""
    median_offset: float  # ovr - mono, с
    spread: float  # max - min, с
    drift_ppm: float
    n: int

    def ovr_to_mono(self, ovr: float) -> float:
        return ovr - self.median_offset

    def to_dict(self) -> dict:
        return {
            "ovr_minus_mono_median_s": self.median_offset,
            "ovr_minus_mono_spread_ms": self.spread * 1e3,
            "drift_ppm": self.drift_ppm,
            "n": self.n,
        }


def fit_ovr_mono(pairs: list[tuple[float, float]]) -> OvrMonoRelation | None:
    """pairs: (ovr_now, mono_now)."""
    if not pairs:
        return None
    d = [o - m for o, m in pairs]
    drift = 0.0
    if len(pairs) > 10:
        ms = [m for _, m in pairs]
        mx, my = statistics.fmean(ms), statistics.fmean(d)
        sxx = sum((x - mx) ** 2 for x in ms)
        if sxx > 0:
            drift = sum((x - mx) * (y - my) for x, y in zip(ms, d)) / sxx * 1e6
    return OvrMonoRelation(statistics.median(d), max(d) - min(d), drift, len(d))
