"""CloudWatch service for fetching AWS Bedrock usage metrics."""

import re
import time
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from .aws_client import AWSClient
from .models import ProfileContribution, TimeRange, UsageMetrics


def _percentile(vals: list[float], p: int) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[idx]


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


class CloudWatchService:
    """Fetch AWS CloudWatch metrics for Bedrock usage."""

    NAMESPACE = "AWS/Bedrock"
    _CACHE_TTL = 300  # 5 minutes

    _SCOPE_PREFIXES = ("global.", "us.", "eu.", "ap.", "sa.", "ca.", "me.", "af.")

    def __init__(self, client: AWSClient):
        self.client = client
        self._discovered_ids: list[str] | None = None
        self._tpd_cache: dict = {}
        self._usage_cache: dict = {}
        self._p90_tpm_cache: dict = {}
        self._contributions_cache: dict = {}
        self._app_profile_arns: dict[str, str] = {}  # profile_id -> ARN

    def _cache_bucket(self) -> int:
        return int(time.time() // self._CACHE_TTL)

    def discover_model_ids(self) -> list[str]:
        """
        Return all known model IDs from three authoritative sources:

        1. bedrock.list_foundation_models() — bare on-demand modelIds
        2. bedrock.list_inference_profiles(type=SYSTEM_DEFINED) — cross-region profile IDs
        3. cloudwatch.list_metrics() — global.* and any other observed dimensions
        """
        if self._discovered_ids is not None:
            return self._discovered_ids

        ids: set[str] = set()

        # Source 1: ON_DEMAND — bare modelIds from list_foundation_models
        try:
            bedrock = self.client.get_client("bedrock")
            resp = bedrock.list_foundation_models()
            for m in resp.get("modelSummaries", []):
                mid = m.get("modelId", "")
                if mid:
                    ids.add(mid)
        except Exception:
            pass

        # Source 2: CROSS_REGION — system-defined inference profiles (region-specific)
        try:
            bedrock = self.client.get_client("bedrock")
            try:
                paginator = bedrock.get_paginator("list_inference_profiles")
                for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
                    for profile in page.get("inferenceProfileSummaries", []):
                        pid = profile.get("inferenceProfileId", "")
                        if pid:
                            ids.add(pid)
            except Exception:
                resp = bedrock.list_inference_profiles(typeEquals="SYSTEM_DEFINED")
                for profile in resp.get("inferenceProfileSummaries", []):
                    pid = profile.get("inferenceProfileId", "")
                    if pid:
                        ids.add(pid)
        except Exception:
            pass

        # Source 2b: APPLICATION profiles — customer-created inference profiles
        try:
            bedrock = self.client.get_client("bedrock")
            resp = bedrock.list_inference_profiles(typeEquals="APPLICATION", maxResults=100)
            for profile in resp.get("inferenceProfileSummaries", []):
                pid = profile.get("inferenceProfileId", "")
                arn = profile.get("inferenceProfileArn", "")
                if pid:
                    ids.add(pid)
                    if arn:
                        self._app_profile_arns[pid] = arn
        except Exception:
            pass

        # Source 3: GLOBAL_CROSS_REGION — observed CloudWatch dimensions
        try:
            cw = self.client.cloudwatch
            paginator = cw.get_paginator("list_metrics")
            for page in paginator.paginate(Namespace=self.NAMESPACE):
                for metric in page["Metrics"]:
                    for dim in metric.get("Dimensions", []):
                        if dim["Name"] == "ModelId":
                            ids.add(dim["Value"])
        except Exception:
            pass

        self._discovered_ids = list(ids)
        return self._discovered_ids

    def filter_ids_by_scope(self, model_ids: list[str], scope: "Scope") -> list[str]:
        """Filter a list of CloudWatch model IDs to those matching a quota scope."""
        from .models import Scope

        cross_region_prefixes = tuple(p for p in self._SCOPE_PREFIXES if p != "global.")

        if scope == Scope.GLOBAL_CROSS_REGION:
            return [mid for mid in model_ids if mid.startswith("global.")]
        elif scope == Scope.CROSS_REGION:
            return [mid for mid in model_ids if any(mid.startswith(p) for p in cross_region_prefixes)]
        else:
            return [
                mid for mid in model_ids
                if not any(mid.startswith(p) for p in self._SCOPE_PREFIXES)
            ]

    @classmethod
    def _to_slug(cls, model_id: str) -> str:
        """
        Strip scope prefix, date stamp, and version suffix to get a bare model slug.

        e.g. "eu.anthropic.claude-sonnet-4-6-20250929-v1:0" -> "anthropic.claude-sonnet-4-6"
        """
        s = model_id.lower()
        for p in cls._SCOPE_PREFIXES:
            if s.startswith(p):
                s = s[len(p):]
                break
        s = re.sub(r"-\d{8}", "", s)
        s = re.sub(r"-?v[\d:\.]+$", "", s)
        s = re.sub(r":[\d]+$", "", s)
        return s.strip("-")

    def find_matching_model_ids(self, model_id: str) -> list[str]:
        """Find all discovered IDs whose slug matches the slug of the given model_id."""
        all_ids = self.discover_model_ids()
        slug = self._to_slug(model_id)
        if not slug:
            return []
        return [real_id for real_id in all_ids if self._to_slug(real_id) == slug]

    def get_recent_tpd(
        self, model_id: str, real_ids: list[str] | tuple[str, ...] | None = None
    ) -> tuple[float, str] | None:
        """Fetch the most recent day's token count for a model."""
        if real_ids is None:
            real_ids = self.find_matching_model_ids(model_id)
        if not real_ids:
            return None

        cache_key = (model_id, tuple(real_ids), self._cache_bucket())
        if cache_key in self._tpd_cache:
            return self._tpd_cache[cache_key]

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        cw = self.client.cloudwatch

        by_day: dict = {}
        try:
            for rid in real_ids:
                for ts, val in self._fetch_token_by_timestamp(cw, rid, start, now, 86400):
                    ts_aware = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
                    by_day[ts_aware] = by_day.get(ts_aware, 0.0) + val
        except ClientError:
            return None

        if not by_day:
            self._tpd_cache[cache_key] = None
            return None

        latest_ts = max(by_day.keys())
        total = by_day[latest_ts]
        label = latest_ts.strftime("%b %-d")
        result = (total, label)
        self._tpd_cache[cache_key] = result
        return result

    def get_p90_tpm_7d(
        self, model_id: str, real_ids: list[str] | tuple[str, ...] | None = None
    ) -> float:
        """Return P90 tokens-per-minute over the last 7 days. Used for quota warning."""
        if real_ids is None:
            real_ids = self.find_matching_model_ids(model_id)
        if not real_ids:
            return 0.0

        cache_key = ("p90tpm", model_id, tuple(real_ids), self._cache_bucket())
        if cache_key in self._p90_tpm_cache:
            return self._p90_tpm_cache[cache_key]

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        cw = self.client.cloudwatch

        min_tokens: dict = {}
        try:
            for rid in real_ids:
                for ts, val in self._fetch_token_by_timestamp(cw, rid, start, now, 60):
                    min_tokens[ts] = min_tokens.get(ts, 0.0) + val
        except ClientError:
            self._p90_tpm_cache[cache_key] = 0.0
            return 0.0

        tpm_vals = [v / 60.0 for v in min_tokens.values()]
        result = _percentile(tpm_vals, 90)
        self._p90_tpm_cache[cache_key] = result
        return result

    def get_usage_metrics(
        self, model_id: str, time_range: TimeRange, today_range: TimeRange
    ) -> UsageMetrics | None:
        """Fetch full usage metrics for a model across all matching IDs."""
        cache_key = (model_id, time_range.label, self._cache_bucket())
        if cache_key in self._usage_cache:
            return self._usage_cache[cache_key]

        real_ids = self.find_matching_model_ids(model_id)
        if not real_ids:
            return None

        cw = self.client.cloudwatch

        # 1-minute granularity: TPM and RPM
        min_tokens: dict = {}
        min_invocations: dict = {}

        # 1-hour granularity: rolling TPD, latency, errors/throttles
        hourly_tokens: dict = {}
        hourly_latency_sum: dict = {}
        hourly_latency_cnt: dict = {}
        hourly_throttles: dict = {}
        hourly_client_errors: dict = {}
        hourly_server_errors: dict = {}

        # Today's TPD
        current_tpd_total = 0.0

        try:
            for rid in real_ids:
                # Today
                for dp in self._fetch_token_datapoints(cw, rid, today_range.start, today_range.end, 86400):
                    current_tpd_total += dp

                # 1-min tokens + invocations for TPM/RPM
                for ts, v in self._fetch_token_by_timestamp(cw, rid, time_range.start, time_range.end, 60):
                    min_tokens[ts] = min_tokens.get(ts, 0.0) + v
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "Invocations", time_range.start, time_range.end, 60):
                    min_invocations[ts] = min_invocations.get(ts, 0.0) + v

                # 1-hour tokens for rolling TPD
                for ts, v in self._fetch_token_by_timestamp(cw, rid, time_range.start, time_range.end, 3600):
                    hourly_tokens[ts] = hourly_tokens.get(ts, 0.0) + v

                # 1-hour latency (Average stat) — track sum+count for weighted mean across real_ids
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "InvocationLatency", time_range.start, time_range.end, 3600, stat="Average"):
                    hourly_latency_sum[ts] = hourly_latency_sum.get(ts, 0.0) + v
                    hourly_latency_cnt[ts] = hourly_latency_cnt.get(ts, 0) + 1

                # 1-hour errors/throttles
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "InvocationThrottles", time_range.start, time_range.end, 3600):
                    hourly_throttles[ts] = hourly_throttles.get(ts, 0.0) + v
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "InvocationClientErrors", time_range.start, time_range.end, 3600):
                    hourly_client_errors[ts] = hourly_client_errors.get(ts, 0.0) + v
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "InvocationServerErrors", time_range.start, time_range.end, 3600):
                    hourly_server_errors[ts] = hourly_server_errors.get(ts, 0.0) + v

        except ClientError:
            return None

        tpd_vals = self._rolling_tpd(hourly_tokens)

        # Fallback for short windows (24h, Today): rolling windows may be empty
        if not tpd_vals and current_tpd_total > 0:
            tpd_vals = [current_tpd_total]

        # Only bail if truly no CloudWatch data at all
        if not tpd_vals and not min_tokens and not min_invocations:
            self._usage_cache[cache_key] = None
            return None

        hourly_latency = {
            ts: hourly_latency_sum[ts] / hourly_latency_cnt[ts]
            for ts in hourly_latency_sum
        }

        tpm_vals = [v / 60.0 for v in min_tokens.values()]
        rpm_vals = [v / 60.0 for v in min_invocations.values()]
        latency_vals = list(hourly_latency.values())

        # Chart series: pick the finest period CloudWatch will actually return for this window.
        # Retention tiers: <3h → 60s, <15d → 300s, <63d → 3600s.
        # We request one tier coarser than the minimum to avoid silent downsampling.
        window_days = (time_range.end - time_range.start).total_seconds() / 86400
        if window_days <= 1:
            chart_period = 60    # 1-min  (data <3h old is always 1-min)
        elif window_days <= 14:
            chart_period = 300   # 5-min  (data <15 days old)
        else:
            chart_period = 3600  # 1-hour (data 15-63 days old)

        chart_input: dict = {}
        chart_output: dict = {}
        try:
            for rid in real_ids:
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "InputTokenCount", time_range.start, time_range.end, chart_period):
                    chart_input[ts] = chart_input.get(ts, 0.0) + v
                for ts, v in self._fetch_metric_by_timestamp(cw, rid, "OutputTokenCount", time_range.start, time_range.end, chart_period):
                    chart_output[ts] = chart_output.get(ts, 0.0) + v
        except ClientError:
            pass

        def _to_series(d: dict, divisor: float) -> list:
            return sorted(
                [(ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts, v / divisor)
                 for ts, v in d.items()],
                key=lambda x: x[0],
            )

        # Combined = input + output merged by timestamp
        chart_tokens: dict = {}
        for ts, v in chart_input.items():
            chart_tokens[ts] = chart_tokens.get(ts, 0.0) + v
        for ts, v in chart_output.items():
            chart_tokens[ts] = chart_tokens.get(ts, 0.0) + v

        result = UsageMetrics(
            current_tpd=current_tpd_total,
            tpd_p50=_percentile(tpd_vals, 50),
            tpd_p90=_percentile(tpd_vals, 90),
            tpd_avg=_mean(tpd_vals),
            tpd_max=max(tpd_vals, default=0.0),
            tpm_p50=_percentile(tpm_vals, 50),
            tpm_p90=_percentile(tpm_vals, 90),
            tpm_avg=_mean(tpm_vals),
            tpm_max=max(tpm_vals, default=0.0),
            rpm_p50=_percentile(rpm_vals, 50),
            rpm_p90=_percentile(rpm_vals, 90),
            rpm_avg=_mean(rpm_vals),
            rpm_max=max(rpm_vals, default=0.0),
            latency_p50=_percentile(latency_vals, 50),
            latency_p90=_percentile(latency_vals, 90),
            latency_avg=_mean(latency_vals),
            latency_max=max(latency_vals, default=0.0),
            input_tokens_total=sum(chart_input.values()),
            output_tokens_total=sum(chart_output.values()),
            input_tpm_avg=_mean([v / chart_period for v in chart_input.values()]),
            output_tpm_avg=_mean([v / chart_period for v in chart_output.values()]),
            throttles_total=sum(hourly_throttles.values()),
            client_errors_total=sum(hourly_client_errors.values()),
            server_errors_total=sum(hourly_server_errors.values()),
            tpm_series=_to_series(chart_tokens, chart_period),
            input_series=_to_series(chart_input, chart_period),
            output_series=_to_series(chart_output, chart_period),
            rpm_series=_to_series(min_invocations, 60.0),
            throttle_series=_to_series(hourly_throttles, 1.0),
            chart_period=chart_period,
        )
        self._usage_cache[cache_key] = result
        return result

    def get_profile_tags(self, profile_id: str) -> dict[str, str]:
        """Fetch tags for an APPLICATION inference profile by its ID."""
        arn = self._app_profile_arns.get(profile_id)
        if not arn:
            return {}
        try:
            bedrock = self.client.get_client("bedrock")
            resp = bedrock.list_tags_for_resource(resourceARN=arn)
            return {t["key"]: t["value"] for t in resp.get("tags", [])}
        except Exception:
            return {}

    def get_profile_contributions(
        self, model_id: str, time_range: TimeRange
    ) -> list[ProfileContribution]:
        """Return per-real_id usage contributions sorted by avg TPM descending."""
        cache_key = ("contributions", model_id, time_range.label, self._cache_bucket())
        if cache_key in self._contributions_cache:
            return self._contributions_cache[cache_key]

        real_ids = self.find_matching_model_ids(model_id)
        if not real_ids:
            return []

        cw = self.client.cloudwatch
        contribs = []

        for rid in real_ids:
            min_tokens: dict = {}
            hourly_tokens: dict = {}
            try:
                for ts, v in self._fetch_token_by_timestamp(cw, rid, time_range.start, time_range.end, 60):
                    min_tokens[ts] = min_tokens.get(ts, 0.0) + v
                for ts, v in self._fetch_token_by_timestamp(cw, rid, time_range.start, time_range.end, 3600):
                    hourly_tokens[ts] = hourly_tokens.get(ts, 0.0) + v
            except ClientError:
                continue

            tpm_vals = [v / 60.0 for v in min_tokens.values()]
            tpd_vals = self._rolling_tpd(hourly_tokens)

            contribs.append(ProfileContribution(
                profile_id=rid,
                profile_arn=self._app_profile_arns.get(rid),
                tags=self.get_profile_tags(rid),
                tpm_avg=_mean(tpm_vals),
                tpm_p90=_percentile(tpm_vals, 90),
                tpd_total=sum(tpd_vals),
            ))

        contribs.sort(key=lambda c: c.tpm_avg, reverse=True)
        self._contributions_cache[cache_key] = contribs
        return contribs

    def _rolling_tpd(self, hourly: dict) -> list[float]:
        """Build daily token totals using 24-hour rolling windows."""
        if not hourly:
            return []
        end = max(hourly)
        end_aware = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        vals = []
        for d in range(30):
            w_end = end_aware - timedelta(days=d)
            w_start = w_end - timedelta(days=1)
            total = sum(
                v for t, v in hourly.items()
                if w_start <= (t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t) < w_end
            )
            if total > 0:
                vals.append(total)
        return vals

    def _fetch_metric_data(
        self,
        cw,
        queries: list[dict],
        start,
        end,
    ) -> dict[str, dict]:
        """
        Call GetMetricData for one or more metric queries.

        Returns {query_id -> {timestamp -> value}}.
        GetMetricData supports up to 100,800 datapoints per request (vs 1,440
        for GetMetricStatistics), so 30-day 1-min queries fit in a single call.
        """
        results: dict[str, dict] = {q["Id"]: {} for q in queries}
        try:
            paginator = cw.get_paginator("get_metric_data")
            for page in paginator.paginate(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
            ):
                for r in page.get("MetricDataResults", []):
                    bucket = results[r["Id"]]
                    for ts, val in zip(r.get("Timestamps", []), r.get("Values", [])):
                        bucket[ts] = bucket.get(ts, 0.0) + val
        except Exception:
            pass
        return results

    def _fetch_token_datapoints(self, cw, model_id: str, start, end, period: int) -> list[float]:
        queries = [
            {
                "Id": "inp",
                "MetricStat": {
                    "Metric": {
                        "Namespace": self.NAMESPACE,
                        "MetricName": "InputTokenCount",
                        "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    },
                    "Period": period,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
            {
                "Id": "out",
                "MetricStat": {
                    "Metric": {
                        "Namespace": self.NAMESPACE,
                        "MetricName": "OutputTokenCount",
                        "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    },
                    "Period": period,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
        ]
        raw = self._fetch_metric_data(cw, queries, start, end)
        totals: dict = {}
        for bucket in raw.values():
            for ts, v in bucket.items():
                totals[ts] = totals.get(ts, 0.0) + v
        return list(totals.values())

    def _fetch_token_by_timestamp(self, cw, model_id: str, start, end, period: int) -> list[tuple]:
        queries = [
            {
                "Id": "inp",
                "MetricStat": {
                    "Metric": {
                        "Namespace": self.NAMESPACE,
                        "MetricName": "InputTokenCount",
                        "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    },
                    "Period": period,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
            {
                "Id": "out",
                "MetricStat": {
                    "Metric": {
                        "Namespace": self.NAMESPACE,
                        "MetricName": "OutputTokenCount",
                        "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    },
                    "Period": period,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
        ]
        raw = self._fetch_metric_data(cw, queries, start, end)
        totals: dict = {}
        for bucket in raw.values():
            for ts, v in bucket.items():
                totals[ts] = totals.get(ts, 0.0) + v
        return list(totals.items())

    def _fetch_metric_by_timestamp(
        self, cw, model_id: str, metric: str, start, end, period: int, stat: str = "Sum"
    ) -> list[tuple]:
        queries = [
            {
                "Id": "m",
                "MetricStat": {
                    "Metric": {
                        "Namespace": self.NAMESPACE,
                        "MetricName": metric,
                        "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    },
                    "Period": period,
                    "Stat": stat,
                },
                "ReturnData": True,
            }
        ]
        raw = self._fetch_metric_data(cw, queries, start, end)
        return list(raw["m"].items())
