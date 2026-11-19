"""Self-contained HTML report: inline SVG, no JavaScript, no external resources, light/dark.

Zooming is done with linked static panels: the overview of the whole log outlines the windows
that are drawn again below at full resolution (a typical wake, the worst outlier wake, one full
cycle); each outline links to its panel. Hovering a state strip or a bar segment shows a native
SVG tooltip (``<title>``), which also works without scripts.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass

from . import __version__
from .analysis import Result, Wake
from .states import format_ma

# Categorical slots (light, dark), assigned to states in order of increasing current.
PALETTE = (
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#0ca30c"),
    ("#6250d6", "#9085e9"),
    ("#e34948", "#e66767"),
)
OTHER = ("#8b8a85", "#76756f")

_W = 1000.0
_ML, _MR, _MT, _MB = 70.0, 14.0, 12.0, 40.0


def _css(n_states: int) -> str:
    light = "".join(f"--c{k}:{PALETTE[k][0]};" for k in range(min(n_states, len(PALETTE))))
    dark = "".join(f"--c{k}:{PALETTE[k][1]};" for k in range(min(n_states, len(PALETTE))))
    rules = "".join(
        f".s{k}{{stroke:var(--c{k})}}.f{k}{{fill:var(--c{k})}}"
        for k in range(min(n_states, len(PALETTE)))
    )
    return (
        """
:root{color-scheme:light;--bg:#f7f7f5;--card:#fcfcfb;--fg:#1d1f21;--muted:#5d646b;
--line:#d9dcdf;--grid:#e7e8e6;--ink:#30363d;--warn:#9a6200;--gap:#c9cbc7;--cother:#8b8a85;"""
        + light
        + """}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--bg:#121416;--card:#1a1a19;--fg:#e6e8ea;--muted:#9aa3ab;--line:#30363d;--grid:#2a2d30;
--ink:#c9d1d9;--warn:#e7a93a;--gap:#3a3d40;--cother:#76756f;"""
        + dark
        + """}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121416;--card:#1a1a19;--fg:#e6e8ea;
--muted:#9aa3ab;--line:#30363d;--grid:#2a2d30;--ink:#c9d1d9;--warn:#e7a93a;--gap:#3a3d40;
--cother:#76756f;"""
        + dark
        + """}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
h3{font-size:15px;margin:0 0 6px}
.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;
margin-bottom:14px;overflow-x:auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.stat b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
.stat span{font-size:13px;color:var(--muted)}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:top;padding:5px 8px;border-bottom:1px solid var(--line)}
th{font-size:13px;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.outlier td{font-weight:600}
.warn{color:var(--warn);font-weight:600}
ul.warnings{margin:0;padding-left:20px}ul.warnings li{margin:4px 0}
svg{width:100%;height:auto;display:block}
.trace{fill:none;stroke-width:1.6;stroke-linejoin:round}
.env{stroke:var(--ink);stroke-width:1}
.grid{stroke:var(--grid);stroke-width:1}
.axis{fill:var(--muted);font-size:12px}
.frame{fill:none;stroke:var(--line)}
.gapbox{fill:var(--gap);opacity:.6}
.zoombox{fill:none;stroke:var(--fg);stroke-width:1.5;stroke-dasharray:4 3}
.zoomlabel{fill:var(--fg);font-size:13px;font-weight:700}
.so{stroke:var(--cother)}.fo{fill:var(--cother)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted);margin-top:8px}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;
vertical-align:-1px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}
a{color:inherit}
"""
        + rules
    )


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _f(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _num(x: float | None, digits: int = 4, unit: str = "") -> str:
    if x is None or not math.isfinite(x):
        return "-"
    return f"{x:.{digits}g}{unit}"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.3g} ms"
    if seconds < 120:
        return f"{seconds:.3g} s"
    if seconds < 7200:
        return f"{seconds / 60:.3g} min"
    return f"{seconds / 3600:.3g} h"


