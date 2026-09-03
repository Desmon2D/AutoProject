from __future__ import annotations

import hashlib
import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

import httpx

from .config import Settings


_SPACE_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|authorization|password|passwd|session|cookie)\b\s*[:=]\s*[^\s,;]+"
)
_UNSAFE_LINK_RE = re.compile(
    r"(?i)(logout|signout|delete|remove|create|edit|update|approve|reject|submit|upload|download|export)"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|access.?token|refresh.?token|authorization|cookie|secret|sessionid|fingerprint)"
)
_STRUCTURE_NODE_ID_RE = re.compile(r"(?i)^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class CfcError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        return result


def _clean_text(value: str, limit: int) -> str:
    value = _SPACE_RE.sub(" ", value).strip()
    value = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value[:limit]


class _PageParser(HTMLParser):
    _BLOCKED = {"script", "style", "noscript", "svg", "template", "form"}
    _TEXT_TAGS = {"title", "h1", "h2", "h3", "h4", "p", "li", "dt", "dd", "td", "th", "label", "a"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self._tag_stack: list[str] = []
        self._text_parts: list[str] = []
        self._title_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        self._tag_stack.append(tag)
        if tag in self._BLOCKED:
            self._blocked_depth += 1
        if tag == "a" and self._blocked_depth == 0:
            self._anchor_href = dict(attrs).get("href")
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchor_href:
            label = _clean_text(" ".join(self._anchor_text), 300)
            self.links.append((label, self._anchor_href))
            self._anchor_href = None
            self._anchor_text = []
        if tag in self._BLOCKED and self._blocked_depth:
            self._blocked_depth -= 1
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._blocked_depth or not self._tag_stack:
            return
        tag = self._tag_stack[-1]
        if tag in self._TEXT_TAGS:
            value = data.strip()
            if value:
                self._text_parts.append(value)
                if tag == "title":
                    self._title_parts.append(value)
                if self._anchor_href is not None:
                    self._anchor_text.append(value)

    @property
    def title(self) -> str:
        return _clean_text(" ".join(self._title_parts), 500)

    @property
    def text(self) -> str:
        return _clean_text("\n".join(self._text_parts), 1_000_000)


class CfcClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._origin = "https://cfc.mos.ru"
        verify: bool | str = settings.ca_bundle or True
        self._http = httpx.Client(
            headers={
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
            },
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            verify=verify,
            proxy=settings.proxy,
            trust_env=settings.trust_env,
        )
        for cookie in settings.portal_cookies:
            self._http.cookies.set(
                cookie["name"], cookie["value"], domain=cookie.get("domain", "cfc.mos.ru"), path=cookie.get("path", "/")
            )
        self._sections: dict[str, dict[str, str]] = {}
        self._instance_id = ""

    def close(self) -> None:
        self._http.close()

    def _assert_same_origin(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "cfc.mos.ru" or parsed.port not in (None, 443):
            raise CfcError("unsafe_redirect", "CFC redirected outside the allowed portal origin")
        return urlunparse(("https", "cfc.mos.ru", parsed.path or "/", "", parsed.query, ""))

    def _get(self, url: str) -> httpx.Response:
        url = self._assert_same_origin(url)
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            raise CfcError("network_error", f"Cannot connect to cfc.mos.ru: {exc}") from exc
        final_url = str(response.url)
        if urlparse(final_url).hostname != "cfc.mos.ru":
            raise CfcError("authentication_required", "CFC session expired; run dit-cfc-mcp --authorize")
        if response.status_code in (401, 403):
            raise CfcError(
                "access_denied",
                "cfc.mos.ru denied access; verify the direct route and renew browser authorization",
                status=response.status_code,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CfcError("http_error", f"cfc.mos.ru returned HTTP {response.status_code}", status=response.status_code) from exc
        return response

    def _parse_page(self, response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").casefold()
        if "json" in content_type:
            try:
                payload = response.json()
            except ValueError as exc:
                raise CfcError("invalid_response", "cfc.mos.ru returned invalid JSON") from exc
            text = _clean_text(json.dumps(payload, ensure_ascii=False), self.settings.max_text_chars)
            return {"url": str(response.url), "title": "", "text": text, "links": []}
        parser = _PageParser()
        parser.feed(response.text)
        links = self._safe_links(str(response.url), parser.links)
        return {
            "url": str(response.url),
            "title": parser.title,
            "text": parser.text[: self.settings.max_text_chars],
            "links": links,
        }

    def _api_post(self, method: str, parameters: list[dict[str, Any]]) -> Any:
        allowed = {
            "getUser_v2",
            "getHomePage",
            "getPageProps2",
            "getFuncStructureStaff",
            "getFuncStructureTree3",
        }
        if method not in allowed:
            raise CfcError("unsafe_method", "The requested CFC API method is not in the read-only allowlist")
        path = f"/{method}"
        body = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time() * 1000)
        signature_source = f"{timestamp}{path.replace('/', '')}{body}turret"
        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "X-CFC-Timestamp": str(timestamp),
            "X-CFC-Token": hashlib.md5(signature_source.encode("utf-8")).hexdigest(),
        }
        if self._instance_id:
            headers["X-CFC-Instance-ID"] = self._instance_id
        url = f"{self._origin}/proxyapi/hs/proxyapi{path}"
        try:
            response = self._http.post(url, content=body.encode("utf-8"), headers=headers)
        except httpx.HTTPError as exc:
            raise CfcError("network_error", f"Cannot connect to the CFC API: {exc}") from exc
        if response.status_code in (401, 403):
            raise CfcError(
                "authentication_required",
                "CFC session is absent or expired; run dit-cfc-mcp --authorize",
                status=response.status_code,
            )
        try:
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise CfcError("http_error", f"CFC API returned HTTP {response.status_code}", status=response.status_code) from exc
        except ValueError as exc:
            raise CfcError("invalid_response", "CFC API returned invalid JSON") from exc
        if isinstance(payload, dict):
            instance_id = payload.get("InstanceID")
            if isinstance(instance_id, str):
                self._instance_id = instance_id
        return payload

    def _user_payload(self) -> dict[str, Any]:
        payload = self._api_post(
            "getUser_v2",
            [
                {
                    "Screen": {"Width": 1920, "Height": 1080},
                    "Viewport": {"Width": 1280, "Height": 720},
                    "Browser": {"Name": "Microsoft Edge", "Version": "140"},
                    "AuthParams": None,
                    "AuthErrorCode": "",
                }
            ],
        )
        if not isinstance(payload, dict) or not payload.get("Authorized"):
            raise CfcError("authentication_required", "CFC browser session is not authorized; run dit-cfc-mcp --authorize")
        return payload

    def _sanitize(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 8:
            return "[maximum nesting depth reached]"
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in list(value.items())[: self.settings.max_links]:
                name = str(key)
                if _SENSITIVE_KEY_RE.search(name):
                    continue
                result[name] = self._sanitize(item, depth=depth + 1)
            return result
        if isinstance(value, list):
            return [self._sanitize(item, depth=depth + 1) for item in value[: self.settings.max_links]]
        if isinstance(value, str):
            return _clean_text(value, self.settings.max_text_chars)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return _clean_text(str(value), self.settings.max_text_chars)

    def get_my_profile(self) -> dict[str, Any]:
        payload = self._user_payload()
        fields = (
            "Username",
            "UserID",
            "Organization",
            "Subdivision",
            "Position",
            "PortalName",
            "Profiles",
        )
        return self._sanitize({key: payload.get(key) for key in fields if key in payload})

    def _refresh_sections(self) -> list[dict[str, Any]]:
        payload = self._user_payload()
        tree = payload.get("ComponentTree")
        if not isinstance(tree, list):
            return []
        self._sections.clear()
        result: list[dict[str, Any]] = []

        def visit(nodes: list[Any], parent: str | None = None) -> None:
            for node in nodes:
                if len(result) >= self.settings.max_links or not isinstance(node, dict):
                    continue
                signature = str(node.get("Signature", "")).strip()
                url = str(node.get("URL", "")).strip()
                title = _clean_text(str(node.get("Title", "")), 300)
                active = bool(node.get("Active", True))
                if signature and active and not _UNSAFE_LINK_RE.search(f"{signature} {url} {title}"):
                    key = hashlib.sha256(f"{signature}\0{url}".encode("utf-8")).hexdigest()[:16]
                    section = {
                        "section_key": key,
                        "signature": signature,
                        "title": title or signature,
                        "url": url,
                        "parent": parent,
                        "is_home_page": bool(node.get("IsHomePage")),
                    }
                    result.append(section)
                    self._sections[key] = {field: str(section[field]) for field in ("signature", "title", "url")}
                    self._sections[key]["is_home_page"] = "1" if section["is_home_page"] else "0"
                    next_parent = key
                else:
                    next_parent = parent
                children = node.get("Children")
                if isinstance(children, list):
                    visit(children, next_parent)

        visit(tree)
        return result

    def _safe_links(self, page_url: str, links: list[tuple[str, str]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for label, raw_href in links:
            if len(result) >= self.settings.max_links:
                break
            absolute = urljoin(page_url, raw_href)
            parsed = urlparse(absolute)
            if parsed.scheme != "https" or parsed.hostname != "cfc.mos.ru" or parsed.port not in (None, 443):
                continue
            clean = urlunparse(("https", "cfc.mos.ru", parsed.path or "/", "", parsed.query, ""))
            query_keys = " ".join(key for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
            if _UNSAFE_LINK_RE.search(f"{parsed.path} {query_keys} {label}"):
                continue
            if clean in seen:
                continue
            seen.add(clean)
            key = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
            item = {"section_key": key, "label": label or parsed.path or "/", "path": parsed.path or "/"}
            result.append(item)
            self._sections[key] = {"url": clean, "label": item["label"]}
        return result

    def get_home_summary(self) -> dict[str, Any]:
        self._user_payload()
        return self._sanitize(self._api_post("getHomePage", []))

    def list_sections(self) -> dict[str, Any]:
        items = self._refresh_sections()
        return {"items": items, "count": len(items)}

    def read_section(self, section_key: str) -> dict[str, Any]:
        self._refresh_sections()
        item = self._sections.get(section_key)
        if item is None:
            raise CfcError("unknown_section", "Use an exact section_key returned by cfc_list_sections")
        if item["is_home_page"] == "1":
            payload = self._api_post("getHomePage", [])
        else:
            try:
                payload = self._api_post(
                    "getPageProps2",
                    [{"Signature": item["signature"], "Url": item["url"]}],
                )
            except CfcError as exc:
                if exc.status == 404:
                    hint = ""
                    if item["signature"].startswith("cfc.hr"):
                        hint = " Use cfc_search_employees and cfc_get_employee_structure for employee data."
                    raise CfcError(
                        "unsupported_section",
                        f"This CFC section has no generic read endpoint.{hint}",
                        status=404,
                    ) from exc
                raise
        return {
            "section_key": section_key,
            "signature": item["signature"],
            "title": item["title"],
            "url": item["url"],
            "data": self._sanitize(payload),
        }

    def search_employees(self, query: str, limit: int = 10) -> dict[str, Any]:
        query = _clean_text(str(query), 200)
        if len(query) < 2:
            raise CfcError("invalid_query", "Employee search requires at least two characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise CfcError("invalid_limit", "Employee search limit must be between 1 and 20")
        self._user_payload()
        payload = self._api_post(
            "getFuncStructureStaff",
            [{"SearchString": query, "Count": limit}],
        )
        if not isinstance(payload, list):
            raise CfcError("invalid_response", "CFC employee search returned an unexpected response")
        items: list[dict[str, str]] = []
        for raw in payload[:limit]:
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("ID", "")).strip()
            name = _clean_text(str(raw.get("Value", "")), 300)
            if _STRUCTURE_NODE_ID_RE.fullmatch(node_id) and name:
                items.append({"structure_node_id": node_id.casefold(), "name": name})
        return {"query": query, "items": items, "count": len(items)}

    @staticmethod
    def _structure_person(node: dict[str, Any]) -> dict[str, Any]:
        owner = node.get("Owner")
        if not isinstance(owner, dict):
            owner = {}
        person: dict[str, Any] = {
            "structure_node_id": str(node.get("ID", "")),
            "employee_id": str(owner.get("ID", "")),
            "name": _clean_text(str(owner.get("FIO", "")), 300),
            "role": _clean_text(str(owner.get("Role") or owner.get("Position") or ""), 500),
            "is_vacant": bool(node.get("IsVacant")),
        }
        company = owner.get("Company")
        if isinstance(company, dict):
            company_name = _clean_text(str(company.get("Title") or company.get("Name") or ""), 300)
            if company_name:
                person["company"] = company_name
        return person

    def get_employee_structure(
        self,
        structure_node_id: str,
        max_depth: int = 1,
        max_results: int = 100,
    ) -> dict[str, Any]:
        structure_node_id = str(structure_node_id).strip().casefold()
        if not _STRUCTURE_NODE_ID_RE.fullmatch(structure_node_id):
            raise CfcError(
                "invalid_structure_node_id",
                "Use a structure_node_id returned by cfc_search_employees",
            )
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= 5:
            raise CfcError("invalid_depth", "max_depth must be between 1 and 5")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 200:
            raise CfcError("invalid_limit", "max_results must be between 1 and 200")
        self._user_payload()
        payload = self._api_post("getFuncStructureTree3", [])
        if not isinstance(payload, list):
            raise CfcError("invalid_response", "CFC employee structure returned an unexpected response")

        root: dict[str, Any] | None = None

        def find(nodes: list[Any]) -> dict[str, Any] | None:
            for raw in nodes:
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("ID", "")).casefold() == structure_node_id:
                    return raw
                children = raw.get("Children")
                if isinstance(children, list):
                    match = find(children)
                    if match is not None:
                        return match
            return None

        root = find(payload)
        if root is None:
            raise CfcError(
                "employee_not_found",
                "The employee structure node was not found; run cfc_search_employees again",
            )

        descendants: list[dict[str, Any]] = []
        truncated = False

        def collect(nodes: list[Any], parent_id: str, relative_depth: int) -> None:
            nonlocal truncated
            if relative_depth > max_depth:
                return
            for raw in nodes:
                if not isinstance(raw, dict):
                    continue
                if len(descendants) >= max_results:
                    truncated = True
                    return
                person = self._structure_person(raw)
                person["parent_structure_node_id"] = parent_id
                person["relative_depth"] = relative_depth
                descendants.append(person)
                children = raw.get("Children")
                if isinstance(children, list):
                    collect(children, person["structure_node_id"], relative_depth + 1)
                if truncated:
                    return

        children = root.get("Children")
        if isinstance(children, list):
            collect(children, structure_node_id, 1)
        return {
            "employee": self._structure_person(root),
            "subordinates": descendants,
            "count": len(descendants),
            "max_depth": max_depth,
            "truncated": truncated,
        }

    def probe(self) -> dict[str, Any]:
        profile = self.get_my_profile()
        sections = self._refresh_sections()
        return {
            "url": self.settings.base_url,
            "username": profile.get("Username", ""),
            "portal_name": profile.get("PortalName", ""),
            "section_count": len(sections),
        }
