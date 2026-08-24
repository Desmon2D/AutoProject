from automation_orchestrator.plane_webhook import normalize_plane_webhook


def test_ready_issue_can_defer_repository_resolution_to_plane_links():
    result = normalize_plane_webhook(
        {
            "event": "issue",
            "action": "update",
            "webhook_id": "webhook-1",
            "data": {
                "id": "issue-1",
                "name": "Add payment retry",
                "description_html": "<p>Retry transient gateway failures.</p>",
                "updated_at": "2026-08-24T12:00:00Z",
                "project": {"id": "project-1", "identifier": "PAY"},
                "state_detail": {"name": "Ready for development"},
            },
        },
        delivery="delivery-1",
        repositories={},
        ready_state_ids=set(),
        ready_state_names={"ready for development"},
    )

    assert result.trigger is not None
    assert result.trigger.data["repository"] == {
        "full_name": None,
        "implementation_ref": None,
        "selection_source": "unresolved",
    }
    assert result.trigger.data["ticket"]["search_query"] == (
        "Add payment retry. Retry transient gateway failures."
    )


def test_cancelled_issue_is_normalized_for_active_workflow_cancellation():
    result = normalize_plane_webhook(
        {
            "event": "issue",
            "action": "updated",
            "webhook_id": "webhook-cancelled",
            "data": {
                "id": "issue-1",
                "name": "Cancelled payment change",
                "updated_at": "2026-08-24T13:00:00Z",
                "project": {"id": "project-1", "identifier": "PAY"},
                "state_detail": {"id": "state-cancelled", "name": "Cancelled"},
            },
        },
        delivery="delivery-cancelled",
        repositories={"PAY": "team/service"},
        ready_state_ids=set(),
        ready_state_names={"ready for development"},
        cancelled_state_ids={"state-cancelled"},
        cancelled_state_names={"cancelled"},
    )

    assert result.trigger is not None
    assert result.trigger.event == "issue.cancelled"
