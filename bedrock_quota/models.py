"""Data models for Bedrock quota and usage metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cloudwatch_service import CloudWatchService


class Scope(str, Enum):
    """Service quota scope."""

    GLOBAL_CROSS_REGION = "global-cross-region"
    CROSS_REGION = "cross-region"
    ON_DEMAND = "on-demand"


class TimePeriod(str, Enum):
    """Time period for metrics aggregation."""

    HOURS_24 = "24h"
    TODAY = "today"
    DAYS_7 = "7d"
    DAYS_14 = "14d"
    DAYS_30 = "30d"
    CURRENT_MONTH = "current-month"
    LAST_MONTH = "last-month"


@dataclass
class QuotaLimits:
    """Service quota limits from AWS Service Quotas API."""

    rpm: float  # Requests per minute
    tpm: float  # Tokens per minute
    tpd: float  # Tokens per day


@dataclass
class UsageMetrics:
    """CloudWatch usage metrics for a time period."""

    current_tpd: float = 0.0

    tpd_p50: float = 0.0
    tpd_p90: float = 0.0
    tpd_avg: float = 0.0
    tpd_max: float = 0.0

    rpm_p50: float = 0.0
    rpm_p90: float = 0.0
    rpm_avg: float = 0.0
    rpm_max: float = 0.0

    tpm_p50: float = 0.0
    tpm_p90: float = 0.0
    tpm_avg: float = 0.0
    tpm_max: float = 0.0

    latency_p50: float = 0.0
    latency_p90: float = 0.0
    latency_avg: float = 0.0
    latency_max: float = 0.0

    input_tokens_total: float = 0.0
    output_tokens_total: float = 0.0
    input_tpm_avg: float = 0.0
    output_tpm_avg: float = 0.0

    throttles_total: float = 0.0
    client_errors_total: float = 0.0
    server_errors_total: float = 0.0

    tpm_series: list = field(default_factory=list)   # [(datetime, tpm_value), ...]
    input_series: list = field(default_factory=list)  # [(datetime, tpm_input), ...]
    output_series: list = field(default_factory=list) # [(datetime, tpm_output), ...]
    rpm_series: list = field(default_factory=list)    # [(datetime, rpm_value), ...]
    throttle_series: list = field(default_factory=list)  # [(datetime, count), ...]
    chart_period: int = 600  # seconds; matches the CloudWatch Period used for tpm_series


@dataclass
class ProfileContribution:
    """Per-inference-profile usage breakdown."""

    profile_id: str
    profile_arn: str | None
    tags: dict[str, str]
    tpm_avg: float
    tpm_p90: float
    tpd_total: float


@dataclass
class ModelMetrics:
    """Combined quota limits and usage metrics for a model."""

    model_name: str
    model_id: str
    provider: str
    scope: Scope
    is_legacy: bool
    limits: QuotaLimits
    region: str = "global"
    real_model_ids: list[str] | None = None
    recent_tpd: tuple[float, str] | None = None
    recent_tpd_fetched: bool = False
    p90_tpm: float = 0.0
    usage: UsageMetrics | None = None
    profile_contributions: list = field(default_factory=list)  # list[ProfileContribution]
    cloudwatch_service: CloudWatchService | None = field(default=None, repr=False, compare=False)


@dataclass
class TimeRange:
    """Time range for CloudWatch queries."""

    start: datetime
    end: datetime
    label: str
