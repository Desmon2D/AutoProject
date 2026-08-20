from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .models import TriggerEvent

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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
    explicitly_ready = data.get("ready_for_development") is True
    configured_ready = bool(
        state_ids.intersection(ready_state_ids) or state_names.intersection(ready_state_names)
    )
    if not explicitly_ready and not configured_ready:
        return PlaneWebhookResult(trigger=None, reason="issue is not ready for development")

    project_references = _project_references(data)
    repository = next(
        (repositories[reference] for reference in project_references if reference in repositories),
        None,
    )
    if repository is None:
        raise PlaneWebhookError("Plane project has no Gitea repository mapping")

    webhook_id = str(payload.get("webhook_id", "")).strip()
    changed_at = _first_text(data, "updated_at", "created_at") or "unknown"
    stable_source = f"{webhook_id}:{event}:{action}:{issue_id}:{changed_at}"
    event_id = f"plane-{hashlib.sha256(stable_source.encode()).hexdigest()[:32]}"
    description = _first_text(
        data,
        "description_stripped",
        "description_html",
        "description",
    )

    normalized = {
        "ticket": {
            "id": issue_id,
            "sequence_id": data.get("sequence_id"),
            "summary": summary[:2000],
            "description": description[:20_000] if description else None,
            "priority": data.get("priority"),
            "state_ids": sorted(state_ids),
            "state_names": sorted(state_names),
        },
        "project": {
            "references": project_references,
            "workspace_id": payload.get("workspace_id"),
        },
        "repository": {"full_name": repository},
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
            event="issue.ready_for_development",
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
