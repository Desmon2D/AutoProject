from automation_orchestrator.swirl_lite import build_response, search_documents


def test_searches_local_bookstack_documents():
    documents = [
        {
            "title": "Payment retry runbook",
            "body": "Use bounded backoff",
            "url": "http://bookstack/retry",
            "source": "Local BookStack",
            "date_published": "2026-08-18",
        },
        {
            "title": "Unrelated page",
            "body": "Office calendar",
            "url": "http://bookstack/calendar",
            "source": "Local BookStack",
            "date_published": "",
        },
    ]

    results = search_documents(
        documents,
        "payment retry",
        providers=["bookstack"],
        limit=5,
    )

    assert [item["title"] for item in results] == ["Payment retry runbook"]
    assert results[0]["swirl_score"] == 1.0


def test_builds_swirl_compatible_grouped_response():
    documents = [
        {
            "title": "Payment retry runbook",
            "body": "Use bounded backoff",
            "url": "http://bookstack/retry",
            "source": "Local BookStack",
            "date_published": "2026-08-18",
        }
    ]

    response = build_response(
        documents,
        "payment retry",
        providers=["bookstack"],
        limit=5,
    )

    assert response["info"]["search"]["id"]
    assert response["results"][0]["searchprovider"] == "Local BookStack"
    assert response["results"][0]["json_results"][0]["url"] == "http://bookstack/retry"
