import io
import json

import pytest

from automation_orchestrator.gitea_client import GiteaClient, GiteaClientError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_creates_idempotent_final_pull_request_for_exact_branch():
    requests = []
    responses = iter(
        [
            {"default_branch": "main"},
            [],
            {
                "number": 12,
                "html_url": "http://gitea/team/service/pulls/12",
                "head": {"ref": "automation/wf-1"},
                "base": {"ref": "main"},
            },
        ]
    )

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps(next(responses)).encode())

    client = GiteaClient(
        "http://gitea:3000",
        "secret",
        {"team/service"},
        opener=opener,
    )

    pull = client.create_final_pull_request(
        repository="team/service",
        head="automation/wf-1",
        commit="a" * 40,
        workflow_id="wf-1",
        title="Validated ticket",
    )

    assert pull == {
        "repository": "team/service",
        "index": 12,
        "url": "http://gitea/team/service/pulls/12",
        "base": "main",
        "head": "automation/wf-1",
        "commit": "a" * 40,
        "reused": False,
    }
    posted = json.loads(requests[-1].data)
    assert posted["base"] == "main"
    assert posted["head"] == "automation/wf-1"
    assert "automation-workflow: wf-1" in posted["body"]
    assert "automation-idempotency-key: wf-1-final-pull-request" in posted["body"]


def test_rejects_repository_outside_allowlist():
    client = GiteaClient(
        "http://gitea:3000",
        "secret",
        {"team/service"},
        opener=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(GiteaClientError, match="not allowed"):
        client.create_final_pull_request(
            repository="other/service",
            head="automation/wf-1",
            commit="a" * 40,
            workflow_id="wf-1",
            title="Validated ticket",
        )


def test_verifies_remote_branch_commit():
    def opener(_request, **_kwargs):
        return Response(json.dumps({"commit": {"id": "a" * 40}}).encode())

    client = GiteaClient(
        "http://gitea:3000",
        "secret",
        {"team/service"},
        opener=opener,
    )

    client.verify_branch(
        repository="team/service",
        branch="automation/wf-1",
        commit="a" * 40,
    )

    with pytest.raises(GiteaClientError, match="does not point"):
        client.verify_branch(
            repository="team/service",
            branch="automation/wf-1",
            commit="b" * 40,
        )


def test_verifies_commit_ancestry_from_gitea_graph():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({"parents": [{"sha": "b" * 40}]}).encode())

    client = GiteaClient(
        "http://gitea:3000",
        "secret",
        {"team/service"},
        opener=opener,
    )

    client.verify_descendant(
        repository="team/service",
        ancestor="b" * 40,
        descendant="a" * 40,
    )

    assert requests[0].full_url.endswith(f"/git/commits/{'a' * 40}")


def test_rejects_commit_outside_required_ancestry():
    responses = iter(
        [
            {"parents": [{"sha": "c" * 40}]},
            {"parents": []},
        ]
    )
    client = GiteaClient(
        "http://gitea:3000",
        "secret",
        {"team/service"},
        opener=lambda *_args, **_kwargs: Response(json.dumps(next(responses)).encode()),
    )

    with pytest.raises(GiteaClientError, match="not descended"):
        client.verify_descendant(
            repository="team/service",
            ancestor="b" * 40,
            descendant="a" * 40,
        )


def test_downloads_exact_commit_archive_with_authentication():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(b"archive")

    client = GiteaClient(
        "http://gitea:3000",
        "secret",
        {"team/service"},
        opener=opener,
    )

    payload = client.download_archive(repository="team/service", commit="a" * 40)

    assert payload == b"archive"
    assert requests[0].get_header("Authorization") == "token secret"
    assert requests[0].full_url.endswith(f"/archive/{'a' * 40}.tar.gz")