@dataclass
class _Plot:
    t0: float
    t1: float
    y0: float  # log10 mA at the bottom
    y1: float
    height: float

    def x(self, t: float) -> float:
        return _ML + (t - self.t0) / (self.t1 - self.t0) * (_W - _ML - _MR)

    def y(self, ma: float, floor: float) -> float:
        v = math.log10(max(ma, 0.0) + floor)
        v = min(max(v, self.y0), self.y1)
        return self.height - _MB - (v - self.y0) / (self.y1 - self.y0) * (self.height - _MT - _MB)


def _decade_label(d: int) -> str:
    ma = 10.0**d
    if ma >= 1000:
        return f"{ma / 1000:g} A"
    if ma >= 1:
        return f"{ma:g} mA"
    if ma >= 1e-3:
        return f"{ma * 1000:g} uA"
    return f"{ma * 1e6:g} nA"


def _axes(p: _Plot, xlabel: str, t_offset: float) -> list[str]:
    parts = []
    for d in range(math.floor(p.y0), math.ceil(p.y1) + 1):
        if d < p.y0 - 1e-9 or d > p.y1 + 1e-9:
            continue
        yy = p.height - _MB - (d - p.y0) / (p.y1 - p.y0) * (p.height - _MT - _MB)
        parts.append(f'<line class="grid" x1="{_ML}" x2="{_W - _MR}" y1="{_f(yy)}" y2="{_f(yy)}"/>')
        parts.append(
            f'<text class="axis" x="{_ML - 6}" y="{_f(yy + 4)}" text-anchor="end">'
            f"{_decade_label(d)}</text>"
        )
    span = p.t1 - p.t0
    step = _nice_step(span / 8)
    first = math.ceil((p.t0 - t_offset) / step) * step
    t = first
    while t + t_offset <= p.t1 + 1e-9:
        xx = p.x(t + t_offset)
        parts.append(
            f'<line class="grid" x1="{_f(xx)}" x2="{_f(xx)}" y1="{_MT}" y2="{p.height - _MB}"/>'
        )
        parts.append(
            f'<text class="axis" x="{_f(xx)}" y="{p.height - _MB + 16}" text-anchor="middle">'
            f"{t:g}</text>"
        )
        t += step
    parts.append(
        f'<text class="axis" x="{_W - _MR}" y="{p.height - 4}" text-anchor="end">'
        f"{_esc(xlabel)}</text>"
    )
    parts.append(
        f'<rect class="frame" x="{_ML}" y="{_MT}" width="{_W - _ML - _MR}" '
        f'height="{p.height - _MT - _MB}"/>'
    )
    return parts


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return float(m * mag)
    return float(10 * mag)  # pragma: no cover


class _Ctx:
    def __init__(self, r: Result) -> None:
        self.r = r
        self.u = r.uniform
        self.floor = r.lsb_ma if r.lsb_ma is not None else 1e-3
        valid = [x for x in self.u.current_ma if not math.isnan(x)]
        lo = max(min(valid), 0.0) + self.floor
        hi = max(valid) + self.floor
        self.y0 = math.floor(math.log10(lo))
        self.y1 = math.ceil(math.log10(hi))
        if self.y1 <= self.y0:
            self.y1 = self.y0 + 1
        names = [s.name for s in r.states]  # ordered by current
        self.cls = {nm: (str(k) if k < len(PALETTE) else "o") for k, nm in enumerate(names)}

    def color_class(self, state: str, kind: str) -> str:
        return f"{kind}{self.cls.get(state, 'o')}"

    def swatch(self, state: str) -> str:
        k = self.cls.get(state, "o")
        var = f"--c{k}" if k != "o" else "--cother"
        return f'<span class="sw" style="background:var({var})"></span>'


