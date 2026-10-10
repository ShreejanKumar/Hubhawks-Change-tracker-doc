"""Starts the Cloud Run Job execution that processes one run (worker.py)."""

from functools import lru_cache

from google.cloud import run_v2

import history_store
from config import CLOUD_RUN_JOB, CLOUD_RUN_REGION, GCP_PROJECT_ID


@lru_cache(maxsize=1)
def _jobs_client() -> run_v2.JobsClient:
    return run_v2.JobsClient(credentials=history_store.credentials())


def start_run(run_id: str) -> None:
    """Fire and forget: returns once the execution is accepted, without
    waiting for it to finish. Everything after that is reported by the
    worker through the run's Firestore doc."""
    request = run_v2.RunJobRequest(
        name=f"projects/{GCP_PROJECT_ID}/locations/{CLOUD_RUN_REGION}/jobs/{CLOUD_RUN_JOB}",
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name="RUN_ID", value=run_id)]
                )
            ]
        ),
    )
    _jobs_client().run_job(request=request)
