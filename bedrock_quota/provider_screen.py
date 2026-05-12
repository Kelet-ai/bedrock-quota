"""Modal screen for selecting Bedrock model providers."""

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, LoadingIndicator, SelectionList, Static

from .aws_client import AWSClient


def _fetch_providers(aws_client: AWSClient) -> list[tuple[str, str]]:
    """Return sorted list of (display_name, provider_id) from Bedrock list_foundation_models."""
    bedrock = aws_client.get_client("bedrock")
    resp = bedrock.list_foundation_models()

    seen: set[str] = set()
    providers: list[tuple[str, str]] = []

    for model in resp.get("modelSummaries", []):
        model_id = model.get("modelId", "")
        provider_name = model.get("providerName", "")
        # provider_id is the prefix before the first dot: "anthropic.claude-..." → "anthropic"
        provider_id = model_id.split(".")[0] if "." in model_id else model_id.lower()

        if provider_id and provider_id not in seen:
            seen.add(provider_id)
            providers.append((provider_name or provider_id.title(), provider_id))

    return sorted(providers, key=lambda x: x[0])


class ProviderScreen(ModalScreen):
    """Modal for picking Bedrock providers. Dismisses with the full desired set or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, active_providers: set[str], aws_client: AWSClient):
        super().__init__()
        self.active_providers = active_providers
        self.aws_client = aws_client

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog"):
            yield Static("Select Providers", id="provider-title")
            yield LoadingIndicator(id="provider-loading")
            with Horizontal(id="provider-buttons"):
                yield Button("Apply", id="confirm", variant="primary", disabled=True)
                yield Button("Cancel", id="cancel")

    async def on_mount(self) -> None:
        self.run_worker(self._load_providers(), exclusive=True)

    async def _load_providers(self) -> None:
        try:
            providers = await asyncio.to_thread(_fetch_providers, self.aws_client)
        except Exception:
            providers = []

        loading = self.query_one("#provider-loading", LoadingIndicator)

        if not providers:
            loading.remove()
            self.query_one("#provider-dialog", Vertical).mount(
                Static("[red]Could not fetch providers — check AWS credentials[/red]"),
                before="#provider-buttons",
            )
            return

        selections = [
            (display, pid, pid in self.active_providers)
            for display, pid in providers
        ]
        provider_list = SelectionList[str](*selections, id="provider-list")
        await loading.remove()
        await self.query_one("#provider-dialog", Vertical).mount(provider_list, before="#provider-buttons")
        self.query_one("#confirm", Button).disabled = False
        provider_list.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            try:
                selected = self.query_one(SelectionList).selected
                self.dismiss(set(selected) if selected else None)
            except Exception:
                self.dismiss(None)
        else:
            self.dismiss(None)
