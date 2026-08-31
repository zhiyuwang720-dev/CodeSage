import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent.task_queue import AGENT_TASK_JOB_NAME
from app.worker.agent_worker import WorkerSettings, decode_task_payload


def test_decode_task_payload_reads_json_task_id():
    assert decode_task_payload('{"task_id": "task-1"}') == "task-1"


@pytest.mark.parametrize("payload", ["", "{}", "{bad json", '{"task_id": ""}'])
def test_decode_task_payload_rejects_invalid_payload(payload):
    with pytest.raises(ValueError):
        decode_task_payload(payload)


def test_agent_worker_settings_use_arq_queue():
    from app.core.config import settings

    assert WorkerSettings.queue_name == settings.AGENT_TASK_QUEUE_NAME
    assert WorkerSettings.max_jobs == settings.AGENT_WORKER_CONCURRENCY
    assert WorkerSettings.job_timeout == settings.AGENT_WORKER_JOB_TIMEOUT_SECONDS
    assert WorkerSettings.max_tries == settings.AGENT_WORKER_MAX_TRIES
    assert WorkerSettings.functions[0].name == AGENT_TASK_JOB_NAME
