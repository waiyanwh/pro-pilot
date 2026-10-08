"""Guarded real-cluster execution; no infrastructure access occurs at import time."""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.config import Config
from kubernetes import client
from kubernetes.config.kube_config import KubeConfigLoader

from pro_pilot.protocol.tools import TOOL_REGISTRY

from .execution import DEADLINE, TRUNCATED, ExecutionResult
from .policy import NamespacePolicy
from .tools.common import KubeReader

LOGGER = logging.getLogger(__name__)
_ROLE_PATTERN = re.compile(
    r"^arn:aws(?:-cn|-us-gov)?:sts::([0-9]{12}):assumed-role/([^*?\[\]/]+)/\*$"
)


class IdentityRejected(RuntimeError):
    """The requested read identity cannot be verified; fail before cluster reads."""


def verify_identity(sts: Any, expected_pattern: str) -> dict[str, str]:
    pattern = _ROLE_PATTERN.fullmatch(expected_pattern)
    if pattern is None:
        raise IdentityRejected(
            "expected_role_arn_pattern must pin account/role; only the session suffix may be '*'"
        )
    try:
        identity = sts.get_caller_identity()
    except Exception as exc:
        raise IdentityRejected("unable to verify the configured read-only AWS role") from exc
    if not isinstance(identity, dict):
        raise IdentityRejected("AWS identity response is invalid")
    arn = identity.get("Arn")
    if (
        not isinstance(arn, str)
        or identity.get("Account") != pattern.group(1)
        or not fnmatch.fnmatchcase(arn, expected_pattern)
    ):
        raise IdentityRejected("AWS identity does not match the configured read-only role")
    return {"Account": str(identity["Account"]), "Arn": arn}


def validate_kubeconfig(
    payload: dict[str, Any],
    *,
    context: str,
    profile: str,
    region: str,
    cluster_name: str,
    endpoint: str,
) -> None:
    contexts = {row["name"]: row["context"] for row in payload.get("contexts", [])}
    selected = contexts.get(context)
    if not isinstance(selected, dict):
        raise IdentityRejected("the configured Kubernetes context does not exist")
    users = {row["name"]: row["user"] for row in payload.get("users", [])}
    clusters = {row["name"]: row["cluster"] for row in payload.get("clusters", [])}
    user = users.get(selected.get("user"), {})
    cluster = clusters.get(selected.get("cluster"), {})
    if cluster.get("server", "").rstrip("/") != endpoint.rstrip("/") or cluster.get(
        "insecure-skip-tls-verify"
    ):
        raise IdentityRejected(
            "kubeconfig endpoint or TLS policy does not match the configured EKS cluster"
        )
    if not isinstance(user, dict) or set(user) != {"exec"}:
        raise IdentityRejected(
            "local EKS access requires only the scoped aws exec credential provider"
        )
    execute = user["exec"]
    if Path(str(execute.get("command", ""))).name != "aws":
        raise IdentityRejected("local EKS credentials must use the aws exec provider")
    args = execute.get("args", [])
    if not isinstance(args, list) or not all(isinstance(part, str) for part in args):
        raise IdentityRejected("invalid EKS exec arguments")
    environment = {row["name"]: row.get("value", "") for row in execute.get("env", [])}
    if set(environment) - {"AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"}:
        raise IdentityRejected("EKS exec environment must not override credential sources")
    if (
        args.count("eks") != 1
        or args.count("get-token") != 1
        or args.index("get-token") != args.index("eks") + 1
    ):
        raise IdentityRejected("local EKS credentials must execute eks get-token")
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"eks", "get-token"}:
            index += 1
            continue
        if (
            value not in {"--profile", "--region", "--cluster-name", "--output"}
            or index + 1 >= len(args)
            or value in options
        ):
            raise IdentityRejected("unscoped EKS exec arguments are not allowed")
        options[value] = args[index + 1]
        index += 2
    selected_profile = options.get("--profile", environment.get("AWS_PROFILE"))
    if selected_profile != profile or (
        "AWS_PROFILE" in environment and environment["AWS_PROFILE"] != profile
    ):
        raise IdentityRejected("kubeconfig must authenticate with the same guarded AWS profile")
    selected_region = options.get(
        "--region", environment.get("AWS_REGION", environment.get("AWS_DEFAULT_REGION"))
    )
    if selected_region != region or options.get("--cluster-name") != cluster_name:
        raise IdentityRejected(
            "kubeconfig exec region or cluster does not match the local configuration"
        )
    if options.get("--output", "json") != "json":
        raise IdentityRejected("EKS exec output must be json")
    # An explicit --profile disables inherited environment-key precedence in AWS CLI.
    if "--profile" not in options:
        execute["args"] = [*args, "--profile", profile]


