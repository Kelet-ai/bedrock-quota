"""Textual TUI application for AWS Bedrock quotas with CloudWatch usage metrics."""

import asyncio
import re

from botocore.exceptions import ClientError, NoCredentialsError
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from .aws_client import AWSClient
from .cloudwatch_service import CloudWatchService
from .detail_screen import ModelDetailScreen
from .models import ModelMetrics, Scope
from .provider_screen import ProviderScreen
from .quota_service import QuotaService, format_number
from .region_screen import RegionScreen


class SkippingDataTable(DataTable):
    """DataTable that skips header/separator rows (no explicit key) during arrow navigation."""

    def _is_data_row(self, row_index: int) -> bool:
        ordered = self.ordered_rows
        if row_index < 0 or row_index >= len(ordered):
            return False
        return ordered[row_index].key.value is not None

    def action_cursor_up(self) -> None:
        super().action_cursor_up()
        while not self._is_data_row(self.cursor_row):
            if self.cursor_row == 0:
                super().action_cursor_down()
                break
            super().action_cursor_up()

    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        while not self._is_data_row(self.cursor_row):
            if self.cursor_row == self.row_count - 1:
                super().action_cursor_up()
                break
            super().action_cursor_down()


class QuitConfirmScreen(ModalScreen):
    """Modal asking the user to confirm quit."""

    BINDINGS = [
        Binding("ctrl+c", "confirm", "Yes", show=False),
        Binding("escape", "cancel", "No", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Quit Bedrock Quotas Dashboard?")
            with Container(id="buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class BedrockTUI(App):
    """Textual TUI for AWS Bedrock quotas with CloudWatch usage metrics."""

    TITLE = "Bedrock Quotas Dashboard"
    CSS_PATH = "app.tcss"
    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _SCOPE_LABEL = {
        "global-cross-region": "global",
        "cross-region":        "cross-regional",
        "on-demand":           "on-demand",
    }

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "confirm_quit", "Quit", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("g", "select_region", "Region"),
        Binding("p", "select_provider", "Provider"),
    ]

    def __init__(self):
        super().__init__()
        self._default_aws_client = AWSClient()
        self.active_regions: set[str] = {self._default_aws_client.aws_region}
        self.active_providers: set[str] = {"anthropic"}
        # Maps region -> (AWSClient, QuotaService, CloudWatchService)
        self._region_services: dict[str, tuple[AWSClient, QuotaService, CloudWatchService]] = {}
        self.all_metrics: list[ModelMetrics] = []
        # Tracks in-flight load phases: maps a label -> short description
        self._pending_loads: dict[str, str] = {}
        self._spinner_frame: int = 0
        # Provider display names populated from API (providerName field)
        self._provider_display: dict[str, str] = {}

    def _services_for(self, region: str) -> tuple[AWSClient, QuotaService, CloudWatchService]:
        if region not in self._region_services:
            aws = AWSClient(region=region)
            self._region_services[region] = (aws, QuotaService(aws), CloudWatchService(aws))
        return self._region_services[region]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Container():
            yield SkippingDataTable(id="quota-table", cursor_type="row")

        with Vertical(id="status"):
            yield Static("", id="status-text")

        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(0.1, self._tick_spinner)
        table = self.query_one("#quota-table", DataTable)
        self._setup_table(table)
        table.loading = True
        default_region = self._default_aws_client.aws_region
        self.run_worker(
            self._load_region(default_region, include_global=True),
            exclusive=False,
            name=f"load-{default_region}",
        )

    def _setup_table(self, table: DataTable) -> None:
        table.add_column("Provider / Region", key="region", width=28)
        table.add_column("Model", key="model", width=24)
        table.add_column("Scope", key="scope", width=16)
        table.add_column("CloudWatch ID", key="model-id")
        table.add_column("Recent TPD", key="recent-tpd", width=18)
        table.add_column("RPM", key="rpm-limit", width=8)
        table.add_column("TPM", key="tpm-limit", width=8)
        table.add_column("TPD", key="tpd-limit", width=9)
        table.zebra_stripes = True
        table.show_header = True

    async def action_confirm_quit(self) -> None:
        def handle(confirmed: bool) -> None:
            if confirmed:
                self.exit()

        await self.push_screen(QuitConfirmScreen(), handle)

    async def action_refresh(self) -> None:
        self.all_metrics = []
        self._region_services.clear()
        self._pending_loads.clear()
        table = self.query_one("#quota-table", DataTable)
        table.clear(columns=False)
        table.loading = True

        regions = list(self.active_regions)
        self.run_worker(
            self._load_region(regions[0], include_global=True),
            exclusive=False,
            name=f"load-{regions[0]}",
        )
        for r in regions[1:]:
            self.run_worker(
                self._load_region(r, include_global=False),
                exclusive=False,
                name=f"load-{r}",
            )

    async def action_select_region(self) -> None:
        self.run_worker(self._region_flow(), exclusive=False, name="region-flow")

    async def _region_flow(self) -> None:
        new_set: set[str] | None = await self.push_screen_wait(
            RegionScreen(self.active_regions, self._default_aws_client)
        )
        if new_set is None:
            return
        to_add = new_set - self.active_regions
        to_remove = self.active_regions - new_set
        if not to_add and not to_remove:
            return

        # Drop removed regions' rows and cached screens
        self.all_metrics = [
            m for m in self.all_metrics
            if m.region == "global" or m.region not in to_remove
        ]
        for r in to_remove:
            self._region_services.pop(r, None)
        self.active_regions = new_set
        self._repopulate_table()

        for region in to_add:
            self.run_worker(
                self._load_region(region, include_global=False),
                exclusive=False,
                name=f"load-{region}",
            )

    async def action_select_provider(self) -> None:
        self.run_worker(self._provider_flow(), exclusive=False, name="provider-flow")

    async def _provider_flow(self) -> None:
        new_set: set[str] | None = await self.push_screen_wait(
            ProviderScreen(self.active_providers, self._default_aws_client)
        )
        if new_set is None:
            return
        to_add = new_set - self.active_providers
        to_remove = self.active_providers - new_set
        if not to_add and not to_remove:
            return

        # Drop removed providers' rows and cached screens
        self.all_metrics = [
            m for m in self.all_metrics
            if m.provider not in to_remove
        ]
        self.active_providers = new_set
        self._repopulate_table()

        # Re-fetch all active regions to load newly added providers
        if to_add:
            regions = list(self.active_regions)
            self.run_worker(
                self._load_region(regions[0], include_global=True),
                exclusive=False,
                name=f"load-{regions[0]}-p",
            )
            for region in regions[1:]:
                self.run_worker(
                    self._load_region(region, include_global=False),
                    exclusive=False,
                    name=f"load-{region}-p",
                )

    def _sort_key(self, metrics: ModelMetrics) -> tuple:
        model = metrics.model_name
        version_match = re.search(r"(\d+)\.?(\d*)", model)
        if version_match:
            major = int(version_match.group(1))
            minor = int(version_match.group(2)) if version_match.group(2) else 0
        else:
            major, minor = 0, 0

        tier_priority = 0
        model_lower = model.lower()
        if "opus" in model_lower:
            tier_priority = 1
        elif "sonnet" in model_lower:
            tier_priority = 2
        elif "haiku" in model_lower:
            tier_priority = 3

        region_order = "" if metrics.region == "global" else metrics.region
        return (metrics.provider, region_order, metrics.is_legacy, -major, -minor, tier_priority, model, metrics.scope.value)

    def _tpd_display(self, m: ModelMetrics) -> Text:
        if not m.recent_tpd_fetched:
            return Text("…", style="dim")
        if m.recent_tpd is None:
            return Text("-", style="dim")
        tokens, date_label = m.recent_tpd
        t = Text(format_number(tokens))
        if m.limits.tpd > 0:
            pct = tokens / m.limits.tpd
            t.append(f" {pct:.0%}", style="dim")
        t.append(f" ({date_label})", style="dim")
        if m.p90_tpm > 0 and m.limits.tpm > 0 and m.p90_tpm / m.limits.tpm > 0.8:
            t.append(" ⚠", style="bold yellow")
        return t

    @staticmethod
    def _short_cw_id(model_id: str) -> str:
        """Strip date stamp and version suffix for compact display."""
        s = re.sub(r"-\d{8}", "", model_id)
        s = re.sub(r"-?v[\d:\.]+$", "", s)
        s = re.sub(r":[\d]+$", "", s)
        return s

    def _repopulate_table(self) -> None:
        table = self.query_one("#quota-table", DataTable)
        table.clear(columns=False)

        prev_provider: str | None = None
        prev_region: str | None = None
        prev_model: str | None = None
        blank = ("", "", "", "", "", "", "", "")

        for m in self.all_metrics:
            is_first_provider = m.provider  != prev_provider
            is_first_region   = m.region    != prev_region or is_first_provider
            is_first_model    = m.model_name != prev_model  or is_first_region

            # Separator + group header row at each new provider·region boundary
            if is_first_provider and prev_provider is not None:
                table.add_row(*blank)

            if is_first_region:
                region_label = "global" if m.region == "global" else m.region
                provider_label = self._provider_display.get(m.provider, m.provider.title())
                header = Text()
                header.append(f" {provider_label}", style="bold white")
                header.append(" · ", style="dim")
                header.append(region_label, style="bold cyan")
                table.add_row(header, Text(""), Text(""), Text(""), Text(""), Text(""), Text(""), Text(""))

            # Data row
            raw_name = f"🏛️ {m.model_name}" if m.is_legacy else m.model_name
            model_cell = Text(f"  {raw_name}" if is_first_model else "", style="bold" if is_first_model else "")

            scope_label = self._SCOPE_LABEL.get(m.scope.value, m.scope.value)
            scope_cell = Text(scope_label, style="dim")

            cw_id = self._short_cw_id(min(m.real_model_ids, key=len)) if m.real_model_ids else "-"
            cw_cell = Text(cw_id, style="dim")

            table.add_row(
                Text(""),
                model_cell,
                scope_cell,
                cw_cell,
                self._tpd_display(m),
                format_number(m.limits.rpm),
                format_number(m.limits.tpm),
                format_number(m.limits.tpd),
                key=str(id(m)),
            )

            prev_provider = m.provider
            prev_region   = m.region
            prev_model    = m.model_name

        for i, row in enumerate(table.ordered_rows):
            if row.key.value is not None:
                table.move_cursor(row=i)
                break

    def _tick_spinner(self) -> None:
        if self._pending_loads:
            self._spinner_frame = (self._spinner_frame + 1) % len(self._SPINNER)
            self._refresh_status()

    def _refresh_status(self) -> None:
        status = self.query_one("#status-text", Static)
        if self._pending_loads:
            spinner = self._SPINNER[self._spinner_frame]
            msgs = list(self._pending_loads.values())
            status.update(f"{spinner}  " + "  ·  ".join(msgs))
        else:
            regions = sorted(self.active_regions)
            providers = sorted(self.active_providers)
            region_str   = ", ".join(f"[bold]{r}[/bold]" for r in regions)
            provider_str = ", ".join(
                f"[bold]{self._provider_display.get(p, p.title())}[/bold]" for p in providers
            )
            total = len(self.all_metrics)
            status.update(
                f"{total} models · {region_str} · {provider_str} · "
                f"[bold]Enter[/bold] details · [bold]r[/bold] refresh · "
                f"[bold]g[/bold] region · [bold]p[/bold] provider"
            )

    async def _load_region(self, region: str, include_global: bool = False) -> None:
        table = self.query_one("#quota-table", DataTable)
        load_key = f"{region}-{'global' if include_global else 'regional'}"

        def _set_phase(msg: str) -> None:
            self._pending_loads[load_key] = msg
            self._refresh_status()

        def _clear_phase() -> None:
            self._pending_loads.pop(load_key, None)
            self._refresh_status()

        try:
            _, quota_svc, cw_svc = self._services_for(region)

            # Phase 1: fetch quotas
            _set_phase(f"{region}: fetching quotas…")
            quotas = await asyncio.to_thread(quota_svc.fetch_quotas)

            # Merge provider display names from this region's fetch
            self._provider_display.update(quota_svc._provider_display)

            # Dedup guard: skip rows already loaded (prevents duplicates on provider-add reload)
            existing_keys = {
                (m.provider, m.model_id, m.scope, m.region)
                for m in self.all_metrics
            }

            new_metrics: list[ModelMetrics] = []
            for (provider_id, model_id), (display_name, is_legacy, scopes) in quotas.items():
                if provider_id not in self.active_providers:
                    continue

                for scope, limits in scopes.items():
                    if not include_global and scope == Scope.GLOBAL_CROSS_REGION:
                        continue
                    row_region = "global" if scope == Scope.GLOBAL_CROSS_REGION else region
                    key = (provider_id, model_id, scope, row_region)
                    if key in existing_keys:
                        continue
                    new_metrics.append(ModelMetrics(
                        model_name=display_name,
                        model_id=model_id,
                        provider=provider_id,
                        scope=scope,
                        is_legacy=is_legacy,
                        limits=limits,
                        region=row_region,
                        cloudwatch_service=cw_svc,
                    ))

            self.all_metrics = sorted(
                self.all_metrics + new_metrics,
                key=self._sort_key,
            )
            table.loading = False
            self._repopulate_table()

            # Phase 2: discover real CloudWatch model IDs
            _set_phase(f"{region}: mapping model IDs…")
            await asyncio.to_thread(cw_svc.discover_model_ids)
            for m in new_metrics:
                all_ids = cw_svc.find_matching_model_ids(m.model_id)
                m.real_model_ids = cw_svc.filter_ids_by_scope(all_ids, m.scope) or None
            self._repopulate_table()

            # Phase 3: fetch recent TPD concurrently
            done_count = 0
            total = len(new_metrics)
            _set_phase(f"{region}: usage 0/{total}…")

            async def fetch_one(m: ModelMetrics) -> None:
                nonlocal done_count
                recent_tpd, p90_tpm = await asyncio.gather(
                    asyncio.to_thread(cw_svc.get_recent_tpd, m.model_id, m.real_model_ids),
                    asyncio.to_thread(cw_svc.get_p90_tpm_7d, m.model_id, m.real_model_ids),
                )
                m.recent_tpd = recent_tpd
                m.p90_tpm = p90_tpm or 0.0
                m.recent_tpd_fetched = True
                try:
                    table.update_cell(str(id(m)), "recent-tpd", self._tpd_display(m))
                except Exception:
                    pass
                done_count += 1
                _set_phase(f"{region}: usage {done_count}/{total}…")

            await asyncio.gather(*[fetch_one(m) for m in new_metrics])
            _clear_phase()

        except NoCredentialsError:
            table.loading = False
            _clear_phase()
            self.query_one("#status-text", Static).update(
                "No AWS credentials — run [bold]aws configure[/bold] then press [bold]r[/bold] to retry"
            )
        except ClientError as e:
            table.loading = False
            _clear_phase()
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "Throttling":
                self.query_one("#status-text", Static).update(
                    "Rate limited by AWS — press [bold]r[/bold] to retry in a moment"
                )
            else:
                self.query_one("#status-text", Static).update(
                    f"AWS error: [bold]{error_code or 'unknown'}[/bold] — press [bold]r[/bold] to retry"
                )
        except Exception as e:
            table.loading = False
            _clear_phase()
            self.query_one("#status-text", Static).update(
                f"Unexpected error — press [bold]r[/bold] to retry  ({e})"
            )

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key.value if event.row_key else None
        if not row_key:
            return
        selected_metric = next((m for m in self.all_metrics if str(id(m)) == row_key), None)
        if selected_metric is None:
            return

        cw_svc: CloudWatchService = (
            selected_metric.cloudwatch_service  # type: ignore[assignment]
            or self._services_for(self._default_aws_client.aws_region)[2]
        )

        await self.push_screen(
            ModelDetailScreen(model=selected_metric, cloudwatch_service=cw_svc)
        )
