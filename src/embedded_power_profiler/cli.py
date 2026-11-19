"""Command-line interface: ``epp analyze | export | synth``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .analysis import Options, Result, analyze
from .battery import DEFAULT_DOD, DEFAULT_SELF_DISCHARGE
from .cluster import DEFAULT_MAX_STATES
from .export import (
    ExportError,
    solar_node_sizer_config,
    solar_node_sizer_profile,
    write_events_csv,
)
from .html_report import render_html
from .loaders import FORMATS, CsvOptions, load
from .segment import DEFAULT_MIN_SAMPLES, DEFAULT_PENALTY
from .sslog import SslogError
from .states import StatesFileError, format_ma, load_spec
from .synth import PROFILES, generate, states_toml
from .trace import TraceError
from .writers import WRITE_FORMATS, write

EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text!r}")
    return value


def _fraction(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not 0 <= value < 1:
        raise argparse.ArgumentTypeError(f"must be in [0, 1): {text!r}")
    return value


def _dod(text: str) -> float:
    value = _positive(text)
    if value > 1:
        raise argparse.ArgumentTypeError(f"must be in (0, 1]: {text!r}")
    return value


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text!r}")
    return value


def _input_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "log", help="log file (ina219 raw log, logger CSV, .sslog, serial-scope CSV, CSV)"
    )
    p.add_argument("--format", choices=FORMATS, default="auto", help="input format (default: auto)")
    g = p.add_argument_group("generic CSV columns (default: guessed from the header)")
    g.add_argument("--time-col", metavar="NAME")
    g.add_argument("--current-col", metavar="NAME")
    g.add_argument("--voltage-col", metavar="NAME")
    g.add_argument("--time-unit", choices=["s", "ms", "us", "ns", "min", "h"])
    g.add_argument("--current-unit", choices=["A", "mA", "uA", "nA"])
    g.add_argument("--voltage-unit", choices=["V", "mV"])
    a = p.add_argument_group("analysis")
    a.add_argument("--states", metavar="FILE", help="TOML/JSON file naming current bands")
    a.add_argument(
        "--voltage",
        type=_positive,
        metavar="V",
        help="supply voltage for mWh when the log has no voltage column",
    )
    a.add_argument(
        "--resample-ms",
        type=_positive,
        metavar="MS",
        help="analysis grid (default: median interval)",
    )
    a.add_argument(
        "--penalty",
        type=_positive,
        default=None,
        help=f"change-point penalty multiplier (default {DEFAULT_PENALTY:g}; higher = fewer)",
    )
    a.add_argument(
        "--min-samples",
        type=_count,
        default=None,
        help=f"fold shorter segments into neighbours (default {DEFAULT_MIN_SAMPLES})",
    )
    a.add_argument("--max-states", type=_count, default=None, help=f"default {DEFAULT_MAX_STATES}")
    a.add_argument(
        "--min-burst-ms",
        type=_positive,
        metavar="MS",
        help="shortest burst you need to resolve: warns if the sample rate is too low",
    )
    b = p.add_argument_group("battery")
    b.add_argument("--capacity-mah", type=_positive, metavar="MAH", help="estimate battery life")
    b.add_argument(
        "--dod",
        type=_dod,
        default=DEFAULT_DOD,
        help=f"usable fraction of the capacity (default {DEFAULT_DOD:g})",
    )
    b.add_argument(
        "--self-discharge",
        type=_fraction,
        default=DEFAULT_SELF_DISCHARGE,
        metavar="PER_MONTH",
        help=f"fraction of the stored charge lost per month (default {DEFAULT_SELF_DISCHARGE:g})",
    )
    p.add_argument("--events-csv", metavar="FILE", help="write every detected event to a CSV file")
    p.add_argument(
        "--strict", action="store_true", help="exit 1 when the analysis produced warnings"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epp",
        description="Turn a current/voltage log of an embedded device into an energy budget "
        "per operating state.",
    )
    parser.add_argument("--version", action="version", version=f"epp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    an = sub.add_parser("analyze", help="detect states and print the energy budget")
    _input_args(an)
    an.add_argument("--report", metavar="FILE.html", help="write a self-contained HTML report")
    an.add_argument("--json", action="store_true", help="print JSON instead of text")

    ex = sub.add_parser("export", help="write a solar-node-sizer load profile")
    _input_args(ex)
    ex.add_argument(
        "--solar-node-sizer",
        required=True,
        metavar="FILE.json",
        help="solar-node-sizer configuration to write",
    )
    ex.add_argument(
        "--full-config",
        action="store_true",
        help="write a complete config (solar-node-sizer defaults + the measured load)",
    )
    ex.add_argument(
        "--measured-at",
        choices=["supply", "battery"],
        default="supply",
        help="where the current was measured (default: supply, after the regulator)",
    )
    ex.add_argument("--name", help="configuration name")

    sy = sub.add_parser("synth", help="write a synthetic trace for demos and tests")
    sy.add_argument("--profile", choices=sorted(PROFILES), default="esp32-wifi")
    sy.add_argument("-o", "--out", required=True, metavar="FILE")
    sy.add_argument(
        "--format", choices=WRITE_FORMATS, default="csv", help="output format (default csv)"
    )
    sy.add_argument("--seed", type=int, default=1)
    sy.add_argument("--duration", type=_positive, metavar="S", help="seconds (profile default)")
    sy.add_argument("--rate", type=_positive, metavar="HZ", help="sample rate (profile default)")
    sy.add_argument("--states-out", metavar="FILE.toml", help="also write a matching states file")
    return parser


def _options(args: argparse.Namespace) -> Options:
    opts = Options(
        resample_dt=args.resample_ms / 1000 if args.resample_ms else None,
        voltage_v=args.voltage,
        capacity_mah=args.capacity_mah,
        depth_of_discharge=args.dod,
        self_discharge_per_month=args.self_discharge,
        min_burst_s=args.min_burst_ms / 1000 if args.min_burst_ms else None,
        spec=load_spec(args.states) if args.states else None,
    )
    opts.apply_spec()
    # Command-line values win over the [analysis] table of a states file.
    if args.penalty is not None:
        opts.penalty = args.penalty
    if args.min_samples is not None:
        opts.min_samples = args.min_samples
    if args.max_states is not None:
        opts.max_states = args.max_states
    if opts.spec is not None:
        opts.spec.penalty = opts.spec.min_samples = opts.spec.max_states = None
    return opts


def _run_analysis(args: argparse.Namespace) -> Result:
    unit = {"A": "a", "mA": "ma", "uA": "ua", "nA": "na", "V": "v", "mV": "mv"}
    csv_opts = CsvOptions(
        time_col=args.time_col,
        current_col=args.current_col,
        voltage_col=args.voltage_col,
        time_unit=args.time_unit,
        current_unit=unit[args.current_unit] if args.current_unit else None,
        voltage_unit=unit[args.voltage_unit] if args.voltage_unit else None,
    )
    trace = load(args.log, args.format, csv_opts)
    return analyze(trace, _options(args))


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.3g} ms"
    if seconds < 120:
        return f"{seconds:.3g} s"
    return f"{seconds / 60:.3g} min"


def print_text(r: Result, out: TextIO) -> None:
    t = r.trace
    w = out.write
    w(f"{t.name}: {t.source}, {len(t)} samples, {1 / r.native_dt:.4g} Hz, ")
    w(f"{_dur(t.span_s)} ({_dur(r.covered_s)} analysed)\n")
    w(f"segments {r.segments} ({r.folded} folded), states {len(r.states)}, ")
    period = _dur(r.cycle.acf_period_s)
    w(f"wake period {period} (autocorrelation), {r.cycle.wakes} complete wakes\n\n")
    head = (
        f"{'state':<22} {'current':>10} {'events':>6} {'median dur':>10} {'period':>9} "
        f"{'uAh/event':>10} {'mAh/day':>9} {'share':>6}\n"
    )
    w(head)
    for s in r.states:
        name = s.name + (" *" if s.base else "")
        w(
            f"{name[:22]:<22} {format_ma(s.level_ma):>10} {s.events:>6} "
            f"{_dur(s.median_duration_s):>10} {_dur(s.median_period_s):>9} "
            f"{s.charge_per_event_mah * 1000:>10.4g} {s.mah_per_day:>9.4g} "
            f"{100 * s.charge_share:>5.1f}%\n"
        )
    w("(* base state)\n\n")
    w(f"mean current  {format_ma(r.mean_ma)}\n")
    w(f"charge/day    {r.mah_per_day:.4g} mAh\n")
    if r.mwh_per_day is not None and r.mean_voltage_v is not None:
        volts = f"{r.mean_voltage_v:.3g} V ({r.voltage_source})"
        w(f"energy/day    {r.mwh_per_day:.4g} mWh at {volts}\n")
    if r.battery is not None:
        b = r.battery
        w(
            f"battery       {b.capacity_mah:g} mAh x {b.depth_of_discharge:g}: "
            f"{b.days_without_self_discharge:.4g} days, {b.days_with_self_discharge:.4g} days with "
            f"{100 * b.self_discharge_per_month:g} %/month self-discharge\n"
        )
    outliers = [x for x in r.wakes if x.outlier]
    if outliers:
        w(f"\noutlier wakes ({len(outliers)}):\n")
        for x in outliers:
            w(f"  {x.start:10.2f} s  {x.outlier}\n")
    if r.warnings:
        w(f"\nwarnings ({len(r.warnings)}):\n")
        for warn in r.warnings:
            w(f"  [{warn.code}] {warn.message}\n")


def cmd_analyze(args: argparse.Namespace, out: TextIO) -> int:
    r = _run_analysis(args)
    if args.report:
        Path(args.report).write_text(render_html(r), encoding="utf-8")
    if args.events_csv:
        write_events_csv(r, args.events_csv)
    if args.json:
        out.write(json.dumps(r.to_json(), indent=2) + "\n")
    else:
        print_text(r, out)
        if args.report:
            out.write(f"\nreport written to {args.report}\n")
    return EXIT_WARN if args.strict and r.warnings else EXIT_OK


def cmd_export(args: argparse.Namespace, out: TextIO) -> int:
    r = _run_analysis(args)
    if args.full_config:
        cfg = solar_node_sizer_config(r, args.name, args.measured_at)
    else:
        cfg = solar_node_sizer_profile(r, args.name, args.measured_at)
    Path(args.solar_node_sizer).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if args.events_csv:
        write_events_csv(r, args.events_csv)
    out.write(
        f"wrote {args.solar_node_sizer}: {len(cfg['load'])} load states, "
        f"{r.mah_per_day:.4g} mAh/day measured\n"
    )
    for state in cfg["load"]:
        if state.get("base"):
            out.write(f"  {state['name']:<22} {state['mA']:>10g} mA  base\n")
        else:
            out.write(
                f"  {state['name']:<22} {state['mA']:>10g} mA  every {state['every']:g} s "
                f"for {state['duration']:g} s\n"
            )
    for warn in r.warnings:
        out.write(f"  [{warn.code}] {warn.message}\n")
    return EXIT_WARN if args.strict and r.warnings else EXIT_OK


def cmd_synth(args: argparse.Namespace, out: TextIO) -> int:
    trace, truth = generate(args.profile, args.seed, args.duration, args.rate)
    write(trace, args.out, args.format)
    out.write(
        f"wrote {args.out}: synthetic '{args.profile}' ({args.format}), {len(trace)} samples, "
        f"{truth.duration_s:g} s, true charge {truth.mah_per_day():.4g} mAh/day\n"
    )
    for name in truth.state_names():
        level = truth.charge_mah(name) * 3600 / max(truth.time_s(name), 1e-12)
        out.write(
            f"  {name:<16} {format_ma(level):>10}"
            f"  {truth.events(name):>4} events  {truth.mah_per_day(name):9.4g} mAh/day\n"
        )
    if args.states_out:
        Path(args.states_out).write_text(states_toml(args.profile), encoding="utf-8")
        out.write(f"wrote {args.states_out}\n")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stream = out if out is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            return cmd_analyze(args, stream)
        if args.command == "export":
            return cmd_export(args, stream)
        return cmd_synth(args, stream)
    except (TraceError, SslogError, StatesFileError, ExportError, ValueError) as exc:
        print(f"epp: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"epp: error: {exc.strerror or exc}: {exc.filename or ''}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
