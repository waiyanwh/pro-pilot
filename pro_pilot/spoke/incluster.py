"""ServiceAccount execution with optional pinned EKS Pod Identity credentials."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from kubernetes import client, config

from .local import _ROLE_PATTERN, IdentityRejected, LocalExecutor, verify_identity
from .policy import NamespacePolicy
from .tools.common import KubeReader

_LOCAL_CREDENTIAL_ENV = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
)


def verify_service_account(configuration: Any, settings: dict[str, Any]) -> None:
    namespace = settings.get("namespace")
    name = settings.get("service_account_name")
    if not isinstance(namespace, str) or not namespace or not isinstance(name, str) or not name:
        raise IdentityRejected("in-cluster mode requires an explicit ServiceAccount and namespace")
    try:
        authorization = configuration.get_api_key_with_prefix("BearerToken")
        scheme, token = authorization.split(None, 1)
        if scheme.lower() != "bearer":
            raise ValueError("not a ServiceAccount bearer")
        pieces = token.split(".")
        if len(pieces) != 3:
            raise ValueError("not a ServiceAccount JWT")
        claims = json.loads(base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4)))
        if claims.get("sub") != f"system:serviceaccount:{namespace}:{name}":
            raise IdentityRejected("projected token does not match the configured ServiceAccount")
    except (AttributeError, TypeError, ValueError) as exc:
        raise IdentityRejected("unable to identify the projected ServiceAccount token") from exc
    # This checks the selected subject, not the token signature. The cluster API
    # authenticates the JWT on the first read; its RBAC remains the access boundary.


def build_executor(settings: dict[str, Any], policy: NamespacePolicy) -> LocalExecutor:
    kubernetes = settings.get("kubernetes", {})
    aws = settings.get("aws", {})
    limits = settings.get("limits", {})
    if kubernetes.get("kubeconfig") or kubernetes.get("context") or aws.get("profile"):
        raise IdentityRejected("in-cluster mode cannot use a laptop kubeconfig, context or profile")
    if os.environ.get("KUBECONFIG"):
        raise IdentityRejected("in-cluster mode cannot use a KUBECONFIG override")

    enabled = bool(aws.get("enabled", False))
    timeout = min(float(limits.get("tool_timeout_s", 15)), 15)
    cluster_name = str(aws.get("eks_cluster_name", ""))
    pattern = str(aws.get("expected_role_arn_pattern", ""))
    session: Any = None
    sts: Any = None
    eks: Any = None
    described: dict[str, Any] | None = None
    sdk_config = Config(
        connect_timeout=min(3, timeout), read_timeout=timeout, retries={"total_max_attempts": 1}
    )
    if enabled:
        if (
            not cluster_name
            or not isinstance(aws.get("region"), str)
            or not aws["region"]
            or not _ROLE_PATTERN.fullmatch(pattern)
        ):
            raise IdentityRejected("in-cluster AWS tools require region, EKS name and pinned role")
        if any(os.environ.get(name) for name in _LOCAL_CREDENTIAL_ENV):
            raise IdentityRejected("in-cluster AWS tools cannot use laptop or static credentials")
        session = boto3.Session(region_name=aws["region"])
        credentials = session.get_credentials()
        if credentials is None or credentials.method != "container-role":
            raise IdentityRejected("in-cluster AWS tools require EKS Pod Identity credentials")
        sts = session.client("sts", config=sdk_config)
        verify_identity(sts, pattern)
        eks = session.client("eks", config=sdk_config)
        try:
            described = eks.describe_cluster(name=cluster_name)["cluster"]
        except Exception as exc:
            raise IdentityRejected(
                "unable to bind Pod Identity to the configured EKS cluster"
            ) from exc

    configuration: Any = client.Configuration()
    config.load_incluster_config(client_configuration=configuration)
    if not configuration.verify_ssl or not configuration.ssl_ca_cert:
        raise IdentityRejected("in-cluster Kubernetes access requires verified ServiceAccount TLS")
    if not configuration.host.startswith("https://"):
        raise IdentityRejected("in-cluster Kubernetes access requires HTTPS")
    verify_service_account(configuration, kubernetes)
    if described is not None:
        try:
            expected_ca = base64.b64decode(described["certificateAuthority"]["data"], validate=True)
            mounted_ca = Path(configuration.ssl_ca_cert).read_bytes()
            endpoint = described["endpoint"]
            identity_account = _ROLE_PATTERN.fullmatch(pattern)
            expected_arn = (
                f"arn:{pattern.split(':')[1]}:eks:{aws['region']}:"
                f"{identity_account.group(1) if identity_account else ''}:cluster/{cluster_name}"
            )
            if (
                expected_ca.strip() != mounted_ca.strip()
                or not isinstance(endpoint, str)
                or not endpoint.startswith("https://")
                or described.get("arn") != expected_arn
            ):
                raise IdentityRejected(
                    "ServiceAccount CA or EKS identity does not match the cluster"
                )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise IdentityRejected("unable to verify the ServiceAccount cluster identity") from exc
        # EKS's internal service IP differs from DescribeCluster's endpoint. Pin the
        # API host after verifying the mounted cluster CA, retaining token refresh.
        configuration.host = endpoint
    configuration.retries = 0
    reader = KubeReader(
        client.ApiClient(configuration),
        max_items=int(limits.get("max_list_items", 200)),
        timeout_s=timeout,
    )
    return LocalExecutor(
        reader,
        policy,
        sts=sts,
        eks=eks,
        elbv2=session.client("elbv2", config=sdk_config) if session is not None else None,
        cloudwatch=session.client("cloudwatch", config=sdk_config) if session is not None else None,
        cluster_name=cluster_name,
        expected_role_arn_pattern=pattern,
        aws_enabled=enabled,
        max_concurrent=int(limits.get("max_concurrent_tools", 4)),
        max_log_lines=int(limits.get("max_log_lines", 200)),
        max_log_since_s=int(limits.get("max_log_since_s", 3600)),
        argocd_namespace=str(kubernetes.get("argocd_namespace", "argocd")),
    )
