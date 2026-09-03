from __future__ import annotations

import re

from .models import TriggerEvent, WorkflowInstance
from .plane_client import PlaneClientError
from .workflow_errors import WorkflowExecutionError


class PlaneImplementationMixin:
    """Resolves trusted Plane repository links and exact implementation revisions."""

    def attach_plane_implementation(self, event: TriggerEvent) -> TriggerEvent:
        if event.source != "plane" or event.event not in {
            "issue.ready_for_development",
            "issue.testing",
        }:
            return event
        event = self._attach_plane_repository(event)
        ticket = event.data.get("ticket")
        repository_data = event.data.get("repository")
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        repository = repository_data.get("full_name") if isinstance(repository_data, dict) else None
        if event.event == "issue.ready_for_development":
            rework = self._failed_test_rework_source(
                ticket_id=ticket_id,
                repository=repository,
            )
            if rework is None:
                return event
            enriched = event.model_copy(deep=True)
            enriched.data["repository"].update(rework["repository"])
            enriched.data["development_feedback"] = rework["feedback"]
            return enriched
        supplied_ref = (
            repository_data.get("implementation_ref") if isinstance(repository_data, dict) else None
        )
        if not isinstance(ticket_id, str) or not isinstance(repository, str):
            raise WorkflowExecutionError("testing event has no Plane issue or repository")

        enriched_event = event
        plane_client = self.command_executor.plane_client
        if supplied_ref is None and plane_client is not None:
            project = event.data.get("project")
            project_id = project.get("id") if isinstance(project, dict) else None
            if not isinstance(project_id, str) and isinstance(project, dict):
                references = project.get("references")
                project_id = (
                    next((item for item in references if isinstance(item, str)), None)
                    if isinstance(references, list)
                    else None
                )
            if isinstance(project_id, str):
                try:
                    source = plane_client.get_implementation_source(
                        project_id=project_id,
                        issue_id=ticket_id,
                    )
                except PlaneClientError as exc:
                    raise WorkflowExecutionError(str(exc)) from exc
                if source is not None:
                    enriched_event = event.model_copy(deep=True)
                    enriched_event.data["repository"].update(source)
                    supplied_ref = source["implementation_ref"]

        for workflow in self.store.list():
            source_ticket = workflow.trigger.data.get("ticket")
            source_repository = workflow.trigger.data.get("repository")
            implementation_is_ready = (
                workflow.status == "COMPLETED" and workflow.outcome == "SUCCESS"
            ) or (
                workflow.status == "WAITING"
                and workflow.pending_review is not None
                and workflow.pending_review.provider == "plane"
            )
            if (
                workflow.scenario_id != "implement-ticket"
                or not implementation_is_ready
                or not isinstance(source_ticket, dict)
                or source_ticket.get("id") != ticket_id
                or not isinstance(source_repository, dict)
                or source_repository.get("full_name") != repository
            ):
                continue
            change = next(
                (
                    execution.data.get("implementation_change")
                    for execution in reversed(workflow.executions)
                    if execution.execution_status == "COMPLETED"
                    and execution.outcome == "SUCCESS"
                    and isinstance(execution.data.get("implementation_change"), dict)
                ),
                None,
            )
            if not isinstance(change, dict):
                continue
            branch = change.get("branch")
            commit = change.get("commit")
            if (
                change.get("repository") != repository
                or not isinstance(branch, str)
                or not branch
                or not isinstance(commit, str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
            ):
                continue
            if supplied_ref is not None and supplied_ref != branch:
                raise WorkflowExecutionError(
                    "Plane implementation ref does not match the completed implementation workflow"
                )
            enriched = enriched_event.model_copy(deep=True)
            enriched_repository = enriched.data["repository"]
            enriched_repository["implementation_ref"] = branch
            enriched_repository["implementation_commit"] = commit.lower()
            enriched_repository["implementation_workflow_id"] = workflow.id
            return enriched

        if supplied_ref is not None:
            return enriched_event
        raise WorkflowExecutionError(
            "no completed implementation workflow was found for this Plane issue"
        )

    def _failed_test_rework_source(
        self,
        *,
        ticket_id: object,
        repository: object,
    ) -> dict[str, object] | None:
        if not isinstance(ticket_id, str) or not isinstance(repository, str):
            return None
        candidates = sorted(self.store.list(), key=lambda item: item.created_at, reverse=True)
        for workflow in candidates:
            source_ticket = workflow.trigger.data.get("ticket")
            if (
                workflow.scenario_id != "test-ticket"
                or not isinstance(source_ticket, dict)
                or source_ticket.get("id") != ticket_id
            ):
                continue
            report = next(
                (
                    execution.data.get("test_report")
                    for execution in reversed(workflow.executions)
                    if isinstance(execution.data.get("test_report"), dict)
                    and execution.data["test_report"].get("verdict") == "PRODUCT_FAILURE"
                ),
                None,
            )
            change = next(
                (
                    execution.data.get("test_change")
                    for execution in reversed(workflow.executions)
                    if isinstance(execution.data.get("test_change"), dict)
                ),
                None,
            )
            if not isinstance(report, dict) or not isinstance(change, dict):
                continue
            branch = change.get("branch")
            commit = change.get("commit")
            if (
                change.get("repository") != repository
                or not isinstance(branch, str)
                or not branch
                or not isinstance(commit, str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
            ):
                continue
            return {
                "repository": {
                    "implementation_ref": branch,
                    "implementation_commit": commit.lower(),
                    "implementation_workflow_id": workflow.id,
                    "selection_source": "failed_test_workflow",
                },
                "feedback": {
                    "test_workflow_id": workflow.id,
                    "verdict": report.get("verdict"),
                    "command": report.get("command"),
                    "exit_code": report.get("exit_code"),
                    "passed": report.get("passed"),
                    "failed": report.get("failed"),
                    "summary": report.get("summary"),
                    "output": str(report.get("output", ""))[-8000:],
                },
            }
        return None

    def find_plane_workflow(
        self,
        event: TriggerEvent,
        *,
        scenario_id: str,
        statuses: set[str],
        review_provider: str | None = None,
    ) -> WorkflowInstance | None:
        ticket = event.data.get("ticket")
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        if not isinstance(ticket_id, str):
            return None
        for workflow in self.store.list():
            source_ticket = workflow.trigger.data.get("ticket")
            if (
                workflow.scenario_id != scenario_id
                or workflow.status not in statuses
                or not isinstance(source_ticket, dict)
                or source_ticket.get("id") != ticket_id
            ):
                continue
            if review_provider is not None and (
                workflow.pending_review is None
                or workflow.pending_review.provider != review_provider
            ):
                continue
            return workflow
        return None

    def _attach_plane_repository(self, event: TriggerEvent) -> TriggerEvent:
        ticket = event.data.get("ticket")
        project = event.data.get("project")
        repository_data = event.data.get("repository")
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(project_id, str) and isinstance(project, dict):
            references = project.get("references")
            project_id = (
                next((item for item in references if isinstance(item, str)), None)
                if isinstance(references, list)
                else None
            )

        linked_source = None
        plane_client = self.command_executor.plane_client
        if (
            plane_client is not None
            and isinstance(project_id, str)
            and isinstance(ticket_id, str)
        ):
            try:
                linked_source = plane_client.get_repository_source(
                    project_id=project_id,
                    issue_id=ticket_id,
                )
            except PlaneClientError as exc:
                raise WorkflowExecutionError(str(exc)) from exc

        if linked_source is not None:
            linked_repository = linked_source["full_name"]
            gitea_client = self.command_executor.gitea_client
            if gitea_client is None:
                raise WorkflowExecutionError(
                    "Gitea client is required to validate the Plane repository link"
                )
            if linked_repository.casefold() not in gitea_client.allowed_repositories:
                raise WorkflowExecutionError(
                    "Plane repository link is not in GITEA_ALLOWED_REPOSITORIES"
                )
            enriched = event.model_copy(deep=True)
            enriched.data.setdefault("repository", {})
            enriched.data["repository"].update(
                {
                    "full_name": linked_repository,
                    "selection_source": "plane_link",
                    "source_url": linked_source["source_url"],
                }
            )
            return enriched

        if not isinstance(repository, str):
            raise WorkflowExecutionError(
                "Plane issue has neither a repository link nor a project repository mapping"
            )
        return event
