"""Pricing service — fetches AWS Bedrock list prices from the AWS Pricing API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.message import Message

if TYPE_CHECKING:
    from .aws_client import AWSClient


class PricingReady(Message):
    """Posted when the PricingService background fetch completes (success or failure)."""


_SCOPE_PREFIXES = ("global.", "us.", "eu.", "ap.", "sa.", "ca.", "me.", "af.")


def _to_slug(model_id: str) -> str:
    """Strip scope prefix, date stamp and version suffix — mirrors CloudWatchService._to_slug."""
    s = model_id.lower()
    for p in _SCOPE_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    s = re.sub(r"-\d{8}", "", s)
    s = re.sub(r"-?v[\d:\.]+$", "", s)
    s = re.sub(r":[\d]+$", "", s)
    return s.strip("-")


@dataclass
class ModelPrice:
    input_per_1k: float   # USD per 1,000 input tokens
    output_per_1k: float  # USD per 1,000 output tokens


def format_cost(usd: float) -> str:
    """Format a USD dollar amount for display."""
    if usd < 0.01:
        return "<$0.01"
    if usd >= 1_000:
        return f"${usd:,.0f}"
    if usd >= 10:
        return f"${usd:,.2f}"
    return f"${usd:.4f}".rstrip("0").rstrip(".")


class PricingService:
    """Fetch and cache AWS Bedrock per-token prices from the AWS Pricing API."""

    def __init__(self, aws_client: AWSClient):
        self._client = aws_client
        # (slug, region) -> ModelPrice; populated by fetch()
        self._prices: dict[tuple[str, str], ModelPrice] = {}
        self._fetched = False
        self._fetch_failed = False

    def is_ready(self) -> bool:
        return self._fetched

    def fetch_failed(self) -> bool:
        return self._fetch_failed

    def _fetch_sync(self) -> None:
        """Synchronous Pricing API call — run via asyncio.to_thread."""
        prices: dict[tuple[str, str], ModelPrice] = {}
        try:
            # Build modelName -> slug lookup from ListFoundationModels.
            # The Pricing API uses modelName as its "model" attribute.
            bedrock = self._client.get_client("bedrock")
            fms = bedrock.list_foundation_models().get("modelSummaries", [])
            name_to_slug: dict[str, str] = {
                fm["modelName"]: _to_slug(fm["modelId"])
                for fm in fms
                if fm.get("modelName") and fm.get("modelId")
            }

            client = self._client.pricing
            paginator = client.get_paginator("get_products")
            for page in paginator.paginate(ServiceCode="AmazonBedrock"):
                for item_str in page.get("PriceList", []):
                    try:
                        item = json.loads(item_str) if isinstance(item_str, str) else item_str
                    except (json.JSONDecodeError, TypeError):
                        continue
                    attrs = item.get("product", {}).get("attributes", {})
                    model_name = attrs.get("model", "")
                    inference_type = attrs.get("inferenceType", "").lower()
                    region = attrs.get("regionCode", "")

                    if not model_name or not region:
                        continue

                    # Only plain on-demand input/output token charges
                    if inference_type == "input tokens":
                        direction = "input"
                    elif inference_type == "output tokens":
                        direction = "output"
                    else:
                        continue

                    slug = name_to_slug.get(model_name)
                    if not slug:
                        continue

                    price_per_1k = _extract_price_per_1k(item)
                    if price_per_1k is None:
                        continue

                    key = (slug, region)
                    existing = prices.get(key)
                    if existing is None:
                        prices[key] = ModelPrice(
                            input_per_1k=price_per_1k if direction == "input" else 0.0,
                            output_per_1k=price_per_1k if direction == "output" else 0.0,
                        )
                    elif direction == "input":
                        prices[key] = ModelPrice(
                            input_per_1k=price_per_1k,
                            output_per_1k=existing.output_per_1k,
                        )
                    else:
                        prices[key] = ModelPrice(
                            input_per_1k=existing.input_per_1k,
                            output_per_1k=price_per_1k,
                        )
        except Exception:
            self._fetch_failed = True
            self._fetched = True
            return

        self._prices = prices
        self._fetch_failed = False
        self._fetched = True

    async def fetch(self) -> None:
        """Fetch prices in a background thread."""
        import asyncio
        await asyncio.to_thread(self._fetch_sync)

    def get(self, model_id: str, region: str) -> ModelPrice | None:
        """Look up price for a model+region, with fallback chain: exact region → us-east-1 → hardcoded table."""
        if not self._fetched:
            return None
        slug = _to_slug(model_id)
        return (
            self._prices.get((slug, region))
            or self._prices.get((slug, "us-east-1"))
            or _FALLBACK_PRICES.get(slug)
        )

    def cost(
        self,
        input_tokens: float,
        output_tokens: float,
        model_id: str,
        region: str,
    ) -> float | None:
        """Return total USD cost, or None if price is unknown."""
        price = self.get(model_id, region)
        if price is None:
            return None
        return (input_tokens / 1000.0 * price.input_per_1k) + (output_tokens / 1000.0 * price.output_per_1k)


# Hardcoded fallback for models absent from the AWS Pricing API.
# Keyed by _to_slug(modelId) → ModelPrice (USD per 1K tokens, list price).
# Source: https://aws.amazon.com/bedrock/pricing/ — update when AWS changes list prices.
_FALLBACK_PRICES: dict[str, ModelPrice] = {
    # ── Anthropic Claude 4 ────────────────────────────────────────────────────
    "anthropic.claude-opus-4-7":        ModelPrice(input_per_1k=0.015,    output_per_1k=0.075),
    "anthropic.claude-opus-4-6":        ModelPrice(input_per_1k=0.015,    output_per_1k=0.075),
    "anthropic.claude-opus-4-5":        ModelPrice(input_per_1k=0.015,    output_per_1k=0.075),
    "anthropic.claude-sonnet-4-6":      ModelPrice(input_per_1k=0.003,    output_per_1k=0.015),
    "anthropic.claude-sonnet-4-5":      ModelPrice(input_per_1k=0.003,    output_per_1k=0.015),
    "anthropic.claude-sonnet-4":        ModelPrice(input_per_1k=0.003,    output_per_1k=0.015),
    "anthropic.claude-haiku-4-5":       ModelPrice(input_per_1k=0.0008,   output_per_1k=0.004),
    # ── Anthropic Claude 3.x ─────────────────────────────────────────────────
    "anthropic.claude-3-7-sonnet":      ModelPrice(input_per_1k=0.003,    output_per_1k=0.015),
    "anthropic.claude-3-5-sonnet":      ModelPrice(input_per_1k=0.003,    output_per_1k=0.015),
    "anthropic.claude-3-5-haiku":       ModelPrice(input_per_1k=0.0008,   output_per_1k=0.004),
    "anthropic.claude-3-opus":          ModelPrice(input_per_1k=0.015,    output_per_1k=0.075),
    "anthropic.claude-3-sonnet":        ModelPrice(input_per_1k=0.003,    output_per_1k=0.015),
    # ── Amazon Nova 2 ────────────────────────────────────────────────────────
    "amazon.nova-2-lite":               ModelPrice(input_per_1k=0.00006,  output_per_1k=0.00024),
    "amazon.nova-2-micro":              ModelPrice(input_per_1k=0.000035, output_per_1k=0.00014),
    "amazon.nova-2-pro":                ModelPrice(input_per_1k=0.0008,   output_per_1k=0.0032),
    # ── Meta Llama 3.x ───────────────────────────────────────────────────────
    "meta.llama3-2-1b-instruct":        ModelPrice(input_per_1k=0.0001,   output_per_1k=0.0001),
    "meta.llama3-2-3b-instruct":        ModelPrice(input_per_1k=0.00015,  output_per_1k=0.00015),
    "meta.llama3-2-11b-instruct":       ModelPrice(input_per_1k=0.00016,  output_per_1k=0.00016),
    "meta.llama3-2-90b-instruct":       ModelPrice(input_per_1k=0.00072,  output_per_1k=0.00072),
    "meta.llama3-3-70b-instruct":       ModelPrice(input_per_1k=0.00072,  output_per_1k=0.00072),
    # ── Mistral ───────────────────────────────────────────────────────────────
    "mistral.mistral-large":            ModelPrice(input_per_1k=0.002,    output_per_1k=0.006),
    "mistral.mistral-large-3":          ModelPrice(input_per_1k=0.002,    output_per_1k=0.006),
    "mistral.pixtral-large-2502":       ModelPrice(input_per_1k=0.002,    output_per_1k=0.006),
    "mistral.devstral-2-123b":          ModelPrice(input_per_1k=0.002,    output_per_1k=0.006),
}


def _extract_price_per_1k(item: dict) -> float | None:
    """Extract price per 1K tokens from a PriceList item (unit is already '1K tokens')."""
    try:
        terms = item.get("terms", {}).get("OnDemand", {})
        for term in terms.values():
            for dimension in term.get("priceDimensions", {}).values():
                usd_str = dimension.get("pricePerUnit", {}).get("USD", "")
                if usd_str:
                    return float(usd_str)
    except Exception:
        pass
    return None
