from __future__ import annotations

import json


CFC_SESSION_TARGET = "DIT-Agent/CFC/Session"


class CredentialStoreError(RuntimeError):
    pass


def _win32cred():
    try:
        import win32cred
    except ImportError as exc:
        raise CredentialStoreError("Windows Credential Manager support is unavailable") from exc
    return win32cred


def _error_code(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def load_portal_cookies(target: str = CFC_SESSION_TARGET) -> tuple[dict[str, str], ...]:
    win32cred = _win32cred()
    try:
        stored = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception as exc:
        if _error_code(exc) == 1168:
            return ()
        raise CredentialStoreError(f"Cannot read Windows Credential Manager target {target!r}") from exc
    blob = stored.get("CredentialBlob", b"")
    if isinstance(blob, bytes):
        try:
            payload = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            payload = blob.decode("utf-8")
    else:
        payload = str(blob)
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise CredentialStoreError("Saved cfc.mos.ru browser session is invalid; authorize again") from exc
    if not isinstance(raw, list):
        raise CredentialStoreError("Saved cfc.mos.ru browser session is invalid; authorize again")
    cookies: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        value = str(item.get("value", ""))
        domain = str(item.get("domain", "")).lstrip(".").casefold()
        path = str(item.get("path", "/"))
        if name and value and domain == "cfc.mos.ru" and "\r" not in value and "\n" not in value:
            cookies.append({"name": name, "value": value, "domain": domain, "path": path})
    return tuple(cookies)


def save_portal_cookies(cookies: tuple[dict[str, str], ...], target: str = CFC_SESSION_TARGET) -> None:
    win32cred = _win32cred()
    payload = json.dumps(list(cookies), ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-16-le")) > 2400:
        raise CredentialStoreError("The cfc.mos.ru session is too large for Windows Credential Manager")
    try:
        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": target,
                "UserName": "CFC browser session",
                "CredentialBlob": payload,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "Comment": "DIT CFC MCP session for cfc.mos.ru",
            },
            0,
        )
    except Exception as exc:
        raise CredentialStoreError(f"Cannot write Windows Credential Manager target {target!r}") from exc


def delete_portal_cookies(target: str = CFC_SESSION_TARGET) -> bool:
    win32cred = _win32cred()
    try:
        win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        return True
    except Exception as exc:
        if _error_code(exc) == 1168:
            return False
        raise CredentialStoreError(f"Cannot delete Windows Credential Manager target {target!r}") from exc
