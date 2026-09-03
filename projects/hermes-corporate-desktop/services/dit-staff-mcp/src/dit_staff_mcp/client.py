from __future__ import annotations

import html
import json
import logging
import re
import time
from html.parser import HTMLParser
from typing import Any, Iterable

import httpx

from .config import Settings

logger = logging.getLogger("dit_staff_mcp.client")

_CONTENT_RANGE_RE = re.compile(r"items\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)
_SECRET_KEYS = {
    "access_token",
    "password",
    "pipassword",
    "secret",
    "secretkey",
    "sign",
    "usrsesid",
}

_ADAPTATION_SECTION_KEYS = (
    "testtesttestmoyaadaptatsiya",
    "adaptatsiyanovogosotrudnika",
)
_PLAN_PROGRAM_LOCAL_PARAMS = {
    "routeobjtype",
    "objtablename",
    "routeid",
    "pstype",
    "routeobjtableid",
    "constructorViewId",
    "iscomment",
    "entityFrameName",
    "efvid",
    "processedstate",
    "currentphaseid",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        elif tag.casefold() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag.casefold() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[\t\r\f\v]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


class StaffError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.status is not None:
            result["status"] = self.status
        return result


class MirapolisClient:
    """Bounded, read-only client for user-scoped Mirapolis API modules."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        verify: bool | str = settings.ca_bundle or True
        self._http = httpx.Client(
            verify=verify,
            proxy=settings.proxy,
            trust_env=settings.trust_env,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "DIT-Staff-MCP/0.1.0"},
        )
        self._access_token: str | None = None
        self._expires_at = 0.0
        for cookie in settings.portal_cookies:
            self._http.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", "staff.mos.ru"),
                path=cookie.get("path", "/"),
            )

    def close(self) -> None:
        self._access_token = None
        self._expires_at = 0.0
        self._http.close()

    def _decode(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise StaffError(
                "invalid_response",
                f"staff.mos.ru returned non-JSON data (HTTP {response.status_code})",
                status=response.status_code,
            ) from exc
        if isinstance(payload, dict) and payload.get("errorMessage") is not None:
            message = str(payload.get("errorMessage") or "Mirapolis API error")
            raise StaffError("upstream_error", message[:500], status=response.status_code)
        if response.is_error:
            raise StaffError(
                "http_error",
                f"staff.mos.ru returned HTTP {response.status_code}",
                status=response.status_code,
            )
        return payload

    def _authenticate(self) -> str:
        form = None
        if self.settings.login and self.settings.password:
            form = {"login": self.settings.login, "password": self.settings.password}
        try:
            response = self._http.post(
                f"{self.settings.api_url}/auth/login",
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://staff.mos.ru",
                    "Referer": "https://staff.mos.ru/mira/",
                },
            )
        except httpx.HTTPError as exc:
            raise StaffError("network_error", f"Cannot connect to staff.mos.ru: {exc}") from exc

        payload = self._decode(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            raise StaffError("auth_error", "Mirapolis did not return access_token")
        token = payload["access_token"].strip()
        if not token:
            raise StaffError("auth_error", "Mirapolis returned an empty access_token")
        try:
            lifetime = max(60, int(payload.get("expires_in", 900)))
        except (TypeError, ValueError):
            lifetime = 900
        self._access_token = token
        self._expires_at = time.monotonic() + lifetime
        logger.info("Authenticated to staff.mos.ru; session lifetime=%ss", lifetime)
        return token

    def _token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 30:
            return self._access_token
        return self._authenticate()

    @staticmethod
    def _looks_expired(response: httpx.Response) -> bool:
        if response.status_code in {401, 403}:
            return True
        text = response.text.casefold()[:1000]
        return "access token expired" in text or "not found access_token" in text

    def _get(self, module: str, params: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        url = f"{self.settings.api_url}/{module.lstrip('/')}"
        for attempt in range(2):
            try:
                response = self._http.get(url, params=params, headers={"access_token": self._token()})
            except httpx.HTTPError as exc:
                raise StaffError("network_error", f"Cannot connect to staff.mos.ru: {exc}") from exc
            if attempt == 0 and self._looks_expired(response):
                self._access_token = None
                self._expires_at = 0.0
                continue
            return self._decode(response), response.headers
        raise StaffError("auth_error", "Unable to refresh the Mirapolis session")

    def _clean(self, value: Any, *, key: str = "") -> Any:
        if key.casefold() in _SECRET_KEYS:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): self._clean(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, str):
            parser = _TextExtractor()
            if "<" in value and ">" in value:
                try:
                    parser.feed(value)
                    value = parser.text()
                except Exception:
                    value = re.sub(r"<[^>]+>", " ", value)
            if len(value) > self.settings.max_text_chars:
                value = value[: self.settings.max_text_chars] + "\n[TRUNCATED]"
        return value

    def _bounded(self, payload: Any) -> Any:
        cleaned = self._clean(payload)
        encoded = json.dumps(cleaned, ensure_ascii=False)
        if len(encoded) <= self.settings.max_response_chars:
            return cleaned
        if isinstance(cleaned, list):
            result: list[Any] = []
            size = 2
            for item in cleaned:
                item_size = len(json.dumps(item, ensure_ascii=False)) + 1
                if size + item_size > self.settings.max_response_chars:
                    break
                result.append(item)
                size += item_size
            return result
        raise StaffError("response_too_large", "Portal response exceeds the configured size limit")

    def _page(self, module: str, *, offset: int, limit: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if offset < 0:
            raise StaffError("invalid_argument", "offset must be zero or greater")
        bounded_limit = min(max(1, limit), self.settings.max_items, 200)
        query = dict(params or {})
        query.update({"offset": offset, "limit": bounded_limit})
        payload, headers = self._get(module, query)
        if not isinstance(payload, list):
            raise StaffError("invalid_response", "Expected a list from Mirapolis")
        cleaned = self._bounded(payload)
        content_range = headers.get("Content-Range")
        total: int | None = None
        if content_range and (match := _CONTENT_RANGE_RE.search(content_range)):
            total = int(match.group(3))
        return {
            "items": cleaned,
            "offset": offset,
            "limit": bounded_limit,
            "returned": len(cleaned),
            "total": total,
            "has_more": total is not None and offset + len(cleaned) < total,
        }

    def get_profile(self) -> dict[str, Any]:
        payload, _ = self._get("myProfile")
        if not isinstance(payload, dict):
            raise StaffError("invalid_response", "Expected a profile object from Mirapolis")
        return self._bounded(payload)

    def list_profile_groups(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._page("myProfile/groups", offset=offset, limit=limit)

    def list_publications(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._page("publications", offset=offset, limit=limit, params={"sort": "pubdate:desc"})

    def list_resources(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._page("resources", offset=offset, limit=limit, params={"sort": "rname:asc"})

    def list_resource_groups(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._page("mediausergr", offset=offset, limit=limit, params={"sort": "ugrname:asc"})

    def list_event_groups(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._page("usergm", offset=offset, limit=limit, params={"sort": "ugrname:asc"})

    def list_available_events(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self._page("availableMeasures", offset=offset, limit=limit)

    @staticmethod
    def _ids(values: Iterable[str] | None, name: str) -> str | None:
        if values is None:
            return None
        cleaned = [str(value).strip() for value in values]
        if len(cleaned) > 50 or any(not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in cleaned):
            raise StaffError("invalid_argument", f"{name} must contain at most 50 simple identifiers")
        return ",".join(cleaned)

    def list_knowledge_base(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        content_type_ids: list[str] | None = None,
        direction_ids: list[str] | None = None,
        tag_ids: list[str] | None = None,
        sort: str = "name:asc",
    ) -> dict[str, Any]:
        allowed_sorts = {
            "name:asc",
            "name:desc",
            "pubdate:asc",
            "pubdate:desc",
            "contenttype:asc",
            "contenttype:desc",
            "passessort:asc",
            "passessort:desc",
        }
        if sort not in allowed_sorts:
            raise StaffError("invalid_argument", "Unsupported knowledge-base sort")
        params: dict[str, Any] = {"sort": sort}
        for key, value in (
            ("contentTypeIds", self._ids(content_type_ids, "content_type_ids")),
            ("directionIds", self._ids(direction_ids, "direction_ids")),
            ("tagIds", self._ids(tag_ids, "tag_ids")),
        ):
            if value:
                params[key] = value
        return self._page("knowledgeBaseList", offset=offset, limit=limit, params=params)

    def list_education_directions(self) -> dict[str, Any]:
        payload, _ = self._get("educationDirections")
        if not isinstance(payload, list):
            raise StaffError("invalid_response", "Expected a list of education directions")
        cleaned = self._bounded(payload[: self.settings.max_items])
        return {"items": cleaned, "returned": len(cleaned), "truncated": len(payload) > len(cleaned)}

    def _portal_response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Origin": "https://staff.mos.ru",
            "Referer": f"{self.settings.base_url.rstrip('/')}/",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise StaffError("network_error", f"Cannot connect to staff.mos.ru: {exc}") from exc
        if response.status_code in {301, 302, 303, 307, 308} or "customloginformaction" in response.text:
            raise StaffError(
                "reauthorize_required",
                "The staff.mos.ru session expired; run dit-staff-mcp --authorize-sudir",
                status=response.status_code,
            )
        if response.is_error:
            message = response.text.strip()[:500] or f"HTTP {response.status_code}"
            raise StaffError("portal_error", message, status=response.status_code)
        return response

    @staticmethod
    def _object_after_marker(text: str, marker: str) -> dict[str, Any]:
        marker_pos = text.find(marker)
        start = text.find("{", marker_pos + len(marker))
        if marker_pos < 0 or start < 0:
            raise StaffError("invalid_response", f"Portal state {marker!r} was not found")
        depth = 0
        quote: str | None = None
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                    except ValueError as exc:
                        raise StaffError("invalid_response", "Portal menu state is not valid JSON") from exc
                    if not isinstance(value, dict):
                        raise StaffError("invalid_response", "Portal menu state has an unexpected shape")
                    return value
        raise StaffError("invalid_response", "Portal menu state is incomplete")

    def _portal_menu(self) -> dict[str, Any]:
        response = self._portal_response("GET", "/")
        return self._object_after_marker(response.text, "menu: ")

    @staticmethod
    def _walk(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from MirapolisClient._walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from MirapolisClient._walk(child)

    def _portal_links(self) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []

        def visit(value: Any, category: str | None = None) -> None:
            if not isinstance(value, dict):
                return
            current_category = category
            if value.get("rtype") == "LabelMenu" and value.get("text"):
                current_category = str(value["text"])
            if value.get("rtype") == "LinkMenu":
                action = value.get("clientAction")
                params = action.get("params") if isinstance(action, dict) else None
                if (
                    isinstance(params, dict)
                    and str(action.get("method", "GET")).upper() == "GET"
                    and params.get("doaction") == "Go"
                    and params.get("type")
                ):
                    links.append(
                        {
                            "key": str(value.get("name", "")),
                            "title": str(value.get("text", value.get("name", ""))),
                            "category": current_category,
                            "params": params,
                        }
                    )
            for child in value.get("items", []):
                visit(child, current_category)

        visit(self._portal_menu())
        return links

    def portal_list_sections(self) -> dict[str, Any]:
        items = [
            {"key": item["key"], "title": item["title"], "category": item["category"]}
            for item in self._portal_links()
        ]
        return {"items": self._bounded(items), "returned": len(items)}

    def _portal_page(self, section_key: str | None = None) -> dict[str, Any]:
        if section_key is None:
            params = {"doaction": "GoToStartPageAction", "type": "startpage", "VIEW_MODE": "standard"}
        else:
            item = next((link for link in self._portal_links() if link["key"] == section_key), None)
            if not item:
                raise StaffError("not_found", "This section is not present in the current user's portal menu")
            params = dict(item["params"])
            params["VIEW_MODE"] = "standard"
        response = self._portal_response("GET", "Do", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise StaffError("invalid_response", "Portal section returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise StaffError("invalid_response", "Portal section has an unexpected shape")
        return payload

    def _portal_clean(self, value: Any, *, key: str = "") -> Any:
        blocked_keys = {
            "s",
            "accesstoken",
            "csrftoken",
            "actions",
            "clientaction",
            "editaction",
            "moveaction",
            "logoutaction",
            "myprofileaction",
            "notificationsaction",
        }
        if key.casefold() in blocked_keys:
            return None
        if isinstance(value, dict):
            if str(value.get("rtype", "")).endswith("ActionClient"):
                return None
            result: dict[str, Any] = {}
            for child_key, child in value.items():
                cleaned = self._portal_clean(child, key=str(child_key))
                if cleaned is not None:
                    result[str(child_key)] = cleaned
            return result
        if isinstance(value, list):
            result = []
            for child in value:
                cleaned = self._portal_clean(child)
                if cleaned is not None:
                    result.append(cleaned)
            return result
        return self._clean(value, key=key)

    def _portal_grid_payload(
        self, grid: dict[str, Any], *, offset: int, limit: int
    ) -> tuple[dict[str, Any], int]:
        bounded_limit = min(max(1, limit), self.settings.max_items, 200)
        body: dict[str, Any] = {
            "MODAL_WINDOW": "false",
            "VIEW_MODE": "standard",
            "doaction": "Grid",
            "type": str(grid.get("typeFrame", "")),
            "gridname": str(grid.get("name", "")),
            "id": str(grid.get("idGrid", "0")),
            "first": str(max(0, offset)),
            "visibleCount": str(bounded_limit),
            "filterWrapperData": "",
            "filters": "",
            "quickFilterIndexes": "",
            "selectedFilters": "",
            "defaultFilter": "",
        }
        for source in (grid.get("gridParams"), grid.get("gridParamsOnce")):
            if isinstance(source, dict):
                for param_key, param_value in source.items():
                    if (
                        str(param_key).casefold()
                        not in {"doaction", "type", "gridname", "id", "first", "visiblecount"}
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(param_key))
                    ):
                        body[str(param_key)] = str(param_value)
        response = self._portal_response("POST", "Do", data=body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise StaffError("invalid_response", "Portal grid returned non-JSON data") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("dataset"), list):
            raise StaffError("invalid_response", "Portal grid has an unexpected shape")
        return payload, bounded_limit

    def _portal_grid(self, grid: dict[str, Any], *, offset: int, limit: int) -> dict[str, Any]:
        payload, bounded_limit = self._portal_grid_payload(grid, offset=offset, limit=limit)
        columns = [
            {"field": column.get("field"), "name": column.get("name"), "type": column.get("type")}
            for column in payload.get("columns", [])
            if isinstance(column, dict) and column.get("field")
        ]
        pager = payload.get("pager") if isinstance(payload.get("pager"), dict) else {}
        dataset = self._bounded(self._portal_clean(payload["dataset"]))
        return {
            "name": grid.get("name"),
            "columns": self._bounded(columns),
            "items": dataset,
            "offset": max(0, offset),
            "limit": bounded_limit,
            "returned": len(dataset),
            "total": pager.get("count"),
        }

    @staticmethod
    def _adaptation_plan_summary(row: dict[str, Any]) -> dict[str, Any]:
        status = row.get("pdstatus") if isinstance(row.get("pdstatus"), dict) else {}
        progress = row.get("pdprogress") if isinstance(row.get("pdprogress"), dict) else {}
        employee_progress = (
            row.get("pdemployeeprogress") if isinstance(row.get("pdemployeeprogress"), dict) else {}
        )
        return {
            "plan_id": row.get("id"),
            "name": row.get("pdname"),
            "kind": row.get("kindidname"),
            "status": status.get("caption") or row.get("statusidname"),
            "completed_stages": progress.get("completedStages"),
            "total_stages": progress.get("totalStages"),
            "employee_completed_stages": employee_progress.get("completedStages"),
            "employee_total_stages": employee_progress.get("totalStages"),
            "start": row.get("pdstartdate"),
            "planned_end": row.get("pdplannedenddate"),
            "completed_at": row.get("pdfactenddate"),
        }

    def _portal_adaptation_plans(self) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        links = {item["key"] for item in self._portal_links()}
        section_key = next((key for key in _ADAPTATION_SECTION_KEYS if key in links), None)
        if not section_key:
            raise StaffError("not_found", "The adaptation section is not present in the current user's portal menu")

        page = self._portal_page(section_key)
        grid = next(
            (
                item
                for item in self._walk(page)
                if item.get("rtype") == "Grid" and item.get("name") == "MyPlansListgrid"
            ),
            None,
        )
        if not isinstance(grid, dict) or grid.get("typeFrame") != "MyPlansList":
            raise StaffError("invalid_response", "The adaptation plans grid was not found")

        payload, _ = self._portal_grid_payload(grid, offset=0, limit=self.settings.max_items)
        plans = [item for item in payload["dataset"] if isinstance(item, dict) and item.get("id")]
        return plans, payload, grid

    def portal_list_my_adaptation_plans(self) -> dict[str, Any]:
        plans, payload, _ = self._portal_adaptation_plans()
        pager = payload.get("pager") if isinstance(payload.get("pager"), dict) else {}
        items = [self._adaptation_plan_summary(plan) for plan in plans]
        return self._bounded(
            {
                "items": items,
                "returned": len(items),
                "total": pager.get("count"),
                "truncated": isinstance(pager.get("count"), int) and pager["count"] > len(items),
            }
        )

    def _portal_open_adaptation_program(
        self, plan_id: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        plans, plans_payload, _ = self._portal_adaptation_plans()
        if not plans:
            raise StaffError("not_found", "No adaptation plans are available to the current user")

        selected: dict[str, Any] | None = None
        if plan_id is not None:
            selected = next((plan for plan in plans if str(plan.get("id")) == plan_id), None)
            if selected is None:
                raise StaffError(
                    "not_found",
                    "plan_id is not present in the current user's adaptation plans",
                )
        elif len(plans) == 1:
            selected = plans[0]
        else:
            raise StaffError(
                "plan_id_required",
                "Several adaptation plans are available; call staff_list_my_adaptation_plans and pass plan_id",
            )

        name_column = next(
            (
                column
                for column in plans_payload.get("columns", [])
                if isinstance(column, dict) and column.get("field") == "pdname"
            ),
            None,
        )
        action = name_column.get("action") if isinstance(name_column, dict) else None
        action_params = action.get("params") if isinstance(action, dict) else None
        if not (
            isinstance(action, dict)
            and action.get("rtype") == "GridGoByAttrActionClient"
            and str(action.get("method", "GET")).upper() == "GET"
            and isinstance(action_params, dict)
            and action_params.get("doaction") == "Go"
            and action_params.get("type") == "PlanDevelopment"
            and action_params.get("attrID") == "id"
        ):
            raise StaffError("invalid_response", "The adaptation plan read action has changed")

        navigation_token = action_params.get("s")
        if not isinstance(navigation_token, str) or not navigation_token:
            raise StaffError("invalid_response", "The adaptation plan navigation token is missing")
        detail_params = {
            "s": navigation_token,
            "attrID": "id",
            "name": "true",
            "doaction": "Go",
            "type": "PlanDevelopment",
            "goByAttrActionName": "Go",
            "id": str(selected["id"]),
            "VIEW_MODE": "standard",
        }
        detail_response = self._portal_response("GET", "Do", params=detail_params)
        try:
            detail = detail_response.json()
        except ValueError as exc:
            raise StaffError("invalid_response", "The adaptation plan returned non-JSON data") from exc
        if not isinstance(detail, dict):
            raise StaffError("invalid_response", "The adaptation plan has an unexpected shape")

        state = next((item for item in self._walk(detail) if item.get("rtype") == "State"), None)
        program_tab = next(
            (
                item
                for item in self._walk(detail)
                if item.get("rtype") == "LoadableTabInfo"
                and item.get("name") == "PlanProgram"
                and item.get("objectName") == "PlanProgram"
            ),
            None,
        )
        local_params = state.get("localParams") if isinstance(state, dict) else None
        if not isinstance(program_tab, dict) or not isinstance(local_params, dict):
            raise StaffError("invalid_response", "The adaptation programme tab was not found")

        program_params: dict[str, Any] = {
            "type": "PlanProgram",
            "id": str(program_tab.get("objId", "")),
            "stype": "nps",
            "doaction": "Go",
            "VIEW_MODE": "standard",
        }
        if not program_params["id"] or not re.fullmatch(r"[A-Za-z0-9_-]+", program_params["id"]):
            raise StaffError("invalid_response", "The adaptation programme identifier is invalid")
        selected_id = str(selected["id"])
        if selected_id != program_params["id"] and not selected_id.startswith(program_params["id"] + "_"):
            raise StaffError("invalid_response", "The adaptation programme identifier does not match the plan")
        for key in _PLAN_PROGRAM_LOCAL_PARAMS:
            value = local_params.get(key)
            if value is not None and len(str(value)) <= 500:
                program_params[key] = str(value)

        program_response = self._portal_response("GET", "Do", params=program_params)
        try:
            program = program_response.json()
        except ValueError as exc:
            raise StaffError("invalid_response", "The adaptation programme returned non-JSON data") from exc
        if not (
            isinstance(program, dict)
            and program.get("rtype") == "Grid"
            and program.get("name") == "PlanProgramgrid"
            and program.get("typeFrame") == "PlanProgram"
        ):
            raise StaffError("invalid_response", "The adaptation stages grid was not found")
        return selected, program

    def portal_list_my_adaptation_stages(
        self, *, plan_id: str | None = None, offset: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        if offset < 0:
            raise StaffError("invalid_argument", "offset must be zero or greater")
        bounded_limit = min(max(1, limit), self.settings.max_items, 200)
        selected, program_grid = self._portal_open_adaptation_program(plan_id)

        rows: list[dict[str, Any]] = []
        raw_offset = 0
        source_total: int | None = None
        while raw_offset < 200:
            payload, _ = self._portal_grid_payload(
                program_grid,
                offset=raw_offset,
                limit=min(self.settings.max_items, 200 - raw_offset),
            )
            batch = [item for item in payload["dataset"] if isinstance(item, dict)]
            rows.extend(batch)
            pager = payload.get("pager") if isinstance(payload.get("pager"), dict) else {}
            try:
                source_total = int(pager.get("count"))
            except (TypeError, ValueError):
                source_total = None
            if not batch or source_total is None or len(rows) >= source_total:
                break
            raw_offset += len(batch)

        stages: list[dict[str, Any]] = []
        section: str | None = None
        for row in rows:
            row_type = str(row.get("type", ""))
            if row_type.casefold() == "stagesection":
                section = str(self._clean(row.get("stname", ""))) or None
                continue
            if row_type.casefold() != "stage":
                continue
            stages.append(
                {
                    "number": len(stages) + 1,
                    "stage_id": row.get("id"),
                    "section": section,
                    "name": self._clean(row.get("stname", "")),
                    "kind": self._clean(row.get("typersname", "")),
                    "status": self._clean(row.get("statusidname", "")),
                    "description": self._clean(row.get("stdesc", "")),
                    "planned_start": row.get("stplannedstarttime"),
                    "planned_end": row.get("stplannedendtime"),
                    "completed_at": row.get("stfactendtime"),
                    "mentor": self._clean(row.get("stadvisorname", "")),
                    "employee_comment": self._clean(row.get("stemployeecomment", "")),
                    "mentor_comment": self._clean(row.get("stadvisorcomment", "")),
                    "mark": self._clean(row.get("mark", "")),
                }
            )

        page = stages[offset : offset + bounded_limit]
        return self._bounded(
            {
                "plan": self._adaptation_plan_summary(selected),
                "items": page,
                "offset": offset,
                "limit": bounded_limit,
                "returned": len(page),
                "total": len(stages),
                "has_more": offset + len(page) < len(stages),
                "source_truncated": source_total is not None and source_total > len(rows),
            }
        )

    def portal_get_profile(self) -> dict[str, Any]:
        page = self._portal_page()
        template = next(
            (
                item
                for item in self._walk(page)
                if item.get("rtype") == "Template" and item.get("name") == "avatar_control"
            ),
            None,
        )
        data = template.get("data") if isinstance(template, dict) else None
        if not isinstance(data, dict) or not data.get("isLogged"):
            raise StaffError("invalid_response", "Authenticated portal profile was not found")
        fields = {
            "user_id": data.get("userId"),
            "first_name": data.get("firstName"),
            "last_name": data.get("lastName"),
            "middle_name": data.get("middleNames"),
            "current_role": self._portal_clean(data.get("currentRole")),
            "current_workplace": self._portal_clean(data.get("currentWorkplace")),
            "locale": self._portal_clean(data.get("currentLocale")),
        }
        return self._bounded(fields)

    def portal_get_home_summary(self) -> dict[str, Any]:
        page = self._portal_page()
        widgets: list[dict[str, Any]] = []
        labels: list[str] = []
        for item in self._walk(page):
            if item.get("rtype") == "Template" and item.get("name") != "avatar_control":
                widgets.append(
                    {
                        "name": item.get("name"),
                        "data": self._portal_clean(item.get("data", {})),
                    }
                )
            elif item.get("rtype") == "HtmlLabel" and item.get("text"):
                labels.append(str(self._clean(item["text"])))
        return self._bounded(
            {
                "display_name": page.get("displayName"),
                "widgets": widgets,
                "labels": labels,
            }
        )

    def portal_read_section(self, section_key: str, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        page = self._portal_page(section_key)
        labels: list[str] = []
        templates: list[dict[str, Any]] = []
        grids: list[dict[str, Any]] = []
        for item in self._walk(page):
            if item.get("rtype") == "HtmlLabel" and item.get("text"):
                labels.append(str(self._clean(item["text"])))
            elif item.get("rtype") == "Template" and item.get("name") != "avatar_control":
                templates.append(
                    {"name": item.get("name"), "data": self._portal_clean(item.get("data", {}))}
                )
            elif item.get("rtype") == "Grid" and item.get("name") and item.get("typeFrame"):
                if len(grids) < 5:
                    grids.append(self._portal_grid(item, offset=offset, limit=limit))
        return self._bounded(
            {
                "section_key": section_key,
                "display_name": page.get("displayName"),
                "labels": labels,
                "templates": templates,
                "grids": grids,
            }
        )

    def portal_list_my_learning(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self.portal_read_section("meTutorMenu", offset=offset, limit=limit)

    def portal_list_my_certificates(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self.portal_read_section("moisertifikaty", offset=offset, limit=limit)

    def probe(self) -> dict[str, Any]:
        if self.settings.portal_cookies:
            profile = self.portal_get_profile()
            auth_mode = "sudir_browser_session"
        else:
            profile = self.get_profile()
            auth_mode = "mirapolis_password"
        return {
            "base_url": self.settings.base_url,
            "login": self.settings.login or "SUDIR",
            "profile": profile,
            "auth_mode": auth_mode,
            "read_only": True,
        }
