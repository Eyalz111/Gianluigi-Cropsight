"""
Cloud Run admin client — one method, used by the rollout orchestrator to
update env vars on the live service via the v2 admin API.

ADC (Application Default Credentials) handles auth on Cloud Run automatically:
the runtime SA needs `roles/run.developer` on the project (a one-time IAM grant).
Locally, `gcloud auth application-default login` provides ADC.

All other env vars are preserved — we GET the current service, patch only the
named keys (add or replace), and UPDATE. Operation runs in a thread so the
async event loop isn't blocked by the admin call's gRPC.
"""

import asyncio
import logging

from config.settings import settings

logger = logging.getLogger(__name__)


class CloudRunAdmin:
    """Thin wrapper around google.cloud.run_v2 for env-var-only updates."""

    def __init__(self):
        self._client = None  # lazy: avoid import-time auth on cold boot

    def _get_client(self):
        if self._client is None:
            from google.cloud import run_v2  # type: ignore
            self._client = run_v2.ServicesClient()
        return self._client

    def _service_name(self) -> str:
        return (
            f"projects/{settings.GCP_PROJECT_ID}"
            f"/locations/{settings.GCP_REGION}"
            f"/services/{settings.CLOUD_RUN_SERVICE_NAME}"
        )

    async def apply_env_changes(self, env_changes: dict[str, str]) -> dict:
        """Patch the named env vars on the live service. Returns {revision, applied}.

        env_changes values are stringified; everything else (image, command, other
        env vars, secrets) is left untouched.
        """
        return await asyncio.to_thread(self._apply_sync, env_changes)

    def _apply_sync(self, env_changes: dict[str, str]) -> dict:
        from google.cloud import run_v2  # type: ignore

        client = self._get_client()
        name = self._service_name()
        service = client.get_service(name=name)

        # Patch env list on the first container (Cloud Run runs a single container).
        container = service.template.containers[0]
        existing = {e.name: e for e in container.env}
        for key, value in env_changes.items():
            v = "" if value is None else str(value)
            if key in existing:
                existing[key].value = v
            else:
                container.env.append(run_v2.EnvVar(name=key, value=v))

        op = client.update_service(service=service)
        result = op.result()  # waits for the rollout to complete

        revision = (
            getattr(result, "latest_ready_revision", None)
            or getattr(result, "latest_created_revision", "")
            or ""
        )
        # Cloud Run returns the full revision resource name; trim to short id.
        if revision and "/" in revision:
            revision = revision.rsplit("/", 1)[-1]
        logger.info(f"[cloud_run_admin] applied env changes; new revision={revision}")
        return {"revision": revision, "applied": list(env_changes.keys())}


# Singleton instance
cloud_run_admin = CloudRunAdmin()
