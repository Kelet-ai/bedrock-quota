"""Detail screen for viewing a single model's usage statistics."""

import asyncio
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane
from textual_plotext import PlotextPlot

from .cloudwatch_service import CloudWatchService
from .models import ModelMetrics, TimePeriod, UsageMetrics
from .pricing_service import PricingReady, PricingService, format_cost
from .quota_service import format_number
from .time_periods import get_time_range

# Ordered list of (period_enum, tab_label)
_PERIODS: list[tuple[TimePeriod, str]] = [
    (TimePeriod.HOURS_24,      "Last 24h"),
    (TimePeriod.TODAY,         "Today"),
    (TimePeriod.DAYS_7,        "7 Days"),
    (TimePeriod.DAYS_14,       "14 Days"),
    (TimePeriod.DAYS_30,       "30 Days"),
    (TimePeriod.CURRENT_MONTH, "This Month"),
    (TimePeriod.LAST_MONTH,    "Last Month"),
]

# O(1) lookups keyed by TabPane id
_PERIOD_BY_ID: dict[str, TimePeriod] = {f"tab-{p.value}": p for p, _ in _PERIODS}
_LABEL_BY_PERIOD: dict[TimePeriod, str] = {p: lbl for p, lbl in _PERIODS}

# Periods that show a single hourly chart (no second chart)
_SHORT_WINDOWS: frozenset[TimePeriod] = frozenset({TimePeriod.HOURS_24, TimePeriod.TODAY})

# Row definitions: (metric_group_label, stat_label, field_name, quota_attr_on_QuotaLimits | None)
# field_name starting with "$" means it's a computed cost row, not a UsageMetrics attribute.
_ROWS: list[tuple[str, str, str, str | None]] = [
    ("Tokens / Day",    "P50",   "tpd_p50",             "tpd"),
    ("",                "P90",   "tpd_p90",             "tpd"),
    ("",                "Avg",   "tpd_avg",             "tpd"),
    ("",                "Max",   "tpd_max",             None),
    ("",                "",      "",                    None),  # separator
    ("Requests / Min",  "P50",   "rpm_p50",             "rpm"),
    ("",                "P90",   "rpm_p90",             "rpm"),
    ("",                "Avg",   "rpm_avg",             "rpm"),
    ("",                "Max",   "rpm_max",             None),
    ("",                "",      "",                    None),
    ("Tokens / Min",    "P50",   "tpm_p50",             "tpm"),
    ("",                "P90",   "tpm_p90",             "tpm"),
    ("",                "Avg",   "tpm_avg",             "tpm"),
    ("",                "Max",   "tpm_max",             None),
    ("",                "",      "",                    None),
    ("Latency (ms)",    "P50",   "latency_p50",         None),
    ("",                "P90",   "latency_p90",         None),
    ("",                "Avg",   "latency_avg",         None),
    ("",                "Max",   "latency_max",         None),
    ("",                "",      "",                    None),
    ("Input Tokens",    "Total", "input_tokens_total",  None),
    ("",                "TPM",   "input_tpm_avg",       None),
    ("Output Tokens",   "Total", "output_tokens_total", None),
    ("",                "TPM",   "output_tpm_avg",      None),
    ("",                "",      "",                    None),
    ("Cost (list $)",   "Input", "$cost_input",         None),
    ("",                "Output","$cost_output",        None),
    ("",                "Total", "$cost_total",         None),
    ("",                "",      "",                    None),
    ("Throttles",       "Total", "throttles_total",     None),
    ("Client Errors",   "Total", "client_errors_total", None),
    ("Server Errors",   "Total", "server_errors_total", None),
]


def _fmt_cell(val: float, warn: bool = False) -> Text:
    if val == 0.0:
        return Text("-", style="dim")
    t = Text(format_number(val))
    if warn:
        t = Text("⚠ ", style="bold yellow") + t
    return t


def _fmt_cost_cell(usd: float | None) -> Text:
    if usd is None:
        return Text("-", style="dim")
    return Text(format_cost(usd))


