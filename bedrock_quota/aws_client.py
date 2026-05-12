"""AWS client factory using boto3."""

import os
from typing import Any

import boto3


class AWSClient:
    """AWS client factory using boto3, compatible with LocalStack."""

    def __init__(self, region: str | None = None):
        self.aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
        self.aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if region:
            self.aws_region = region
        elif explicit := os.environ.get("AWS_DEFAULT_REGION"):
            self.aws_region = explicit
        else:
            session = boto3.session.Session()
            self.aws_region = session.region_name or "us-east-1"
        self._cloudwatch = None
        self._service_quotas = None
        self._pricing = None

    def get_client(self, service: str, **kwargs: Any) -> Any:
        """Create boto3 client with proper credential handling."""
        # Support AWS_ENDPOINT_URL_{SERVICE} convention for LocalStack
        env_key = f"AWS_ENDPOINT_URL_{service.replace('-', '_').upper()}"
        endpoint_url = (
            kwargs.get("endpoint_url")
            or os.environ.get(env_key)
            or os.environ.get("AWS_ENDPOINT_URL")
        )

        client_kwargs = {"region_name": self.aws_region, **kwargs}

        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            # For LocalStack, provide dummy credentials
            client_kwargs.setdefault("aws_access_key_id", self.aws_access_key_id or "local")
            client_kwargs.setdefault(
                "aws_secret_access_key", self.aws_secret_access_key or "local"
            )

        return boto3.client(service, **client_kwargs)

    @property
    def service_quotas(self):
        if self._service_quotas is None:
            self._service_quotas = self.get_client("service-quotas")
        return self._service_quotas

    @property
    def cloudwatch(self):
        if self._cloudwatch is None:
            self._cloudwatch = self.get_client("cloudwatch")
        return self._cloudwatch

    @property
    def pricing(self):
        if self._pricing is None:
            self._pricing = self.get_client("pricing", region_name="us-east-1")
        return self._pricing
