"""Quota service for fetching and parsing AWS Bedrock service quotas."""

import re
from collections import defaultdict

from .aws_client import AWSClient
from .models import QuotaLimits, Scope


def is_legacy_from_lifecycle(status: str) -> bool:
    """Return True if the model lifecycle status indicates a legacy/deprecated model."""
    return status not in ("ACTIVE",)


def _match_quota_to_foundation_model(quota_name: str, foundation_models: list[dict]) -> dict | None:
    """
    Find the foundation model best matching a quota name using token overlap.
    Extracts the model name substring after 'for ' in the quota name, then scores
    each foundation model by fraction of candidate tokens matched against modelName.
    Returns the best match if score >= 0.6, else None.
    """
    m = re.search(r"\bfor\s+(.+?)(?:\s*\(|$)", quota_name, re.IGNORECASE)
    if not m:
        return None
    candidate = m.group(1).strip().lower()
    candidate_tokens = set(re.split(r"[\s\-_./]+", candidate))
    candidate_tokens.discard("")

    if not candidate_tokens:
        return None

    best: dict | None = None
    best_score = 0.0

    for fm in foundation_models:
        model_name = fm.get("modelName", "")
        if not model_name:
            continue
        name_tokens = set(re.split(r"[\s\-_./]+", model_name.lower()))
        name_tokens.discard("")
        if not name_tokens:
            continue

        overlap = len(candidate_tokens & name_tokens)
        score = overlap / len(candidate_tokens)
        if score > best_score and score >= 0.6:
            best, best_score = fm, score

    return best


def parse_quota(
    quota: dict, foundation_models: list[dict]
) -> tuple[str | None, str | None, str | None, str | None, str | None, float]:
    """
    Parse a service quota entry into (provider_id, model_id, display_name, quota_type, scope, value).
    Returns all-None (except value) if the quota should be skipped.
    """
    name = quota["QuotaName"]
    value = quota["Value"]
    name_lower = name.lower()

    # Skip non-inference quotas
    skip_keywords = [
        "batch", "provisioned", "model units", "job",
        "records per", "input file", "minimum number", "sum of",
    ]
    if any(kw in name_lower for kw in skip_keywords):
        return None, None, None, None, None, value

    # Determine scope from quota name (AWS-defined strings)
    if "global cross-region" in name_lower:
        scope = "global-cross-region"
    elif "cross-region" in name_lower or "doubled for cross-region" in name_lower:
        scope = "cross-region"
    elif "on-demand" in name_lower:
        scope = "on-demand"
    else:
        return None, None, None, None, None, value

    # Determine quota type (AWS-defined strings)
    if "tokens per day" in name_lower or "max tokens per day" in name_lower:
        quota_type = "TPD"
    elif "tokens per minute" in name_lower:
        quota_type = "TPM"
    elif "requests per minute" in name_lower:
        quota_type = "RPM"
    else:
        return None, None, None, None, None, value

    # Match quota to a foundation model via token overlap on modelName
    fm = _match_quota_to_foundation_model(name, foundation_models)
    if fm is None:
        return None, None, None, None, None, value

    model_id = fm.get("modelId", "")
    if not model_id:
        return None, None, None, None, None, value

    provider_id = model_id.split(".")[0]
    display_name = fm.get("modelName", model_id)

    return provider_id, model_id, display_name, quota_type, scope, value


def format_number(value: float) -> str:
    """Format large numbers with K/M/B suffixes."""
    if value == 0:
        return "-"
    elif value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    elif value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    else:
        return f"{value:.0f}"


class QuotaService:
    """Fetch and parse AWS Bedrock service quotas."""

    def __init__(self, client: AWSClient):
        self.client = client
        self._foundation_models: list[dict] | None = None

    def fetch_foundation_models(self) -> list[dict]:
        """Return modelSummaries from bedrock.list_foundation_models(), cached per instance."""
        if self._foundation_models is not None:
            return self._foundation_models
        bedrock = self.client.get_client("bedrock")
        resp = bedrock.list_foundation_models()
        fms: list[dict] = resp.get("modelSummaries", []) or []
        self._foundation_models = fms
        return fms

    def fetch_quotas(self) -> dict[tuple[str, str], tuple[str, bool, dict[Scope, QuotaLimits]]]:
        """
        Fetch all Bedrock quotas grouped by (provider_id, model_id).

        Returns:
            dict mapping (provider_id, model_id) ->
                (display_name, is_legacy, {scope: QuotaLimits})
        """
        fms = self.fetch_foundation_models()

        # Build lifecycle status lookup: modelId -> status string
        lifecycle_by_id: dict[str, str] = {
            fm.get("modelId", ""): fm.get("modelLifecycle", {}).get("status", "ACTIVE")
            for fm in fms
        }

        # Build provider display name lookup: provider_id -> providerName
        # (used externally by app to populate _provider_display)
        self._provider_display: dict[str, str] = {}
        for fm in fms:
            mid = fm.get("modelId", "")
            if mid and "." in mid:
                pid = mid.split(".")[0]
                pname = fm.get("providerName", "")
                if pname and pid not in self._provider_display:
                    self._provider_display[pid] = pname

        svc = self.client.service_quotas
        paginator = svc.get_paginator("list_service_quotas")
        page_iterator = paginator.paginate(ServiceCode="bedrock")

        # Accumulate: (provider_id, model_id) -> (display_name, is_legacy, {scope: limits})
        results: dict[tuple[str, str], tuple[str, bool, dict[Scope, QuotaLimits]]] = {}

        for page in page_iterator:
            for quota in page["Quotas"]:
                provider_id, model_id, display_name, quota_type, scope_str, value = parse_quota(quota, fms)

                if not provider_id or not model_id or not display_name or not quota_type or not scope_str:
                    continue

                scope = Scope(scope_str)
                key = (provider_id, model_id)

                if key not in results:
                    lifecycle_status = lifecycle_by_id.get(model_id, "ACTIVE")
                    results[key] = (
                        display_name,
                        is_legacy_from_lifecycle(lifecycle_status),
                        defaultdict(lambda: QuotaLimits(rpm=0, tpm=0, tpd=0)),
                    )

                _, _, scopes = results[key]
                limits = scopes[scope]

                if quota_type == "RPM":
                    limits.rpm = value
                elif quota_type == "TPM":
                    limits.tpm = value
                elif quota_type == "TPD":
                    limits.tpd = value

        # Convert inner defaultdicts to plain dicts
        return {
            k: (dn, il, dict(scopes))
            for k, (dn, il, scopes) in results.items()
        }
