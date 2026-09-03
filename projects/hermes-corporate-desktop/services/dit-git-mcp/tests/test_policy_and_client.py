from __future__ import annotations

import base64

import httpx

from dit_git_mcp.client import DitGitError, GitLabClient
from dit_git_mcp.config import ConfigError, Settings


def settings(**overrides):
    values = {
        "base_url": "https://git.mos.ru",
        "token": "test-token",
        "auth_type": "private-token",
        "allowed_projects": frozenset({"dit/special"}),
        "allowed_groups": ("dit/platform",),
        "retries": 0,
    }
    values.update(overrides)
    return Settings(**values)


def test_settings_fail_closed_without_allowlist():
    try:
        settings(allowed_projects=frozenset(), allowed_groups=())
    except ConfigError as exc:
        assert "allow-project" in str(exc)
    else:
        raise AssertionError("settings accepted an empty allowlist")


def test_allow_all_visible_explicitly_disables_namespace_filter():
    cfg = settings(
        allowed_projects=frozenset(),
        allowed_groups=(),
        allow_all_visible=True,
    )

    assert cfg.project_allowed("cicd/kgh/dit.id")
    assert cfg.project_allowed("another-group/another-project")
    assert cfg.group_allowed("another-group")


def test_group_policy_includes_subgroups_but_not_similar_prefixes():
    cfg = settings()
    assert cfg.project_allowed("dit/platform/service")
    assert cfg.project_allowed("DIT/PLATFORM/subgroup/service")
    assert not cfg.project_allowed("dit/platform-other/service")
    assert cfg.project_allowed("dit/special")


def test_list_projects_filters_visible_results_against_allowlist():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "test-token"
        return httpx.Response(
            200,
            headers={"X-Page": "1", "X-Next-Page": "2", "X-Total": "3"},
            json=[
                {"id": 1, "path_with_namespace": "dit/platform/api", "name": "api"},
                {"id": 2, "path_with_namespace": "other/secret", "name": "secret"},
                {"id": 3, "path_with_namespace": "dit/special", "name": "special"},
            ],
        )

    with GitLabClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = client.search_projects()

    assert [item["path"] for item in result["projects"]] == ["dit/platform/api", "dit/special"]
    assert result["pagination"]["next_page"] == 2


def test_read_file_decodes_text_and_bounds_lines():
    content = base64.b64encode(b"one\ntwo\nthree\nfour\n").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).split("?", 1)[0].endswith(
            "/projects/dit%2Fplatform%2Fapi/repository/files/src%2Fapp.py"
        )
        return httpx.Response(
            200,
            json={
                "encoding": "base64",
                "content": content,
                "size": 19,
                "ref": "main",
                "blob_id": "blob",
                "last_commit_id": "commit",
            },
        )

    with GitLabClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = client.read_file("dit/platform/api", "src/app.py", ref="main", start_line=2, end_line=3)

    assert result["content"] == "two\nthree"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["truncated"] is True


def test_project_outside_allowlist_is_rejected_before_repository_request():
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("forbidden path reached the upstream API")

    with GitLabClient(settings(), transport=httpx.MockTransport(handler)) as client:
        try:
            client.list_tree("other/secret")
        except DitGitError as exc:
            assert exc.code == "project_not_allowed"
        else:
            raise AssertionError("forbidden project was accepted")


def test_non_get_requests_are_rejected_before_transport():
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("write request reached the upstream transport")

    with GitLabClient(settings(), transport=httpx.MockTransport(handler)) as client:
        try:
            client._request("POST", "projects/1")
        except DitGitError as exc:
            assert exc.code == "read_only_violation"
        else:
            raise AssertionError("write request was accepted")


def test_exact_project_search_does_not_scan_global_projects():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/projects/dit%2Fspecial")
        return httpx.Response(
            200,
            json={
                "id": 7,
                "path_with_namespace": "dit/special",
                "name": "special",
                "description": "Allowed project",
                "default_branch": "main",
            },
        )

    cfg = settings(allowed_groups=())
    with GitLabClient(cfg, transport=httpx.MockTransport(handler)) as client:
        result = client.search_projects("special")

    assert result["pagination"]["total"] == 1
    assert [item["path"] for item in result["projects"]] == ["dit/special"]


def test_tree_depth_is_bounded_and_pinned_to_commit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/dit%2Fspecial"):
            return httpx.Response(200, json={"path_with_namespace": "dit/special", "default_branch": "main"})
        if request.url.path.endswith("/repository/commits/main"):
            return httpx.Response(200, json={"id": "abc123", "title": "head"})
        assert request.url.path.endswith("/repository/tree")
        assert request.url.params["ref"] == "abc123"
        assert request.url.params["recursive"] == "true"
        return httpx.Response(
            200,
            json=[
                {"id": "1", "name": "src", "path": "src", "type": "tree", "mode": "040000"},
                {"id": "2", "name": "app.py", "path": "src/app.py", "type": "blob", "mode": "100644"},
                {"id": "3", "name": "deep.py", "path": "src/pkg/deep.py", "type": "blob", "mode": "100644"},
            ],
        )

    cfg = settings(allowed_groups=())
    with GitLabClient(cfg, transport=httpx.MockTransport(handler)) as client:
        result = client.list_tree("dit/special", depth=2)

    assert result["commit_sha"] == "abc123"
    assert [item["path"] for item in result["entries"]] == ["src", "src/app.py"]
