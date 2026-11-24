"""The analysis pipeline: resample, segment, cluster, collect events, budget and warnings."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .battery import DEFAULT_DOD, DEFAULT_SELF_DISCHARGE, BatteryLife, battery_life
from .cluster import DEFAULT_MAX_STATES, cluster_levels
from .segment import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_PENALTY,
    Segment,
    candidate_positions,
    merge_short,
    noise_sigma,
    pelt,
    segments_from_changes,
)
from .spectral import autocorrelation, dominant_period, parabolic_peak
from .states import StateSpec, default_names, format_ma
from .trace import DEFAULT_GAP_FACTOR, Gap, Trace, Uniform, estimate_lsb, median, resample

SECONDS_PER_DAY = 86_400.0
ACF_MAX_POINTS = 1 << 15
OUTLIER_Z = 4.0  # robust z-score (MAD based) above which an event is an outlier ...
OUTLIER_RATIO = 1.5  # ... and it must also be this many times the median


@dataclass
class Options:
    resample_dt: float | None = None
    gap_factor: float = DEFAULT_GAP_FACTOR
    penalty: float = DEFAULT_PENALTY
    min_samples: int = DEFAULT_MIN_SAMPLES
    max_states: int = DEFAULT_MAX_STATES
    voltage_v: float | None = None  # supply voltage when the log has no voltage column
    capacity_mah: float | None = None
    depth_of_discharge: float = DEFAULT_DOD
    self_discharge_per_month: float = DEFAULT_SELF_DISCHARGE
    min_burst_s: float | None = None
    spec: StateSpec | None = None

    def apply_spec(self) -> None:
        """Let the ``[analysis]`` table of a states file override the defaults."""
        if self.spec is None:
            return
        if self.spec.penalty is not None:
            self.penalty = self.spec.penalty
        if self.spec.min_samples is not None:
            self.min_samples = self.spec.min_samples
        if self.spec.max_states is not None:
            self.max_states = self.spec.max_states


@dataclass
class Event:
    index: int
    state: str
    start: float
    end: float
    mean_ma: float
    charge_mah: float
    energy_mwh: float | None
    clipped: bool  # touches the start/end of the log or a gap: duration is a lower bound
    outlier: str | None = None
    run: int = 0  # index of the gap-free stretch of the log the event is in

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class StateStats:
    name: str
    level_ma: float
    voltage_v: float | None
    base: bool
    events: int
    time_s: float
    time_share: float
    charge_mah: float
    charge_share: float
    energy_mwh: float | None
    mean_duration_s: float | None
    median_duration_s: float | None
    max_duration_s: float | None
    median_period_s: float | None
    charge_per_event_mah: float
    mah_per_day: float
    mwh_per_day: float | None
    outliers: int


@dataclass
class Wake:
    start: float
    end: float
    charge_mah: float
    states: list[str]
    clipped: bool
    outlier: str | None = None
    run: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Warning_:
    code: str
    message: str


@dataclass
class Cycle:
    acf_period_s: float | None
    acf_peak: float | None
    median_wake_interval_s: float | None
    wakes: int


@dataclass
class Result:
    trace: Trace
    uniform: Uniform
    options: Options
    native_dt: float
    lsb_ma: float | None
    sigma: float
    beta: float
    segments: int
    folded: int
    excursions: int
    states: list[StateStats]
    events: list[Event]
    wakes: list[Wake]
    cycle: Cycle
    covered_s: float
    charge_mah: float
    energy_mwh: float | None
    mean_ma: float
    mean_voltage_v: float | None
    voltage_source: str
    mah_per_day: float
    mwh_per_day: float | None
    battery: BatteryLife | None
    warnings: list[Warning_] = field(default_factory=list)

    @property
    def base_state(self) -> StateStats:
        return next(s for s in self.states if s.base)

    def state(self, name: str) -> StateStats:
        for s in self.states:
            if s.name == name:
                return s
        raise KeyError(name)

    def to_json(self) -> dict[str, Any]:
        return result_json(self)


def _robust_outliers(values: list[float]) -> list[bool]:
    """Flag values far above the median: robust z > OUTLIER_Z and > OUTLIER_RATIO x median."""
    if len(values) < 5:
        return [False] * len(values)
    med = median(values)
    mad = median([abs(v - med) for v in values])
    scale = 1.4826 * mad
    flags = []
    for v in values:
        z = (v - med) / scale if scale > 0 else (math.inf if v > med else 0.0)
        flags.append(z > OUTLIER_Z and v > OUTLIER_RATIO * med)
    return flags


def _intervals(starts: list[float]) -> list[float]:
    return [b - a for a, b in itertools.pairwise(starts)]


def analyze(trace: Trace, options: Options | None = None) -> Result:
    opts = options or Options()
    opts.apply_spec()
    u = resample(trace, opts.resample_dt, opts.gap_factor)
    native_dt = median([b - a for a, b in zip(trace.t, trace.t[1:], strict=False)])
    dt = u.dt
    lsb = estimate_lsb(trace.current_ma)
    floor = lsb if lsb is not None else 1e-3
    xs = u.current_ma
    y = [math.nan if math.isnan(x) else math.log10(max(x, 0.0) + floor) for x in xs]
    runs = u.valid_runs()
    if not runs:
        raise ValueError("no valid samples")
    sigma = noise_sigma([y[a:b] for a, b in runs])
    n_valid = sum(b - a for a, b in runs)
    beta = 2.0 * opts.penalty * math.log(max(n_valid, 2))
    quant = lsb / math.sqrt(12.0) if lsb is not None else 0.0

    # Prefix sums of current and power over the grid (each grid sample stands for dt seconds).
    pre_i = [0.0] * (len(xs) + 1)
    for k, x in enumerate(xs):
        pre_i[k + 1] = pre_i[k] + (0.0 if math.isnan(x) else x)
    volts = u.voltage_v
    have_v = volts is not None and any(not math.isnan(v) for v in volts)
    pre_p: list[float] | None = None
    pre_v: list[float] | None = None
    if have_v and volts is not None:
        pre_p = [0.0] * (len(xs) + 1)
        pre_v = [0.0] * (len(xs) + 1)
        for k, (x, v) in enumerate(zip(xs, volts, strict=False)):
            ok = not (math.isnan(x) or math.isnan(v))
            pre_p[k + 1] = pre_p[k] + (x * v if ok else 0.0)
            pre_v[k + 1] = pre_v[k] + (v if ok else 0.0)

    def mean_i(a: int, b: int) -> float:
        return (pre_i[b] - pre_i[a]) / (b - a)

    all_segments: list[tuple[int, list[Segment]]] = []  # (run index, segments)
    folded = 0
    excursions = 0
    n_segments = 0
    for r, (a, b) in enumerate(runs):
        ys = y[a:b]
        cps = pelt(
            ys,
            beta,
            min_size=1,
            positions=candidate_positions(ys),
            quant_ma=quant,
            floor_ma=floor,
        )
        segs = segments_from_changes(ys, cps, offset=a)
        n_segments += len(segs)
        segs, f, x = merge_short(segs, y, opts.min_samples)
        folded += f
        excursions += x
        all_segments.append((r, segs))

    # Name every segment: user bands first, then clustering of the rest.
    flat = [(r, s) for r, segs in all_segments for s in segs]
    names: list[str | None] = []
    for _, s in flat:
        names.append(opts.spec.match(mean_i(s.start, s.end)) if opts.spec else None)
    todo = [k for k, n in enumerate(names) if n is None]
    if todo:
        pts = [math.log10(max(mean_i(flat[k][1].start, flat[k][1].end), 0.0) + floor) for k in todo]
        labels, means = cluster_levels(pts, opts.max_states)
        n_clusters = len(means)
        # Charge-weighted level of each cluster, for naming.
        q_sum = [0.0] * n_clusters
        n_sum = [0] * n_clusters
        for k, lab in zip(todo, labels, strict=True):
            seg = flat[k][1]
            q_sum[lab] += pre_i[seg.end] - pre_i[seg.start]
            n_sum[lab] += seg.n
        levels = [q_sum[j] / n_sum[j] if n_sum[j] else 0.0 for j in range(n_clusters)]
        auto = default_names(levels)
        taken = {b.name for b in opts.spec.bands} if opts.spec else set()
        final: list[str] = []
        for nm in auto:
            candidate = nm if nm not in taken else f"{nm} (auto)"
            suffix = 2
            base_name = candidate
            while candidate in final:
                candidate = f"{base_name} #{suffix}"
                suffix += 1
            final.append(candidate)
        for k, lab in zip(todo, labels, strict=False):
            names[k] = final[lab]

    # Events: maximal runs of equally named segments inside one valid run.
    raw_events: list[tuple[int, int, int, str]] = []  # (run, start, end, name)
    for (r, s), label in zip(flat, names, strict=True):
        assert label is not None
        if raw_events and raw_events[-1][0] == r and raw_events[-1][3] == label:
            pr, pa, _, _ = raw_events[-1]
            raw_events[-1] = (pr, pa, s.end, label)
        else:
            raw_events.append((r, s.start, s.end, label))

    v_fixed = opts.voltage_v
    events: list[Event] = []
    for idx, (r, a, b, nm) in enumerate(raw_events):
        ra, rb = runs[r]
        charge = (pre_i[b] - pre_i[a]) * dt / 3600.0
        if pre_p is not None:
            energy: float | None = (pre_p[b] - pre_p[a]) * dt / 3600.0
        elif v_fixed is not None:
            energy = charge * v_fixed
        else:
            energy = None
        events.append(
            Event(
                idx,
                nm,
                u.time(a),
                u.time(b),
                mean_i(a, b),
                charge,
                energy,
                clipped=(a == ra or b == rb),
                run=r,
            )
        )

    covered = n_valid * dt
    total_charge = (pre_i[-1]) * dt / 3600.0
    if pre_p is not None:
        total_energy: float | None = pre_p[-1] * dt / 3600.0
        voltage_source = "measured"
    elif v_fixed is not None:
        total_energy = total_charge * v_fixed
        voltage_source = "given"
    else:
        total_energy = None
        voltage_source = "none"
    day = SECONDS_PER_DAY / covered

    # Per-state statistics.
    order: list[str] = []
    for e in events:
        if e.state not in order:
            order.append(e.state)
    time_by: dict[str, float] = dict.fromkeys(order, 0.0)
    for e in events:
        time_by[e.state] += e.duration
    base_name = max(order, key=lambda nm: time_by[nm])

    for nm in order:
        mine = [e for e in events if e.state == nm and not e.clipped]
        flags = _robust_outliers([e.duration for e in mine])
        for e, flag in zip(mine, flags, strict=False):
            if flag and nm != base_name:
                med = median([x.duration for x in mine])
                e.outlier = f"{e.duration:.3g} s, median {med:.3g} s"

    states: list[StateStats] = []
    for nm in order:
        evs = [e for e in events if e.state == nm]
        full = [e for e in evs if not e.clipped]
        t_s = sum(e.duration for e in evs)
        q = sum(e.charge_mah for e in evs)
        en: float | None = None
        if all(e.energy_mwh is not None for e in evs):
            en = sum(e.energy_mwh for e in evs if e.energy_mwh is not None)
        durations = [e.duration for e in full]
        periods: list[float] = []
        for r in range(len(runs)):
            starts = [e.start for e in full if e.run == r]
            periods += _intervals(starts)
        volt = None
        if pre_v is not None:
            vs = 0.0
            nv = 0
            for e in evs:
                a = round((e.start - u.t0) / dt)
                b = round((e.end - u.t0) / dt)
                vs += pre_v[b] - pre_v[a]
                nv += b - a
            volt = vs / nv if nv else None
        elif v_fixed is not None:
            volt = v_fixed
        states.append(
            StateStats(
                name=nm,
                level_ma=q * 3600.0 / t_s if t_s > 0 else 0.0,
                voltage_v=volt,
                base=nm == base_name,
                events=len(evs),
                time_s=t_s,
                time_share=t_s / covered,
                charge_mah=q,
                charge_share=q / total_charge if total_charge > 0 else 0.0,
                energy_mwh=en,
                mean_duration_s=sum(durations) / len(durations) if durations else None,
                median_duration_s=median(durations) if durations else None,
                max_duration_s=max(durations) if durations else None,
                median_period_s=median(periods) if periods else None,
                charge_per_event_mah=q / len(evs),
                mah_per_day=q * day,
                mwh_per_day=en * day if en is not None else None,
                outliers=sum(1 for e in evs if e.outlier),
            )
        )
    states.sort(key=lambda s: s.level_ma)

    # Wakes: everything between two stretches of the base state.
    wakes: list[Wake] = []
    current: list[Event] = []
    prev_run: int | None = None
    after_base = False  # a wake is complete only if base-state time precedes and follows it

    def close(run_end: bool) -> None:
        if current:
            wakes.append(
                Wake(
                    current[0].start,
                    current[-1].end,
                    sum(e.charge_mah for e in current),
                    [e.state for e in current],
                    clipped=run_end or not after_base,
                    run=current[0].run,
                )
            )
            current.clear()

    for e in events:
        r = e.run
        if prev_run is not None and r != prev_run:
            close(run_end=True)
            after_base = False
        if e.state == base_name:
            close(run_end=False)
            after_base = True
        else:
            current.append(e)
        prev_run = r
    close(run_end=True)
    full_wakes = [w for w in wakes if not w.clipped]
    med_charge = median([w.charge_mah for w in full_wakes]) if full_wakes else 0.0
    med_wake_dur = median([w.duration for w in full_wakes]) if full_wakes else 0.0
    for w, by_q, by_d in zip(
        full_wakes,
        _robust_outliers([w.charge_mah for w in full_wakes]),
        _robust_outliers([w.duration for w in full_wakes]),
        strict=False,
    ):
        if by_q or by_d:
            w.outlier = (
                f"{w.duration:.3g} s / {w.charge_mah * 1000:.3g} uAh "
                f"(median {med_wake_dur:.3g} s / {med_charge * 1000:.3g} uAh)"
            )
    wake_intervals: list[float] = []
    for r in range(len(runs)):
        wake_intervals += _intervals([w.start for w in wakes if w.run == r and not w.clipped])

    cycle = _cycle(u, runs, wake_intervals, len(full_wakes))

    mean_v = None
    if pre_v is not None:
        mean_v = pre_v[-1] / n_valid
    elif v_fixed is not None:
        mean_v = v_fixed
    mah_day = total_charge * day
    life = None
    if opts.capacity_mah is not None:
        life = battery_life(
            opts.capacity_mah, mah_day, opts.depth_of_discharge, opts.self_discharge_per_month
        )
    result = Result(
        trace=trace,
        uniform=u,
        options=opts,
        native_dt=native_dt,
        lsb_ma=lsb,
        sigma=sigma,
        beta=beta,
        segments=n_segments,
        folded=folded,
        excursions=excursions,
        states=states,
        events=events,
        wakes=wakes,
        cycle=cycle,
        covered_s=covered,
        charge_mah=total_charge,
        energy_mwh=total_energy,
        mean_ma=total_charge * 3600.0 / covered,
        mean_voltage_v=mean_v,
        voltage_source=voltage_source,
        mah_per_day=mah_day,
        mwh_per_day=total_energy * day if total_energy is not None else None,
        battery=life,
    )
    result.warnings = _warnings(result)
    return result


def _cycle(
    u: Uniform, runs: list[tuple[int, int]], wake_intervals: list[float], n_wakes: int
) -> Cycle:
    xs = u.current_ma
    valid = [x for x in xs if not math.isnan(x)]
    mean = sum(valid) / len(valid)
    block = max(1, math.ceil(len(xs) / ACF_MAX_POINTS))
    series: list[float] = []
    for k in range(0, len(xs), block):
        chunk = [x for x in xs[k : k + block] if not math.isnan(x)]
        series.append(sum(chunk) / len(chunk) if chunk else mean)
    acf = autocorrelation(series, len(series) // 2)
    found = dominant_period(acf, min_lag=2)
    period = peak = None
    if found is not None:
        lag, peak = found
        period = parabolic_peak(acf, lag) * block * u.dt
    return Cycle(
        period,
        peak,
        median(wake_intervals) if wake_intervals else None,
        n_wakes,
    )


# Below this much covered log time the daily extrapolation gets its own warning.
MIN_DAILY_BASIS_S = 60.0


def _warnings(r: Result) -> list[Warning_]:
    out: list[Warning_] = []
    dt = r.native_dt
    fs = 1.0 / dt
    if r.trace.notes:
        for note in r.trace.notes:
            out.append(Warning_("input", note))
    gaps: list[Gap] = r.uniform.gaps
    if gaps:
        lost = sum(g.length for g in gaps)
        out.append(
            Warning_(
                "gaps",
                f"{len(gaps)} gap(s) longer than {r.options.gap_factor:g} sample periods "
                f"({lost:.3g} s in total) are excluded; daily figures are scaled from the "
                f"{r.covered_s:.4g} s that were covered",
            )
        )
    for s in r.states:
        if s.base or s.median_duration_s is None:
            continue
        n = s.median_duration_s / dt
        if n < 2:
            out.append(
                Warning_(
                    "sample-rate",
                    f"'{s.name}' lasts a median {s.median_duration_s * 1000:.3g} ms, "
                    f"only {n:.2g} sample periods at {fs:.3g} Hz. A burst of length d is only "
                    f"certain to be sampled twice when the sample period is at most d/2 "
                    f"(f_s >= 2/d, the Nyquist-style limit for a rectangular pulse); its "
                    f"current and duration are not resolved",
                )
            )
        elif n < 10:
            out.append(
                Warning_(
                    "sample-rate",
                    f"'{s.name}' lasts a median {s.median_duration_s * 1000:.3g} ms = "
                    f"{n:.2g} samples; each edge is uncertain by up to one period, so its "
                    f"duration may be off by +/-{dt * 1000:.3g} ms "
                    f"(+/-{100 * dt / s.median_duration_s:.2g} %). At least 10 samples per "
                    f"burst (f_s >= 10/d) keeps this under 10 %",
                )
            )
    wakes = max(1, len(r.wakes))
    if r.excursions >= 3 and r.excursions >= 0.1 * wakes:
        out.append(
            Warning_(
                "aliasing",
                f"{r.excursions} single-sample excursion(s) above or below both neighbouring "
                f"states were folded into a neighbour. They are most likely bursts shorter "
                f"than 2 sample periods ({2 * dt * 1000:.3g} ms), which this sample rate cannot "
                f"resolve (f_s >= 2/d); their charge is counted but attributed to a "
                f"neighbouring state, and bursts that fell between samples are missing",
            )
        )
    if r.options.min_burst_s is not None:
        d = r.options.min_burst_s
        if dt > d / 2:
            out.append(
                Warning_(
                    "sample-rate",
                    f"bursts of {d * 1000:.3g} ms need a sample period of at most "
                    f"{d * 500:.3g} ms (f_s >= 2/d = {2 / d:.3g} Hz) to be seen at all; this "
                    f"log has {dt * 1000:.3g} ms ({fs:.3g} Hz). Recommended: <= "
                    f"{d * 100:.3g} ms (10 samples per burst)",
                )
            )
        elif dt > d / 10:
            out.append(
                Warning_(
                    "sample-rate",
                    f"bursts of {d * 1000:.3g} ms get only {d / dt:.2g} samples at "
                    f"{fs:.3g} Hz; durations are uncertain by up to {100 * dt / d:.2g} %",
                )
            )
    if r.lsb_ma is not None:
        base = r.base_state
        if base.level_ma < 10 * r.lsb_ma:
            out.append(
                Warning_(
                    "quantisation",
                    f"the base state '{base.name}' draws {format_ma(base.level_ma)}, only "
                    f"{base.level_ma / r.lsb_ma:.2g} ADC steps of {format_ma(r.lsb_ma)}; "
                    f"each reading is rounded by up to {format_ma(r.lsb_ma / 2)}, and without "
                    f"enough noise to dither the readings the mean can be biased by that much "
                    f"({50 * r.lsb_ma / max(base.level_ma, 1e-12):.2g} % of the level)",
                )
            )
    if r.covered_s < MIN_DAILY_BASIS_S:
        out.append(
            Warning_(
                "short-log",
                f"daily figures are extrapolated x{SECONDS_PER_DAY / r.covered_s:.3g} from "
                f"only {r.covered_s:.3g} s of log; record at least {MIN_DAILY_BASIS_S:g} s "
                "and several wake cycles before relying on mAh/day or battery life",
            )
        )
    if r.cycle.wakes < 3:
        out.append(
            Warning_(
                "short-log",
                f"only {r.cycle.wakes} complete wake cycle(s) in the log; per-event "
                "statistics and the period are not reliable",
            )
        )
    if r.trace.device_charge_mah is not None and r.charge_mah > 0:
        dev = r.trace.device_charge_mah
        diff = (dev - r.charge_mah) / r.charge_mah
        if abs(diff) > 0.05:
            out.append(
                Warning_(
                    "device-counter",
                    f"the device's coulomb counter reports {dev:.4g} mAh over the log, "
                    f"{100 * diff:+.2g} % from the integrated trace ({r.charge_mah:.4g} mAh)",
                )
            )
    return out


def _round(x: float | None, digits: int = 6) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return float(f"{x:.{digits}g}")


def result_json(r: Result) -> dict[str, Any]:
    return {
        "tool": "embedded-power-profiler",
        "version": __version__,
        "input": {
            "name": r.trace.name,
            "format": r.trace.source,
            "samples": len(r.trace),
            "sample_period_s": _round(r.native_dt),
            "analysis_period_s": _round(r.uniform.dt),
            "span_s": _round(r.trace.span_s),
            "covered_s": _round(r.covered_s),
            "gaps": [{"start_s": _round(g.start), "end_s": _round(g.end)} for g in r.uniform.gaps],
            "adc_step_ma": _round(r.lsb_ma),
            "device_counter_mah": _round(r.trace.device_charge_mah),
        },
        "segmentation": {
            "noise_sigma_decades": _round(r.sigma),
            "penalty": r.options.penalty,
            "beta": _round(r.beta),
            "segments": r.segments,
            "folded_short_segments": r.folded,
            "excursions": r.excursions,
        },
        "states": [
            {
                "name": s.name,
                "base": s.base,
                "current_ma": _round(s.level_ma),
                "voltage_v": _round(s.voltage_v),
                "events": s.events,
                "time_s": _round(s.time_s),
                "time_share": _round(s.time_share),
                "charge_mah": _round(s.charge_mah),
                "charge_share": _round(s.charge_share),
                "energy_mwh": _round(s.energy_mwh),
                "mean_duration_s": _round(s.mean_duration_s),
                "median_duration_s": _round(s.median_duration_s),
                "max_duration_s": _round(s.max_duration_s),
                "median_period_s": _round(s.median_period_s),
                "charge_per_event_mah": _round(s.charge_per_event_mah),
                "mah_per_day": _round(s.mah_per_day),
                "mwh_per_day": _round(s.mwh_per_day),
                "outlier_events": s.outliers,
            }
            for s in r.states
        ],
        "cycle": {
            "acf_period_s": _round(r.cycle.acf_period_s),
            "acf_peak": _round(r.cycle.acf_peak),
            "median_wake_interval_s": _round(r.cycle.median_wake_interval_s),
            "complete_wakes": r.cycle.wakes,
        },
        "outliers": {
            "events": [
                {"state": e.state, "start_s": _round(e.start), "detail": e.outlier}
                for e in r.events
                if e.outlier
            ],
            "wakes": [
                {"start_s": _round(w.start), "detail": w.outlier} for w in r.wakes if w.outlier
            ],
        },
        "budget": {
            "mean_current_ma": _round(r.mean_ma),
            "mean_voltage_v": _round(r.mean_voltage_v),
            "voltage_source": r.voltage_source,
            "charge_mah": _round(r.charge_mah),
            "energy_mwh": _round(r.energy_mwh),
            "mah_per_day": _round(r.mah_per_day),
            "mwh_per_day": _round(r.mwh_per_day),
        },
        "battery": r.battery.to_json() if r.battery else None,
        "warnings": [{"code": w.code, "message": w.message} for w in r.warnings],
    }
