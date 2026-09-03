from __future__ import annotations

import json
from typing import NamedTuple


DEFAULT_CREDENTIAL_TARGET = "DIT-Agent/Staff"
SUDIR_SESSION_TARGET = "DIT-Agent/Staff/SUDIR"


class StoredCredential(NamedTuple):
    login: str
    password: str


class CredentialStoreError(RuntimeError):
    pass


def _error_code(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def _win32cred():
    try:
        import win32cred
    except ImportError as exc:
        raise CredentialStoreError(
            "Windows Credential Manager support is unavailable; use DIT_STAFF_LOGIN and DIT_STAFF_PASSWORD"
        ) from exc
    return win32cred


def load_credential(target: str = DEFAULT_CREDENTIAL_TARGET) -> StoredCredential | None:
    win32cred = _win32cred()
    try:
        value = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception as exc:
        if _error_code(exc) == 1168:  # ERROR_NOT_FOUND
            return None
        raise CredentialStoreError(f"Cannot read Windows Credential Manager target {target!r}") from exc
    blob = value.get("CredentialBlob", b"")
    if isinstance(blob, bytes):
        try:
            password = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            password = blob.decode("utf-8")
    else:
        password = str(blob)
    return StoredCredential(str(value.get("UserName", "")).strip(), password)


def save_credential(login: str, password: str, target: str = DEFAULT_CREDENTIAL_TARGET) -> None:
    win32cred = _win32cred()
    try:
        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": target,
                "UserName": login,
                "CredentialBlob": password,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "Comment": "DIT Staff MCP credentials for staff.mos.ru",
            },
            0,
        )
    except Exception as exc:
        raise CredentialStoreError(f"Cannot write Windows Credential Manager target {target!r}") from exc


def delete_credential(target: str = DEFAULT_CREDENTIAL_TARGET) -> bool:
    win32cred = _win32cred()
    try:
        win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        return True
    except Exception as exc:
        if _error_code(exc) == 1168:
            return False
        raise CredentialStoreError(f"Cannot delete Windows Credential Manager target {target!r}") from exc


def load_portal_cookies(target: str = SUDIR_SESSION_TARGET) -> tuple[dict[str, str], ...]:
    stored = load_credential(target)
    if not stored:
        return ()
    try:
        payload = json.loads(stored.password)
    except (TypeError, ValueError) as exc:
        raise CredentialStoreError("Saved staff.mos.ru browser session is invalid; authorize again") from exc
    if not isinstance(payload, list):
        raise CredentialStoreError("Saved staff.mos.ru browser session is invalid; authorize again")
    result: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        value = str(item.get("value", ""))
        domain = str(item.get("domain", "")).lstrip(".").casefold()
        path = str(item.get("path", "/"))
        if name and value and domain == "staff.mos.ru" and "\r" not in value and "\n" not in value:
            result.append({"name": name, "value": value, "domain": domain, "path": path})
    return tuple(result)


def save_portal_cookies(cookies: tuple[dict[str, str], ...], target: str = SUDIR_SESSION_TARGET) -> None:
    payload = json.dumps(list(cookies), ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-16-le")) > 2400:
        raise CredentialStoreError("The staff.mos.ru session is too large for Windows Credential Manager")
    save_credential("SUDIR browser session", payload, target)