class ModelDetailScreen(Screen):
    CSS_PATH = "detail.tcss"

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh_data", "Refresh"),
    ]

    def __init__(
        self,
        model: ModelMetrics,
        cloudwatch_service: CloudWatchService,
        pricing_service: PricingService | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.model = model
        self.cw_svc = cloudwatch_service
        self._pricing = pricing_service
        self._results: dict[TimePeriod, UsageMetrics | None] = {}

    def _header_text(self) -> Text:
        scope_label = {
            "global-cross-region": "global cross-regional",
            "cross-region": "cross-regional",
            "on-demand": "on-demand",
        }.get(self.model.scope.value, self.model.scope.value)
        region = "global" if self.model.region == "global" else self.model.region
        cw_id = (
            min(self.model.real_model_ids, key=len)
            if self.model.real_model_ids else self.model.model_id
        )
        lim = self.model.limits

        t = Text()
        t.append(self.model.model_name, style="bold white")
        t.append("   scope: ", style="dim")
        t.append(scope_label, style="dim")
        t.append("   region: ", style="dim")
        t.append(region, style="cyan")
        t.append("\n")
        t.append(cw_id, style="dim")
        t.append("\n\n")
        t.append("Quota:  ", style="dim")
        t.append("RPM ", style="dim")
        t.append(format_number(lim.rpm), style="bold cyan")
        t.append("   TPM ", style="dim")
        t.append(format_number(lim.tpm), style="bold cyan")
        t.append("   TPD ", style="dim")
        t.append(format_number(lim.tpd), style="bold cyan")
        return t

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self._header_text(), id="detail-header")
        yield PlotextPlot(id="chart-daily")
        yield PlotextPlot(id="chart-hourly")
        yield Static("", id="chart-heatmap")
        with TabbedContent(id="detail-tabs"):
            for period, label in _PERIODS:
                with TabPane(label, id=f"tab-{period.value}"):
                    yield DataTable(
                        id=f"table-{period.value}",
                        cursor_type="none",
                        show_cursor=False,
                    )
            with TabPane("Profiles", id="tab-profiles"):
                yield DataTable(id="table-profiles", cursor_type="none", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        for period, _ in _PERIODS:
            table = self.query_one(f"#table-{period.value}", DataTable)
            self._setup_table(table)
            self._prefill_table(table)
            table.loading = True
        self._setup_profiles_table()
        self._redraw_charts_for(_PERIODS[0][0])
        self.run_worker(self._load_all(), exclusive=True)

    def _setup_profiles_table(self) -> None:
        table = self.query_one("#table-profiles", DataTable)
        table.add_column("Profile ID",    key="profile_id",  width=48)
        table.add_column("Tags",          key="tags",        width=30)
        table.add_column("Avg TPM",       key="tpm_avg",     width=10)
        table.add_column("P90 TPM",       key="tpm_p90",     width=10)
        table.add_column("Total Tokens",  key="tpd_total",   width=14)
        table.loading = True

    def _setup_table(self, table: DataTable) -> None:
        table.add_column("Metric",   key="metric",    width=16)
        table.add_column("Stat",     key="stat",      width=6)
        table.add_column("Value",    key="value",     width=14)
        table.add_column("% Quota",  key="quota_pct", width=9)

    def _prefill_table(self, table: DataTable) -> None:
        sep_count = 0
        for metric_label, stat_label, field_name, _ in _ROWS:
            if not field_name:
                table.add_row("", "", "", "", key=f"_sep{sep_count}")
                sep_count += 1
            else:
                table.add_row(
                    Text(metric_label, style="bold" if metric_label else ""),
                    Text(stat_label, style="dim"),
                    Text(""),
                    Text(""),
                    key=field_name,
                )

    # ── visibility ────────────────────────────────────────────────────────────

    def _set_chart_visibility(self, period: TimePeriod) -> None:
        is_short = period in _SHORT_WINDOWS
        self.query_one("#chart-hourly", PlotextPlot).display = not is_short
        self.query_one("#chart-heatmap", Static).display = not is_short

    # ── daily chart (top) ─────────────────────────────────────────────────────

    def _draw_daily_placeholder(self, label: str) -> None:
        w = self.query_one("#chart-daily", PlotextPlot)
        w.plt.clear_data()
        w.plt.title(f"Tokens / min  ·  {label}  (loading…)")
        w.refresh()

    def _draw_daily_no_data(self, label: str) -> None:
        w = self.query_one("#chart-daily", PlotextPlot)
        w.plt.clear_data()
        w.plt.title(f"Tokens / min  ·  {label}  (no usage data)")
        w.refresh()

    def _draw_daily_chart(
        self,
        series: list,
        period: TimePeriod,
        label: str,
        chart_period: int = 600,
        input_series: list | None = None,
        output_series: list | None = None,
    ) -> None:
        w = self.query_one("#chart-daily", PlotextPlot)
        w.plt.clear_data()

        if not series:
            w.plt.title(f"Tokens / min  ·  {label}  (no usage data)")
            w.refresh()
            return

        if period in _SHORT_WINDOWS:
            # Short windows: datetime scatter lines, input + output split
            w.plt.date_form("H:M")
            fmt = "%H:%M"
            inp_pts = sorted(input_series or [], key=lambda x: x[0])
            out_pts = sorted(output_series or [], key=lambda x: x[0])
            if inp_pts or out_pts:
                if inp_pts:
                    w.plt.plot([ts.strftime(fmt) for ts, _ in inp_pts], [v for _, v in inp_pts], color="blue", label="input")
                if out_pts:
                    w.plt.plot([ts.strftime(fmt) for ts, _ in out_pts], [v for _, v in out_pts], color="orange", label="output")
            else:
                pts = sorted(series, key=lambda x: x[0])
                w.plt.plot([ts.strftime(fmt) for ts, _ in pts], [v for _, v in pts], color="cyan", label="total")
            w.plt.title(f"Tokens / min  ·  {label}")
            w.plt.xlabel("time (UTC)")

        else:
            # Multi-day: stacked bar input/output with categorical date labels
            by_day_inp: dict[tuple, float] = {}
            by_day_out: dict[tuple, float] = {}
            for ts, v in (input_series or []):
                k = (ts.year, ts.month, ts.day)
                by_day_inp[k] = by_day_inp.get(k, 0.0) + v
            for ts, v in (output_series or []):
                k = (ts.year, ts.month, ts.day)
                by_day_out[k] = by_day_out.get(k, 0.0) + v

            # Fall back to combined series if no input/output split available
            if not by_day_inp and not by_day_out:
                by_day_tot: dict[tuple, float] = {}
                for ts, tpm in series:
                    k = (ts.year, ts.month, ts.day)
                    by_day_tot[k] = by_day_tot.get(k, 0.0) + tpm
                sorted_keys = sorted(by_day_tot)
                labels_x = [datetime(y, m, d).strftime("%-m/%-d") for y, m, d in sorted_keys]
                w.plt.bar(labels_x, [by_day_tot[k] for k in sorted_keys], color="cyan")
            else:
                sorted_keys = sorted(by_day_inp.keys() | by_day_out.keys())
                labels_x = [datetime(y, m, d).strftime("%-m/%-d") for y, m, d in sorted_keys]
                inp_vals = [by_day_inp.get(k, 0.0) for k in sorted_keys]
                out_vals = [by_day_out.get(k, 0.0) for k in sorted_keys]
                w.plt.stacked_bar(labels_x, [inp_vals, out_vals], labels=["input", "output"], color=["blue", "cyan"])

            w.plt.title(f"Tokens / day  ·  {label}")
            w.plt.xlabel("date")

        w.refresh()

    # ── hourly breakdown chart (bottom, multi-day only) ───────────────────────

    def _draw_hourly_placeholder(self, label: str) -> None:
        w = self.query_one("#chart-hourly", PlotextPlot)
        w.plt.clear_data()
        w.plt.title(f"Avg tokens / hour  ·  {label}  (loading…)")
        w.refresh()

    def _draw_hourly_no_data(self, label: str) -> None:
        w = self.query_one("#chart-hourly", PlotextPlot)
        w.plt.clear_data()
        w.plt.title(f"Avg tokens / hour  ·  {label}  (no usage data)")
        w.refresh()

    def _draw_hourly_chart(self, series: list, label: str, chart_period: int = 3600) -> None:
        w = self.query_one("#chart-hourly", PlotextPlot)
        w.plt.clear_data()

        slot_min = chart_period // 60  # slot width in minutes
        if slot_min >= 60:
            # Hourly buckets
            by_slot: dict[int, list[float]] = {}
            for ts, tpm in series:
                by_slot.setdefault(ts.hour, []).append(tpm)
            if not by_slot:
                w.plt.title(f"Avg tokens / hour  ·  {label}  (no usage data)")
                w.refresh()
                return
            all_slots = list(range(24))
            avgs = [sum(by_slot.get(h, [0.0])) / max(len(by_slot.get(h, [0.0])), 1) for h in all_slots]
            labels_x = [f"{h:02d}" for h in all_slots]
            res_label = "hour"
            xlabel = "hour of day (UTC)"
        else:
            # Sub-hourly buckets: group by (hour, rounded-minute)
            by_slot_hm: dict[tuple[int, int], list[float]] = {}
            for ts, tpm in series:
                m = (ts.minute // slot_min) * slot_min
                by_slot_hm.setdefault((ts.hour, m), []).append(tpm)
            if not by_slot_hm:
                w.plt.title(f"Avg tokens / {slot_min} min  ·  {label}  (no usage data)")
                w.refresh()
                return
            all_slots_hm = [(h, m) for h in range(24) for m in range(0, 60, slot_min)]
            avgs = [
                sum(by_slot_hm.get(s, [0.0])) / max(len(by_slot_hm.get(s, [0.0])), 1)
                for s in all_slots_hm
            ]
            labels_x = [f"{h:02d}:00" if m == 0 else "" for h, m in all_slots_hm]
            res_label = f"{slot_min} min"
            xlabel = "time of day (UTC)"

        w.plt.bar(labels_x, avgs, color="blue")
        w.plt.title(f"Avg tokens / {res_label}  ·  {label}")
        w.plt.xlabel(xlabel)
        w.refresh()

    # ── heatmap (day × hour, multi-day only, rendered with Rich block chars) ──

    # 5 activity levels (0 = none, 1–4 = low→high), thresholds as fractions of max
    _LEVEL_THRESHOLDS = (0.0, 0.15, 0.40, 0.70, 1.01)  # upper bound for each level

    def _heatmap_palette(self) -> list[tuple[int,int,int]]:
        """Return 5 RGB colors: level 0 (none) → level 4 (max), from theme."""
        from textual.color import Color
        try:
            css_vars = self.app.get_css_variables()
            lo = Color.parse(css_vars.get("surface", "#14141e"))
            hi = Color.parse(css_vars.get("accent", "#00c8ff"))
        except Exception:
            lo = Color.parse("#14141e")
            hi = Color.parse("#00c8ff")
        return [
            (lo.r, lo.g, lo.b),
            (
                int(lo.r + 0.25 * (hi.r - lo.r)),
                int(lo.g + 0.25 * (hi.g - lo.g)),
                int(lo.b + 0.25 * (hi.b - lo.b)),
            ),
            (
                int(lo.r + 0.50 * (hi.r - lo.r)),
                int(lo.g + 0.50 * (hi.g - lo.g)),
                int(lo.b + 0.50 * (hi.b - lo.b)),
            ),
            (
                int(lo.r + 0.75 * (hi.r - lo.r)),
                int(lo.g + 0.75 * (hi.g - lo.g)),
                int(lo.b + 0.75 * (hi.b - lo.b)),
            ),
            (hi.r, hi.g, hi.b),
        ]

    @staticmethod
    def _heatmap_level(val: float, max_val: float) -> int:
        if val <= 0 or max_val <= 0:
            return 0
        t = val / max_val
        for level, upper in enumerate(ModelDetailScreen._LEVEL_THRESHOLDS):
            if t < upper:
                return level
        return 4

    def _heatmap_markup(
        self,
        by_day_hour: dict,
        sorted_days: list,
        max_val: float,
        palette: list[tuple[int,int,int]],
    ) -> str:
        """Build Rich markup: one row per day, 24 ██ blocks per row, with a date label."""
        label_width = 6
        lines: list[str] = []

        # Header: hour labels every 3 hours
        hour_header = " " * label_width
        for h in range(24):
            hour_header += f"{h:02d} " if h % 3 == 0 else "   "
        lines.append(f"[dim]{hour_header}[/dim]")

        for dk in sorted_days:
            date_str = datetime(*dk).strftime("%-m/%-d").ljust(label_width)
            row = f"[dim]{date_str}[/dim]"
            for hour in range(24):
                val = by_day_hour.get((dk, hour), 0.0)
                r, g, b = palette[self._heatmap_level(val, max_val)]
                row += f"[rgb({r},{g},{b})]██[/rgb({r},{g},{b})]"
            lines.append(row)

        # Legend: 5 swatches with labels
        legend = " " * label_width + "[dim]less  [/dim]"
        for r, g, b in palette:
            legend += f"[rgb({r},{g},{b})]██[/rgb({r},{g},{b})]"
        legend += f"[dim]  more  (max {format_number(max_val)} TPM)[/dim]"
        lines.append("")
        lines.append(legend)

        return "\n".join(lines)

    def _draw_heatmap_placeholder(self, label: str) -> None:
        w = self.query_one("#chart-heatmap", Static)
        w.update(f"[dim]Token activity  ·  {label}  (loading…)[/dim]")

    def _draw_heatmap_no_data(self, label: str) -> None:
        w = self.query_one("#chart-heatmap", Static)
        w.update(f"[dim]Token activity  ·  {label}  (no usage data)[/dim]")

    def _draw_heatmap(self, series: list, label: str) -> None:
        w = self.query_one("#chart-heatmap", Static)

        if not series:
            w.update(f"[dim]Token activity  ·  {label}  (no usage data)[/dim]")
            return

        by_day_hour: dict[tuple, float] = {}
        day_keys: set[tuple] = set()
        for ts, tpm in series:
            dk = (ts.year, ts.month, ts.day)
            day_keys.add(dk)
            key = (dk, ts.hour)
            by_day_hour[key] = by_day_hour.get(key, 0.0) + tpm

        sorted_days = sorted(day_keys)
        if not sorted_days:
            w.update(f"[dim]Token activity  ·  {label}  (no usage data)[/dim]")
            return

        max_val = max(by_day_hour.values(), default=1.0) or 1.0
        palette = self._heatmap_palette()

        title = f"[bold]Token activity  ·  {label}[/bold]"
        body = self._heatmap_markup(by_day_hour, sorted_days, max_val, palette)
        w.update(title + "\n" + body)

    # ── unified redraw ────────────────────────────────────────────────────────

    def _redraw_charts_for(self, period: TimePeriod) -> None:
        label = _LABEL_BY_PERIOD[period]
        self._set_chart_visibility(period)

        if period not in self._results:
            self._draw_daily_placeholder(label)
            if period not in _SHORT_WINDOWS:
                self._draw_hourly_placeholder(label)
                self._draw_heatmap_placeholder(label)
            return

        result = self._results[period]

        cp = result.chart_period if result else 600
        if result and result.tpm_series:
            self._draw_daily_chart(
                result.tpm_series, period, label, cp,
                input_series=result.input_series or None,
                output_series=result.output_series or None,
            )
        else:
            self._draw_daily_no_data(label)

        if period not in _SHORT_WINDOWS:
            if result and result.tpm_series:
                self._draw_hourly_chart(result.tpm_series, label, cp)
                self._draw_heatmap(result.tpm_series, label)
            else:
                self._draw_hourly_no_data(label)
                self._draw_heatmap_no_data(label)

    # ── table update ──────────────────────────────────────────────────────────

    def _compute_costs(self, result: UsageMetrics) -> tuple[float | None, float | None, float | None]:
        """Return (cost_input, cost_output, cost_total) or (None, None, None) if price unknown."""
        if self._pricing is None or not self._pricing.is_ready():
            return None, None, None
        region = self.model.region if self.model.region != "global" else ""
        price = self._pricing.get(self.model.model_id, region)
        if price is None:
            return None, None, None
        cost_in = result.input_tokens_total / 1000.0 * price.input_per_1k
        cost_out = result.output_tokens_total / 1000.0 * price.output_per_1k
        return cost_in, cost_out, cost_in + cost_out

    def _update_tab(self, period: TimePeriod, result: UsageMetrics | None) -> None:
        table = self.query_one(f"#table-{period.value}", DataTable)
        table.loading = False
        lim = self.model.limits

        cost_in, cost_out, cost_total = (
            self._compute_costs(result) if result is not None else (None, None, None)
        )

        for _, _, field_name, quota_attr in _ROWS:
            if not field_name:
                continue

            # Cost rows — computed separately
            if field_name.startswith("$"):
                if field_name == "$cost_input":
                    cell = _fmt_cost_cell(cost_in)
                elif field_name == "$cost_output":
                    cell = _fmt_cost_cell(cost_out)
                else:
                    cell = _fmt_cost_cell(cost_total)
                try:
                    table.update_cell(field_name, "value", cell)
                    table.update_cell(field_name, "quota_pct", Text(""))
                except Exception:
                    pass
                continue

            quota_val: float | None = getattr(lim, quota_attr, None) if quota_attr else None

            if result is None:
                val_cell = Text("-", style="dim")
                pct_cell = Text("")
            else:
                val = getattr(result, field_name, 0.0)
                if quota_val and quota_val > 0 and val > 0:
                    pct = val / quota_val
                    warn = pct >= 0.8
                    pct_cell = Text(f"{pct:.0%}", style="bold yellow" if warn else "dim")
                else:
                    warn = False
                    pct_cell = Text("")
                val_cell = _fmt_cell(val, warn=warn)

            try:
                table.update_cell(field_name, "value", val_cell)
                table.update_cell(field_name, "quota_pct", pct_cell)
            except Exception:
                pass

    # ── event handlers ────────────────────────────────────────────────────────

    def on_pricing_ready(self, _: PricingReady) -> None:
        """Re-render cost cells for all periods that already have data."""
        for period, _ in _PERIODS:
            result = self._results.get(period)
            if result is not None:
                cost_in, cost_out, cost_total = self._compute_costs(result)
                table = self.query_one(f"#table-{period.value}", DataTable)
                for field_name, usd in (
                    ("$cost_input", cost_in),
                    ("$cost_output", cost_out),
                    ("$cost_total", cost_total),
                ):
                    try:
                        table.update_cell(field_name, "value", _fmt_cost_cell(usd))
                    except Exception:
                        pass

    def on_app_theme_changed(self) -> None:
        """Redraw the heatmap with the new theme's accent/surface colors."""
        active = self.query_one("#detail-tabs", TabbedContent).active
        period = _PERIOD_BY_ID.get(active or "")
        if period is None or period in _SHORT_WINDOWS:
            return
        result = self._results.get(period)
        if result and result.tpm_series:
            self._draw_heatmap(result.tpm_series, _LABEL_BY_PERIOD[period])

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane_id = event.pane.id  # raw TabPane id, e.g. "tab-24h"
        period = _PERIOD_BY_ID.get(pane_id or "")
        if period is None:
            return
        self._redraw_charts_for(period)

    async def _load_all(self) -> None:
        _today_range = get_time_range(TimePeriod.TODAY)

        async def fetch_one(period: TimePeriod) -> None:
            time_range = get_time_range(period)
            result = await asyncio.to_thread(
                self.cw_svc.get_usage_metrics,
                self.model.model_id,
                time_range,
                _today_range,
            )
            self._results[period] = result
            self._update_tab(period, result)

            active = self.query_one("#detail-tabs", TabbedContent).active
            if active == f"tab-{period.value}":
                self._redraw_charts_for(period)

        async def fetch_profiles() -> None:
            time_range = get_time_range(TimePeriod.DAYS_7)
            contribs = await asyncio.to_thread(
                self.cw_svc.get_profile_contributions,
                self.model.model_id,
                time_range,
            )
            table = self.query_one("#table-profiles", DataTable)
            table.loading = False
            for c in contribs:
                tags_str = ", ".join(f"{k}={v}" for k, v in c.tags.items()) if c.tags else "-"
                table.add_row(
                    Text(c.profile_id, style="dim"),
                    Text(tags_str, style="dim"),
                    _fmt_cell(c.tpm_avg),
                    _fmt_cell(c.tpm_p90),
                    _fmt_cell(c.tpd_total),
                )

        await asyncio.gather(*[fetch_one(p) for p, _ in _PERIODS], fetch_profiles())

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh_data(self) -> None:
        self._results.clear()
        for period, _ in _PERIODS:
            table = self.query_one(f"#table-{period.value}", DataTable)
            table.loading = True

        active = self.query_one("#detail-tabs", TabbedContent).active
        period = _PERIOD_BY_ID.get(active or "")
        if period:
            self._redraw_charts_for(period)
        self.run_worker(self._load_all(), exclusive=True)