class LocalExecutor:
    def __init__(
        self,
        reader: KubeReader,
        policy: NamespacePolicy,
        *,
        sts: Any,
        eks: Any,
        elbv2: Any,
        cloudwatch: Any,
        cluster_name: str,
        expected_role_arn_pattern: str,
        aws_enabled: bool = True,
        max_concurrent: int = 4,
        max_log_lines: int = 200,
        max_log_since_s: int = 3600,
        argocd_namespace: str = "argocd",
    ) -> None:
        from .tools.argo import ArgoTools
        from .tools.aws import AwsTools
        from .tools.k8s import K8sTools
        from .tools.rollouts import RolloutTools

        self.reader = reader
        self.policy = policy
        self.sts = sts
        self.eks = eks
        self.cluster_name = cluster_name
        self.expected_pattern = expected_role_arn_pattern
        self.aws_enabled = aws_enabled
        self._capabilities: set[str] = set()
        self._workers = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="spoke-read"
        )
        self._slots = asyncio.Semaphore(max_concurrent)
        self._groups = {
            "k8s": K8sTools(
                reader, policy, max_log_lines=max_log_lines, max_log_since_s=max_log_since_s
            ),
            "argocd": ArgoTools(reader, policy, argocd_namespace=argocd_namespace),
            "rollouts": RolloutTools(reader, policy),
            "aws": AwsTools(
                eks,
                elbv2,
                cloudwatch,
                reader,
                policy,
                cluster_name=cluster_name,
                max_items=reader.max_items,
            ),
        }

    @classmethod
    def from_config(cls, settings: dict[str, Any], policy: NamespacePolicy) -> LocalExecutor:
        aws = settings.get("aws", {})
        kubernetes = settings.get("kubernetes", {})
        limits = settings.get("limits", {})
        required = (
            aws.get("profile"),
            aws.get("region"),
            aws.get("eks_cluster_name"),
            aws.get("expected_role_arn_pattern"),
            kubernetes.get("context"),
            kubernetes.get("kubeconfig"),
        )
        if not all(isinstance(value, str) and value for value in required):
            raise IdentityRejected(
                "local mode requires explicit role/profile/region/cluster and kubeconfig/context"
            )
        profile, region, cluster_name, pattern, context, path = required
        if not _ROLE_PATTERN.fullmatch(pattern):
            raise IdentityRejected("local mode requires a pinned read-only account/role pattern")
        timeout = min(float(limits.get("tool_timeout_s", 15)), 15)
        sdk_config = Config(
            connect_timeout=min(3, timeout), read_timeout=timeout, retries={"total_max_attempts": 1}
        )
        session = boto3.Session(profile_name=profile, region_name=region)
        sts = session.client("sts", config=sdk_config)
        verify_identity(sts, pattern)
        eks = session.client("eks", config=sdk_config)
        try:
            described = eks.describe_cluster(name=cluster_name)["cluster"]
        except Exception as exc:
            raise IdentityRejected(
                "unable to bind the read-only profile to the configured EKS cluster"
            ) from exc
        with Path(path).expanduser().open(encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        if not isinstance(payload, dict):
            raise IdentityRejected("kubeconfig must be an object")
        validate_kubeconfig(
            payload,
            context=context,
            profile=profile,
            region=region,
            cluster_name=cluster_name,
            endpoint=described["endpoint"],
        )
        configuration: Any = client.Configuration()
        loader = KubeConfigLoader(
            payload, active_context=context, config_base_path=str(Path(path).expanduser().parent)
        )
        loader.load_and_set(configuration)
        configuration.retries = 0
        reader = KubeReader(
            client.ApiClient(configuration),
            max_items=int(limits.get("max_list_items", 200)),
            timeout_s=timeout,
        )
        return cls(
            reader,
            policy,
            sts=sts,
            eks=eks,
            elbv2=session.client("elbv2", config=sdk_config),
            cloudwatch=session.client("cloudwatch", config=sdk_config),
            cluster_name=cluster_name,
            expected_role_arn_pattern=pattern,
            aws_enabled=bool(aws.get("enabled", True)),
            max_concurrent=int(limits.get("max_concurrent_tools", 4)),
            max_log_lines=int(limits.get("max_log_lines", 200)),
            max_log_since_s=int(limits.get("max_log_since_s", 3600)),
            argocd_namespace=str(kubernetes.get("argocd_namespace", "argocd")),
        )

    def detect_capabilities(self) -> list[str]:
        if self.sts is not None:
            verify_identity(self.sts, self.expected_pattern)
        elif self.aws_enabled:
            raise IdentityRejected("AWS tools require a verified read-only identity")
        self.reader.list("v1", "namespaces", params={"limit": 1})
        capabilities = {"k8s"}
        try:
            discovery = self.reader.discover()
        except (PermissionError, FileNotFoundError):
            discovery = {}
        if "pods" in discovery.get("metrics.k8s.io", {}):
            capabilities.add("metrics")
        argo = discovery.get("argoproj.io", {})
        if "rollouts" in argo and "analysisruns" in argo:
            capabilities.add("rollouts")
        if "applications" in argo:
            try:
                self.reader.list(
                    self.reader.api_versions["argoproj.io"], "applications", params={"limit": 1}
                )
            except (PermissionError, FileNotFoundError):
                pass
            else:
                capabilities.add("argocd")
        karpenter = discovery.get("karpenter.sh", {})
        if "nodepools" in karpenter and "nodeclaims" in karpenter:
            capabilities.add("karpenter")
        if self.aws_enabled:
            try:
                self.eks.describe_cluster(name=self.cluster_name)
            except Exception:
                LOGGER.warning("spoke_aws_capability_unavailable")
            else:
                capabilities.add("aws")
        self._capabilities = capabilities
        return sorted(capabilities)

    def capabilities(self) -> list[str]:
        return sorted(self._capabilities)

    def advertised_tools(self) -> list[str]:
        return sorted(
            name
            for name, tool in TOOL_REGISTRY.items()
            if not tool.hub_local and tool.capability in self._capabilities
        )

    async def prepare(self) -> None:
        import time

        deadline_token = DEADLINE.set(time.monotonic() + self.reader.timeout_s)
        try:
            await asyncio.wait_for(
                self._run_blocking(self.detect_capabilities), self.reader.timeout_s
            )
        finally:
            DEADLINE.reset(deadline_token)

    async def execute(self, tool_name: str, args: dict[str, Any]) -> ExecutionResult:
        return await self._run_blocking(lambda: self._execute(tool_name, args))

    def _execute(self, tool_name: str, args: dict[str, Any]) -> ExecutionResult:
        TRUNCATED.set(False)
        definition = TOOL_REGISTRY[tool_name]
        if definition.capability is None:
            raise ValueError("hub-local tools cannot execute on the spoke")
        validated = definition.args_model.model_validate(args).model_dump(
            mode="json", exclude_none=True
        )
        denied = self.policy.check_tool_args(tool_name, validated)
        if denied:
            raise PermissionError("namespace is outside spoke policy")
        group = self._groups[
            definition.capability
            if definition.capability not in {"metrics", "karpenter"}
            else "k8s"
        ]
        data = group.execute(tool_name, validated)
        return ExecutionResult(data, TRUNCATED.get())

    async def _run_blocking(self, fn: Any) -> Any:
        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        try:
            future = self._workers.submit(context.run, fn)
        except BaseException:
            self._slots.release()
            raise

        def release_slot(done: Any) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._slots.release)

        future.add_done_callback(release_slot)
        wrapped = asyncio.wrap_future(future)

        def consume_error(done: asyncio.Future[Any]) -> None:
            if not done.cancelled():
                done.exception()

        wrapped.add_done_callback(consume_error)
        return await asyncio.shield(wrapped)

    async def close(self) -> None:
        await asyncio.to_thread(self._workers.shutdown, wait=True, cancel_futures=True)
        self.reader.close()
        for group in (
            self.sts,
            self.eks,
            self._groups["aws"].elbv2,
            self._groups["aws"].cloudwatch,
        ):
            if group is not None:
                group.close()
