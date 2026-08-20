from __future__ import annotations

import os

import pytest

from automation_orchestrator.swirl_client import SwirlClient


@pytest.mark.docker
def test_self_hosted_swirl_search():
    if os.environ.get("SWIRL_DOCKER_E2E") != "1":
        pytest.skip("set SWIRL_DOCKER_E2E=1 inside the Compose service")

    client = SwirlClient.from_environment()
    assert client is not None

    response = client.search("automation healthcheck", max_results=3)

    assert response.search_id is not None
    assert 0 < len(response.results) <= 3