def _trace_svg(
    ctx: _Ctx, t0: float, t1: float, height: float, xlabel: str, t_offset: float, extra: str = ""
) -> str:
    r, u = ctx.r, ctx.u
    p = _Plot(t0, t1, ctx.y0, ctx.y1, height)
    parts = [
        f'<svg viewBox="0 0 {_W:g} {height:g}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Current trace from {t0:.4g} s to {t1:.4g} s">'
    ]
    parts += _axes(p, xlabel, t_offset)
    for g in u.gaps:
        if g.end < t0 or g.start > t1:
            continue
        xa, xb = p.x(max(g.start, t0)), p.x(min(g.end, t1))
        parts.append(
            f'<rect class="gapbox" x="{_f(xa)}" y="{_MT}" width="{_f(max(xb - xa, 1))}" '
            f'height="{height - _MT - _MB}"><title>gap {g.start:.4g}-{g.end:.4g} s '
            f"(not analysed)</title></rect>"
        )
    k0 = max(0, math.floor((t0 - u.t0) / u.dt))
    k1 = min(len(u), math.ceil((t1 - u.t0) / u.dt) + 1)
    columns = _W - _ML - _MR
    dense = (k1 - k0) > 2 * columns
    for e in r.events:
        if e.end < t0 or e.start > t1:
            continue
        a = max(k0, round((e.start - u.t0) / u.dt))
        b = min(k1, round((e.end - u.t0) / u.dt) + 1)  # include the next sample: no breaks
        b = min(b, len(u))
        cls = ctx.color_class(e.state, "s")
        if dense:
            # min/max envelope per pixel column
            cols: dict[int, tuple[float, float]] = {}
            for k in range(a, b):
                x = u.current_ma[k]
                if math.isnan(x):
                    continue
                c = int(p.x(u.time(k)))
                lo, hi = cols.get(c, (x, x))
                cols[c] = (min(lo, x), max(hi, x))
            d = "".join(
                f"M{c}.5 {_f(p.y(hi, ctx.floor))}V{_f(p.y(lo, ctx.floor) + 0.8)}"
                for c, (lo, hi) in sorted(cols.items())
            )
            if d:
                parts.append(f'<path class="trace {cls}" d="{d}"/>')
        else:
            pts = []
            for k in range(a, b):
                x = u.current_ma[k]
                if math.isnan(x):
                    continue
                pts.append(f"{_f(p.x(u.time(k)))},{_f(p.y(x, ctx.floor))}")
            if len(pts) >= 2:
                parts.append(f'<polyline class="trace {cls}" points="{" ".join(pts)}"/>')
    # State strip along the bottom of the plot, one rect per event, with a tooltip.
    strip_y = height - _MB - 6
    for e in r.events:
        if e.end < t0 or e.start > t1:
            continue
        xa, xb = p.x(max(e.start, t0)), p.x(min(e.end, t1))
        parts.append(
            f'<rect class="{ctx.color_class(e.state, "f")}" x="{_f(xa)}" y="{_f(strip_y)}" '
            f'width="{_f(max(xb - xa, 0.8))}" height="5"><title>{_esc(e.state)}: '
            f"{e.start:.4g}-{e.end:.4g} s, {_dur(e.duration)}, {format_ma(e.mean_ma)}"
            f"</title></rect>"
        )
    parts.append(extra)
    parts.append("</svg>")
    return "\n".join(x for x in parts if x)


def _window(w: Wake, pad_frac: float, t_min: float, t_max: float) -> tuple[float, float]:
    pad = max(w.duration * pad_frac, 0.2)
    return max(t_min, w.start - pad), min(t_max, w.end + pad)


