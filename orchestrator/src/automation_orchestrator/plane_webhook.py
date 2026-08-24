from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any

from .models import TriggerEvent

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPOSITORY_MARKER = re.compile(
    r"(?im)^\s*Automation repository:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*$"
)
IMPLEMENTATION_REF_MARKER = re.compile(
    r"(?im)^\s*Automation implementation ref:\s*([A-Za-z0-9][A-Za-z0-9._/-]{0,199})\s*$"
)


class PlaneWebhookError(ValueError):
    pass


@dataclass(frozen=True)
class PlaneWebhookResult:
    trigger: TriggerEvent | None
    reason: str | None = None


def parse_project_repositories(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlaneWebhookError("PLANE_PROJECT_REPOSITORIES must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PlaneWebhookError("PLANE_PROJECT_REPOSITORIES must be a JSON object")
    repositories: dict[str, str] = {}
    for project, repository in value.items():
        if not isinstance(project, str) or not project.strip():
            raise PlaneWebhookError("Plane project mapping contains an invalid project")
        if not isinstance(repository, str) or not REPOSITORY_PATTERN.fullmatch(repository):
            raise PlaneWebhookError("Plane project mapping contains an invalid Gitea repository")
        repositories[project.strip()] = repository
    return repositories


def parse_csv(raw: str) -> set[str]:
    return {item.strip().casefold() for item in raw.split(",") if item.strip()}


def normalize_plane_webhook(
    payload: dict[str, Any],
    *,
    delivery: str | None,
    repositories: dict[str, str],
    ready_state_ids: set[str],
    ready_state_names: set[str],
    testing_state_ids: set[str] | None = None,
    testing_state_names: set[str] | None = None,
    cancelled_state_ids: set[str] | None = None,
    cancelled_state_names: set[str] | None = None,
) -> PlaneWebhookResult:
    event = str(payload.get("event", "")).strip().casefold()
    action = str(payload.get("action", "")).strip().casefold()
    if event != "issue":
        return PlaneWebhookResult(trigger=None, reason="unsupported event")
    if action not in {"create", "created", "update", "updated"}:
        return PlaneWebhookResult(trigger=None, reason="unsupported action")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise PlaneWebhookError("Plane webhook data must be an object")
    issue_id = _required_text(data, "id", "Plane issue has no id")
    summary = _first_text(data, "name", "title")
    if summary is None:
        raise PlaneWebhookError("Plane issue has no name")

    state_ids, state_names = _state_values(data)
    testing_state_ids = testing_state_ids or set()
    testing_state_names = testing_state_names or set()
    cancelled_state_ids = cancelled_state_ids or set()
    cancelled_state_names = cancelled_state_names or set()
    configured_cancelled = bool(
        state_ids.intersection(cancelled_state_ids)
        or state_names.intersection(cancelled_state_names)
    )
    explicitly_testing = data.get("ready_for_testing") is True or data.get("testing") is True
    configured_testing = bool(
        state_ids.intersection(testing_state_ids)
        or state_names.intersection(testing_state_names)
    )
    explicitly_ready = data.get("ready_for_development") is True
    configured_ready = bool(
        state_ids.intersection(ready_state_ids) or state_names.intersection(ready_state_names)
    )
    if configured_cancelled:
        trigger_event = "issue.cancelled"
    elif explicitly_testing or configured_testing:
        trigger_event = "issue.testing"
    elif explicitly_ready or configured_ready:
        trigger_event = "issue.ready_for_development"
    else:
        return PlaneWebhookResult(trigger=None, reason="issue is not in an actionable state")

    project_references = _project_references(data)
    project_id, project_identifier = _project_identity(data)
    mapped_repository = next(
        (repositories[reference] for reference in project_references if reference in repositories),
        None,
    )
    description = _first_text(
        data,
        "description_stripped",
        "description_html",
        "description",
    )
    task_repository, implementation_ref = _automation_source(description)
    if task_repository is not None and task_repository != mapped_repository:
        raise PlaneWebhookError(
            "Plane task repository does not match its allowed project repository"
        )
    repository = task_repository or mapped_repository
    repository_source = (
        "description_marker"
        if task_repository is not None
        else "project_mapping"
        if mapped_repository is not None
        else "unresolved"
    )
    plain_description = _plain_text(description)
    search_query = (
        f"{summary}. {plain_description}" if plain_description else summary
    )[:2000]
    webhook_id = str(payload.get("webhook_id", "")).strip()
    changed_at = _first_text(data, "updated_at", "created_at") or "unknown"
    stable_source = f"{webhook_id}:{trigger_event}:{action}:{issue_id}:{changed_at}"
    event_id = f"plane-{hashlib.sha256(stable_source.encode()).hexdigest()[:32]}"
    normalized = {
        "ticket": {
            "id": issue_id,
            "sequence_id": data.get("sequence_id"),
            "summary": summary[:2000],
            "description": description[:20_000] if description else None,
            "search_query": search_query,
            "priority": data.get("priority"),
            "state_ids": sorted(state_ids),
            "state_names": sorted(state_names),
        },
        "project": {
            "id": project_id,
            "identifier": project_identifier,
            "references": project_references,
            "workspace_id": payload.get("workspace_id"),
        },
        "repository": {
            "full_name": repository,
            "implementation_ref": implementation_ref,
            "selection_source": repository_source,
        },
        "plane": {
            "delivery": delivery,
            "webhook_id": webhook_id or None,
            "action": action,
            "activity": payload.get("activity")
            if isinstance(payload.get("activity"), dict)
            else None,
        },
    }
    return PlaneWebhookResult(
        trigger=TriggerEvent(
            source="plane",
            event=trigger_event,
            event_id=event_id,
            data=normalized,
        )
    )


def _required_text(data: dict[str, Any], key: str, message: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlaneWebhookError(message)
    return value.strip()


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _state_values(data: dict[str, Any]) -> tuple[set[str], set[str]]:
    identifiers: set[str] = set()
    names: set[str] = set()
    for key in ("state", "state_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            identifiers.add(value.strip().casefold())
    for key in ("state", "state_detail"):
        value = data.get(key)
        if not isinstance(value, dict):
            continue
        for identifier_key in ("id", "uuid"):
            identifier = value.get(identifier_key)
            if isinstance(identifier, str) and identifier.strip():
                identifiers.add(identifier.strip().casefold())
        for name_key in ("name", "group"):
            name = value.get(name_key)
            if isinstance(name, str) and name.strip():
                names.add(name.strip().casefold())
    return identifiers, names


def _project_references(data: dict[str, Any]) -> list[str]:
    references: list[str] = []
    for key in ("project", "project_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            references.append(value.strip())
    for key in ("project", "project_detail"):
        value = data.get(key)
        if not isinstance(value, dict):
            continue
        for reference_key in ("id", "identifier"):
            reference = value.get(reference_key)
            if isinstance(reference, str) and reference.strip():
                references.append(reference.strip())
    return list(dict.fromkeys(references))


def _project_identity(data: dict[str, Any]) -> tuple[str | None, str | None]:
    project = data.get("project")
    if isinstance(project, str) and project.strip():
        return project.strip(), None
    if not isinstance(project, dict):
        project = data.get("project_detail")
    if not isinstance(project, dict):
        return None, None
    project_id = project.get("id")
    identifier = project.get("identifier")
    return (
        project_id.strip() if isinstance(project_id, str) and project_id.strip() else None,
        identifier.strip() if isinstance(identifier, str) and identifier.strip() else None,
    )


def _automation_source(description: str | None) -> tuple[str | None, str | None]:
    if description is None:
        return None, None
    plain = html.unescape(re.sub(r"<[^>]{1,500}>", "\n", description))
    repository_match = REPOSITORY_MARKER.search(plain)
    ref_match = IMPLEMENTATION_REF_MARKER.search(plain)
    implementation_ref = ref_match.group(1) if ref_match else None
    if implementation_ref is not None and (
        ".." in implementation_ref
        or "//" in implementation_ref
        or implementation_ref.endswith(("/", ".lock"))
    ):
        raise PlaneWebhookError("Testing task has an invalid implementation ref")
    return (
        repository_match.group(1) if repository_match else None,
        implementation_ref,
    )


def _plain_text(value: str | None) -> str:
    if value is None:
        return ""
    plain = html.unescape(re.sub(r"<[^>]{1,500}>", " ", value))
    return " ".join(plain.split())
