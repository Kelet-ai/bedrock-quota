"""Modal screen for selecting AWS regions."""

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, LoadingIndicator, SelectionList, Static

from .aws_client import AWSClient


def _fetch_regions(aws_client: AWSClient) -> list[str]:
    """Return all enabled regions via EC2 describe_regions."""
    ec2 = aws_client.get_client("ec2")
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return sorted(r["RegionName"] for r in resp.get("Regions", []))


class RegionScreen(ModalScreen[set[str] | None]):
    """Modal for picking AWS regions. Dismisses with the full desired set of regions or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, active_regions: set[str], aws_client: AWSClient):
        super().__init__()
        self.active_regions = active_regions
        self.aws_client = aws_client

    def compose(self) -> ComposeResult:
        with Vertical(id="region-dialog"):
            yield Static("Select AWS Regions", id="region-title")
            yield LoadingIndicator(id="region-loading")
            with Horizontal(id="region-buttons"):
                yield Button("Apply", id="confirm", variant="primary", disabled=True)
                yield Button("Cancel", id="cancel")

    async def on_mount(self) -> None:
        self.run_worker(self._load_regions(), exclusive=True)

    async def _load_regions(self) -> None:
        try:
            regions = await asyncio.to_thread(_fetch_regions, self.aws_client)
        except Exception:
            regions = []

        loading = self.query_one("#region-loading", LoadingIndicator)

        if not regions:
            loading.remove()
            self.query_one("#region-dialog", Vertical).mount(
                Static("[red]Could not fetch regions — check AWS credentials[/red]"),
                before="#region-buttons",
            )
            return

        # Pre-check currently active regions
        selections = [(r, r, r in self.active_regions) for r in regions]
        region_list = SelectionList[str](*selections, id="region-list")
        await loading.remove()
        await self.query_one("#region-dialog", Vertical).mount(region_list, before="#region-buttons")
        self.query_one("#confirm", Button).disabled = False
        region_list.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            try:
                selected = self.query_one(SelectionList).selected
                self.dismiss(set(selected) if selected else None)
            except Exception:
                self.dismiss(None)
        else:
            self.dismiss(None)
