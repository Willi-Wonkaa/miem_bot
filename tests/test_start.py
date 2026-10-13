from unittest.mock import Mock

import pytest

from scripts.start import GitHub, find_run, github_token, main, wait_for_run


def test_environment_token_does_not_invoke_git(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    git = Mock(side_effect=AssertionError("Git must not be needed"))
    monkeypatch.setattr("scripts.start.subprocess.run", git)
    assert github_token() == "test-token"
    git.assert_not_called()


def test_find_run_ignores_another_dispatch():
    client = Mock()
    client.request.return_value = {
        "workflow_runs": [
            {"id": 2, "display_title": "MIEM index / other-request"},
            {"id": 1, "display_title": "MIEM index / requested-id"},
        ]
    }
    assert find_run(client, "requested-id", "main")["id"] == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_failed_workflow_cannot_report_success(monkeypatch, conclusion):
    run = {"id": 1, "status": "completed", "conclusion": conclusion, "html_url": "test-url"}
    monkeypatch.setattr("scripts.start.find_run", lambda *args: run)
    with pytest.raises(RuntimeError, match=conclusion):
        wait_for_run(Mock(), "request", "main", 30)


def test_successful_ci_with_skipped_deploy_is_a_failure(monkeypatch):
    run = {"id": 1, "status": "completed", "conclusion": "success", "html_url": "test-url"}
    monkeypatch.setattr("scripts.start.find_run", lambda *args: run)
    client = Mock()
    client.request.return_value = {"jobs": [{"name": "deploy", "conclusion": "skipped"}]}
    with pytest.raises(RuntimeError, match="deployment was skipped"):
        wait_for_run(client, "request", "main", 30)


def test_default_command_dispatches_index(monkeypatch):
    client = Mock()
    client.request.side_effect = [{"state": "active"}, None]
    monkeypatch.setattr("scripts.start.github_token", lambda: "test-token")
    monkeypatch.setattr("scripts.start.GitHub", lambda *args: client)
    wait = Mock()
    monkeypatch.setattr("scripts.start.wait_for_run", wait)
    assert main(["--no-wait"]) == 0
    dispatch = client.request.call_args.args[1]
    assert dispatch["ref"] == "main" and dispatch["inputs"]["mode"] == "index"
    assert len(dispatch["inputs"]["request_id"]) == 32
    assert wait.call_args.kwargs == {"wait": False}


def test_invalid_repository_is_rejected():
    with pytest.raises(ValueError, match="OWNER/NAME"):
        GitHub("https://other-host/repo", "test-token")
