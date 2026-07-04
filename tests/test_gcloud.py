"""Unit tests for the gcloud CLI wrapper (parsing, run-ids, command shape,
retry behavior) using an injected fake runner — no real gcloud needed."""
import json
import subprocess

from composer_flow.models.execution import NodeStatus
from composer_flow.models.workflow import DagNode
from composer_flow.services.gcloud import (
    ComposerTarget,
    GcloudClient,
    extract_json,
    generate_run_id,
)

TARGET = ComposerTarget(environment="my-env", location="europe-west1", project="my-proj")


class FakeRunner:
    """Callable standing in for subprocess.run."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        rc, out, err = self.responses.pop(0) if self.responses else (0, "", "")
        return subprocess.CompletedProcess(cmd, rc, out, err)


def make_client(responses, retries=2):
    client = GcloudClient(retry_count=retries, retry_backoff_seconds=0,
                          runner=FakeRunner(responses))
    client._gcloud_path = "gcloud"  # skip PATH lookup in tests
    return client


# -- extract_json --------------------------------------------------------

def test_extract_json_from_noisy_kubectl_output():
    noisy = (
        "kubectl exec pod/airflow-worker ...\n"
        "Defaulting container name to airflow-worker.\n"
        '[{"dag_id": "my_dag", "run_id": "cf__x__1", "state": "running"}]\n'
        "some trailing log line\n"
    )
    parsed = extract_json(noisy)
    assert parsed == [{"dag_id": "my_dag", "run_id": "cf__x__1", "state": "running"}]


def test_extract_json_skips_invalid_candidates():
    text = "{broken json} then later " + json.dumps({"ok": True})
    assert extract_json(text) == {"ok": True}


def test_extract_json_none_when_absent():
    assert extract_json("no json here at all") is None


# -- run ids / conf ---------------------------------------------------------

def test_generate_run_id_unique_and_sanitized():
    a = generate_run_id("My Run! 2026")
    b = generate_run_id("My Run! 2026")
    assert a != b
    assert a.startswith("cf__My_Run__2026__") or a.startswith("cf__My_Run_")
    assert " " not in a and "!" not in a


def test_node_conf_json_matches_requirement():
    node = DagNode(dag_id="d", params={
        "schedule_date_time": "202605250000",
        "country": "UK",
    })
    assert json.loads(node.conf_json()) == {
        "schedule_date_time": "202605250000",
        "country": "UK",
    }


# -- command construction ----------------------------------------------------

def test_trigger_command_shape():
    client = make_client([(0, "triggered", "")])
    result = client.trigger_dag(TARGET, "my_dag", '{"k":"v"}', "cf__run__1")
    assert result.ok
    cmd = client._runner.calls[0]
    assert cmd[:5] == ["gcloud", "composer", "environments", "run", "my-env"]
    assert "--location" in cmd and "europe-west1" in cmd
    assert "--project" in cmd and "my-proj" in cmd
    dd = cmd.index("--")
    assert cmd[dd - 2 : dd] == ["dags", "trigger"]
    assert cmd[dd + 1 :] == ["my_dag", "--run-id", "cf__run__1", "--conf", '{"k":"v"}']


def test_trigger_omits_empty_conf():
    client = make_client([(0, "ok", "")])
    client.trigger_dag(TARGET, "my_dag", "{}", "rid")
    assert "--conf" not in client._runner.calls[0]


def test_get_run_state_maps_airflow_states():
    runs = json.dumps([
        {"dag_id": "d", "run_id": "other", "state": "failed"},
        {"dag_id": "d", "run_id": "mine", "state": "success"},
    ])
    client = make_client([(0, runs, "")])
    state, result = client.get_run_state(TARGET, "d", "mine")
    assert state == NodeStatus.SUCCESS
    assert result.ok


def test_get_run_state_none_when_run_not_visible_yet():
    client = make_client([(0, "[]", "")])
    state, _ = client.get_run_state(TARGET, "d", "mine")
    assert state is None


# -- retries ------------------------------------------------------------

def test_transient_error_is_retried():
    client = make_client([
        (1, "", "ERROR: gateway timeout while contacting server"),
        (0, "[]", ""),
    ])
    result = client.run(["composer", "whatever"])
    assert result.ok
    assert result.attempts == 2


def test_non_transient_error_not_retried():
    client = make_client([
        (1, "", "ERROR: PERMISSION_DENIED: caller lacks permission"),
        (0, "should not be reached", ""),
    ])
    result = client.run(["composer", "whatever"])
    assert not result.ok
    assert result.attempts == 1


def test_trigger_never_blind_retries():
    # A trigger may have side effects; the wrapper must not auto-retry it.
    client = make_client([
        (1, "", "ERROR: deadline exceeded"),
        (0, "would be a duplicate trigger", ""),
    ])
    result = client.trigger_dag(TARGET, "d", "{}", "rid")
    assert not result.ok
    assert len(client._runner.calls) == 1