def _zoom_windows(r: Result) -> list[tuple[str, str, float, float]]:
    """(id, title, t0, t1) of the detail panels."""
    u = r.uniform
    t_min, t_max = u.t0, u.time(len(u) - 1)
    full = [w for w in r.wakes if not w.clipped] or list(r.wakes)
    out: list[tuple[str, str, float, float]] = []
    if not full:
        return out
    charges = sorted(w.charge_mah for w in full)
    med = charges[len(charges) // 2]
    typical = min(full, key=lambda w: abs(w.charge_mah - med))
    a, b = _window(typical, 0.25, t_min, t_max)
    out.append(("a", f"A - typical wake at {typical.start:.4g} s", a, b))
    outliers = [w for w in full if w.outlier]
    if outliers:
        worst = max(outliers, key=lambda w: w.charge_mah)
        a, b = _window(worst, 0.1, t_min, t_max)
        out.append(("b", f"B - largest outlier wake at {worst.start:.4g} s", a, b))
    period = r.cycle.acf_period_s or r.cycle.median_wake_interval_s
    if period and period > 0:
        a = max(t_min, typical.start - 0.1 * period)
        b = min(t_max, a + 1.2 * period)
        out.append(("c", f"C - one cycle ({_dur(period)}) from {a:.4g} s", a, b))
    return out


def _overview(ctx: _Ctx, windows: list[tuple[str, str, float, float]]) -> str:
    u = ctx.u
    t0, t1 = u.t0, u.time(len(u) - 1)
    height = 260.0
    p = _Plot(t0, t1, ctx.y0, ctx.y1, height)
    boxes = []
    for wid, title, a, b in windows:
        xa, xb = p.x(a), p.x(b)
        if xb - xa < 6:
            mid = (xa + xb) / 2
            xa, xb = mid - 3, mid + 3
        boxes.append(
            f'<a href="#zoom-{wid}"><rect class="zoombox" x="{_f(xa)}" y="{_MT + 2}" '
            f'width="{_f(xb - xa)}" height="{height - _MT - _MB - 4}"><title>{_esc(title)}'
            f'</title></rect><text class="zoomlabel" x="{_f(xb + 3)}" y="{_MT + 16}">'
            f"{wid.upper()}</text></a>"
        )
    return _trace_svg(ctx, t0, t1, height, "time (s)", 0.0, "\n".join(boxes))


def _energy_bar(ctx: _Ctx) -> str:
    r = ctx.r
    total = r.mah_per_day
    width, height = _W, 64.0
    x = 0.0
    gap = 2.0
    usable = width - gap * (len(r.states) - 1)
    parts = [
        f'<svg viewBox="0 0 {width:g} {height:g}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Charge per day by state, stacked">'
    ]
    for s in sorted(r.states, key=lambda s: -s.mah_per_day):
        w = usable * (s.mah_per_day / total) if total > 0 else 0.0
        if w <= 0:
            continue
        energy = f", {_num(s.mwh_per_day)} mWh/day" if s.mwh_per_day is not None else ""
        parts.append(
            f'<rect class="{ctx.color_class(s.name, "f")}" x="{_f(x)}" y="8" '
            f'width="{_f(max(w, 1.0))}" height="40" rx="4"><title>{_esc(s.name)}: '
            f"{_num(s.mah_per_day)} mAh/day ({100 * s.charge_share:.1f} %){energy}</title></rect>"
        )
        x += w + gap
    parts.append("</svg>")
    return "\n".join(parts)


def _bar_legend(ctx: _Ctx) -> str:
    items = [
        f"<span>{ctx.swatch(s.name)}{_esc(s.name)}: {_num(s.mah_per_day)} mAh/day "
        f"({100 * s.charge_share:.1f} %)</span>"
        for s in sorted(ctx.r.states, key=lambda s: -s.mah_per_day)
    ]
    return f'<div class="legend">{"".join(items)}</div>'


def _legend(ctx: _Ctx) -> str:
    items = [
        f"<span>{ctx.swatch(s.name)}{_esc(s.name)} ({format_ma(s.level_ma)})</span>"
        for s in ctx.r.states
    ]
    if ctx.u.gaps:
        items.append(
            '<span><span class="sw" style="background:var(--gap)"></span>gap (excluded)</span>'
        )
    return f'<div class="legend">{"".join(items)}</div>'


def _states_table(ctx: _Ctx) -> str:
    r = ctx.r
    has_e = r.mwh_per_day is not None
    head = (
        "<tr><th>State</th><th class=num>Current</th><th class=num>Events</th>"
        "<th class=num>Median duration</th><th class=num>Max duration</th>"
        "<th class=num>Period</th><th class=num>Charge / event</th>"
        "<th class=num>Time share</th><th class=num>mAh/day</th><th class=num>Share</th>"
        + ("<th class=num>mWh/day</th>" if has_e else "")
        + "</tr>"
    )
    rows = []
    for s in r.states:
        rows.append(
            f"<tr><td>{ctx.swatch(s.name)}{_esc(s.name)}{' (base)' if s.base else ''}</td>"
            f"<td class=num>{format_ma(s.level_ma)}</td><td class=num>{s.events}</td>"
            f"<td class=num>{_dur(s.median_duration_s)}</td>"
            f"<td class=num>{_dur(s.max_duration_s)}</td>"
            f"<td class=num>{_dur(s.median_period_s)}</td>"
            f"<td class=num>{_num(s.charge_per_event_mah * 1000)} uAh</td>"
            f"<td class=num>{100 * s.time_share:.3g} %</td>"
            f"<td class=num>{_num(s.mah_per_day)}</td>"
            f"<td class=num>{100 * s.charge_share:.1f} %</td>"
            + (f"<td class=num>{_num(s.mwh_per_day)}</td>" if has_e else "")
            + "</tr>"
        )
    return f"<table>{head}{''.join(rows)}</table>"


def _events_table(ctx: _Ctx, limit: int) -> str:
    r = ctx.r
    events = r.events
    shown = events[:limit]
    extra = [e for e in events[limit:] if e.outlier]
    head = (
        "<tr><th class=num>#</th><th>State</th><th class=num>Start (s)</th>"
        "<th class=num>Duration</th><th class=num>Mean current</th><th class=num>Charge</th>"
        "<th>Note</th></tr>"
    )
    rows = []
    for e in [*shown, *extra]:
        note = []
        if e.outlier:
            note.append(f'<span class="warn">outlier</span> ({_esc(e.outlier)})')
        if e.clipped:
            note.append("clipped by the log edge or a gap")
        cls = ' class="outlier"' if e.outlier else ""
        rows.append(
            f"<tr{cls}><td class=num>{e.index}</td><td>{ctx.swatch(e.state)}{_esc(e.state)}</td>"
            f"<td class=num>{e.start:.3f}</td><td class=num>{_dur(e.duration)}</td>"
            f"<td class=num>{format_ma(e.mean_ma)}</td>"
            f"<td class=num>{_num(e.charge_mah * 1000)} uAh</td><td>{'; '.join(note)}</td></tr>"
        )
    more = ""
    if len(events) > limit:
        more = (
            f'<p class="muted">Showing the first {limit} of {len(events)} events'
            f"{' and every later outlier' if extra else ''}. "
            "Write them all with <code>--events-csv</code>.</p>"
        )
    return f"<table>{head}{''.join(rows)}</table>{more}"


def render_html(r: Result, event_limit: int = 150) -> str:
    ctx = _Ctx(r)
    windows = _zoom_windows(r)
    name = r.trace.name
    stats = [
        (format_ma(r.mean_ma), "mean current"),
        (f"{r.mah_per_day:.4g} mAh", "per day"),
        (
            f"{r.mwh_per_day:.4g} mWh" if r.mwh_per_day is not None else "-",
            "per day"
            + (
                f" at {r.mean_voltage_v:.3g} V ({r.voltage_source})"
                if r.mean_voltage_v is not None
                else " (no voltage)"
            ),
        ),
        (_dur(r.cycle.acf_period_s), "wake period (autocorrelation)"),
        (str(len(r.states)), "states"),
    ]
    if r.battery is not None:
        bat = r.battery
        stats.append(
            (
                f"{bat.days_with_self_discharge:.4g} d",
                f"on {bat.capacity_mah:g} mAh ({bat.days_without_self_discharge:.4g} d "
                "without self-discharge)",
            )
        )
    stat_html = "".join(
        f'<div class="stat"><b>{_esc(v)}</b><span>{_esc(k)}</span></div>' for v, k in stats
    )
    warn_html = (
        "<ul class=warnings>"
        + "".join(f"<li><b>{_esc(w.code)}</b>: {_esc(w.message)}</li>" for w in r.warnings)
        + "</ul>"
        if r.warnings
        else '<p class="muted">No warnings.</p>'
    )
    panels = []
    for wid, title, a, b in windows:
        panels.append(
            f'<div class="card" id="zoom-{wid}"><h3>{_esc(title)}</h3>'
            + _trace_svg(ctx, a, b, 230.0, f"seconds after {a:.4g} s", a)
            + '<p class="muted"><a href="#overview">back to the overview</a></p></div>'
        )
    cyc = r.cycle
    outlier_wakes = [w for w in r.wakes if w.outlier]
    wake_html = (
        "<ul>"
        + "".join(
            f"<li>{w.start:.4g} s: {_esc(w.outlier)} - {_esc(' > '.join(w.states))}</li>"
            for w in outlier_wakes
        )
        + "</ul>"
        if outlier_wakes
        else '<p class="muted">No outlier wakes.</p>'
    )
    battery_html = ""
    if r.battery is not None:
        bat = r.battery
        battery_html = (
            "<h2>Battery life</h2><div class=card><p>"
            f"{bat.capacity_mah:g} mAh, {100 * bat.depth_of_discharge:g} % usable, "
            f"{_num(bat.mah_per_day)} mAh/day: <b>{bat.days_without_self_discharge:.4g} days</b> "
            f"without self-discharge, <b>{bat.days_with_self_discharge:.4g} days</b> with "
            f"{100 * bat.self_discharge_per_month:g} % per month of the stored charge "
            "(solar-node-sizer v0.1.1 convention: self-discharge proportional to the stored "
            "energy, applied hour by hour before the load).</p></div>"
        )
    step = format_ma(r.lsb_ma) if r.lsb_ma else "not detected"
    params = (
        f"sample period {r.native_dt * 1000:.4g} ms ({1 / r.native_dt:.4g} Hz), analysis grid "
        f"{r.uniform.dt * 1000:.4g} ms, ADC step {step}"
        f"; PELT on log10 current, noise sigma {r.sigma:.3g} decades, penalty "
        f"{r.options.penalty:g} (beta {r.beta:.3g}); {r.segments} segments, {r.folded} folded "
        f"(shorter than {r.options.min_samples} samples); {len(r.states)} states from a Gaussian "
        "mixture chosen by BIC."
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Power profile - {_esc(name)}</title>
<style>{_css(len(r.states))}</style></head>
<body><main>
<h1>Power profile: {_esc(name)}</h1>
<p class="muted">{_esc(r.trace.source)} log, {len(r.trace)} samples over {_dur(r.trace.span_s)}
({_dur(r.covered_s)} analysed). embedded-power-profiler {__version__}.</p>
<div class="stats">{stat_html}</div>
<h2>Warnings</h2><div class="card">{warn_html}</div>
<h2 id="overview">Current trace</h2>
<div class="card">{_overview(ctx, windows)}{_legend(ctx)}
<p class="muted">Log-scale current; the strip along the bottom shows the detected state. Dashed
boxes link to the detail panels below.</p></div>
{"".join(panels)}
<h2>Charge per day by state</h2>
<div class="card">{_energy_bar(ctx)}{_bar_legend(ctx)}
<p class="muted">Total {r.mah_per_day:.4g} mAh/day
{f"/ {r.mwh_per_day:.4g} mWh/day" if r.mwh_per_day is not None else ""}, scaled from the
analysed time.</p></div>
<h2>States</h2><div class="card">{_states_table(ctx)}</div>
<h2>Wake cycle</h2><div class="card"><p>Autocorrelation period
<b>{_dur(cyc.acf_period_s)}</b> (peak r = {_num(cyc.acf_peak, 3)}); median interval between
wakes <b>{_dur(cyc.median_wake_interval_s)}</b> over {cyc.wakes} complete wakes.</p>
{wake_html}</div>
{battery_html}
<h2>Events</h2><div class="card">{_events_table(ctx, event_limit)}</div>
<h2>Method</h2><div class="card"><p class="muted">{_esc(params)}</p></div>
</main></body></html>
"""
