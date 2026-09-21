"""
wad.py
------
Minimal WinAppDriver client, stdlib only.

Why this exists instead of Appium-Python-Client
===============================================
WinAppDriver (1.2.x, last released 2021) speaks the *old* JSON Wire Protocol.
It reads its capabilities from a top-level ``desiredCapabilities`` object and
expects the keys unprefixed (``app``, ``appTopLevelWindow``, ...).

Appium-Python-Client 6.x on Selenium 4.x only ever sends W3C capabilities, and
``AppiumOptions`` vendor-prefixes anything non-standard. So ``app: "Root"``
arrives at WinAppDriver as ``appium:app``, which it does not look at, and the
session request dies with:

    WebDriverException: Bad capabilities. Specify either app or
    appTopLevelWindow to create a session

There is no combination of AppiumOptions flags that avoids the prefixing, so
this module talks to WinAppDriver directly over HTTP instead. It is ~200 lines
and removes the whole Appium/Selenium version-compatibility surface.

Usage
-----
    import wad

    for w in wad.desktop_windows():
        print(w)

    with wad.attach(handle=0x1b0cc0) as s:
        el = s.find("id", "lbxColumnTypes")
        print(s.attr(el, "Name"))
"""

from __future__ import annotations

import ctypes
import json
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Iterator, Optional

DEFAULT_URL = "http://127.0.0.1:4723"

# WinAppDriver returns JSONWP element refs ({"ELEMENT": id}) but newer builds
# may also include the W3C key, so accept either.
_W3C_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"

# Locator strategies WinAppDriver understands, mapped to the wire value.
#
# CAREFUL: in WinAppDriver the wire strategy "id" means **RuntimeId**, not
# AutomationId -- AutomationId is "accessibility id". Passing an AutomationId
# with using="id" does not error, it just never matches, which looks exactly
# like the element being absent. So "id" here is deliberately mapped to
# "accessibility id" (what callers actually mean); use "runtime id" for the
# genuine RuntimeId lookup.
STRATEGIES = {
    "xpath": "xpath",
    "id": "accessibility id",          # AutomationId - the common case
    "accessibility id": "accessibility id",
    "runtime id": "id",                # WinAppDriver's RuntimeId
    "name": "name",
    "class": "class name",
    "class name": "class name",
    "tag": "tag name",
    "tag name": "tag name",
}


class WadError(RuntimeError):
    """A WinAppDriver request failed."""


class ElementNotFound(WadError):
    """No element matched the locator."""


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


def _request(method: str, url: str, payload: Optional[dict], timeout: float) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        _raise_from_body(raw, f"{method} {url}")
        raise WadError(f"{method} {url} -> HTTP {exc.code}: {raw[:400]}") from exc
    except urllib.error.URLError as exc:
        raise WadError(
            f"Could not reach WinAppDriver at {url} ({exc.reason}). "
            "Is WinAppDriver.exe running and listening on 127.0.0.1:4723?"
        ) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    # JSONWP signals failure with a non-zero "status" even on HTTP 200.
    if isinstance(parsed, dict) and parsed.get("status", 0) not in (0, None):
        _raise_from_body(raw, f"{method} {url}")
    return parsed


def _raise_from_body(raw: str, ctx: str) -> None:
    """Turn a JSONWP error body into the right exception type."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return
    status = parsed.get("status")
    value = parsed.get("value")
    message = value.get("message") if isinstance(value, dict) else str(value)
    # 7 = NoSuchElement, 11 = ElementNotVisible in the JSON Wire Protocol.
    if status == 7:
        raise ElementNotFound(f"{ctx}: {message}")
    raise WadError(f"{ctx}: [status {status}] {message}")


def _element_id(ref: dict) -> str:
    eid = ref.get("ELEMENT") or ref.get(_W3C_ELEMENT_KEY)
    if not eid:
        raise WadError(f"Unrecognised element reference: {ref!r}")
    return eid


# --------------------------------------------------------------------------
# Process helper (used to identify which windows belong to UNICORN)
# --------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_name(pid: int) -> Optional[str]:
    """Return the executable name for `pid`, or None if it can't be read."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        k32.CloseHandle(handle)
    return None


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class Session:
    """A live WinAppDriver session. Use via `attach()` / `root()`."""

    def __init__(self, base_url: str, session_id: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout

    # -- lifecycle ------------------------------------------------------
    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        """End the session.

        For a session created with `appTopLevelWindow` this detaches only --
        it does not close the application window.
        """
        if not self.session_id:
            return
        try:
            self._call("DELETE", "")
        except WadError:
            pass  # session may already be gone; nothing useful to do
        finally:
            self.session_id = ""

    # -- request helpers ------------------------------------------------
    def _call(self, method: str, path: str, payload: Optional[dict] = None,
              timeout: Optional[float] = None) -> Any:
        url = f"{self.base_url}/session/{self.session_id}{path}"
        return _request(method, url, payload, timeout or self.timeout)

    def _value(self, method: str, path: str, payload: Optional[dict] = None,
               timeout: Optional[float] = None) -> Any:
        result = self._call(method, path, payload, timeout)
        return result.get("value") if isinstance(result, dict) else result

    def set_implicit_wait(self, ms: int) -> None:
        """How long WinAppDriver retries a failing element lookup."""
        self._call("POST", "/timeouts/implicit_wait", {"ms": ms})

    # -- finding --------------------------------------------------------
    def find_all(self, strategy: str, value: str, within: Optional[str] = None) -> list[str]:
        """Return element ids matching the locator (empty list if none)."""
        using = STRATEGIES.get(strategy.lower())
        if using is None:
            raise ValueError(f"Unknown strategy {strategy!r}; use one of {sorted(STRATEGIES)}")
        path = f"/element/{within}/elements" if within else "/elements"
        try:
            refs = self._value("POST", path, {"using": using, "value": value})
        except ElementNotFound:
            return []
        return [_element_id(r) for r in refs] if isinstance(refs, list) else []

    def find(self, strategy: str, value: str, within: Optional[str] = None) -> str:
        """Return the single element id matching the locator, else raise."""
        using = STRATEGIES.get(strategy.lower())
        if using is None:
            raise ValueError(f"Unknown strategy {strategy!r}; use one of {sorted(STRATEGIES)}")
        path = f"/element/{within}/element" if within else "/element"
        ref = self._value("POST", path, {"using": using, "value": value})
        if not isinstance(ref, dict):
            raise ElementNotFound(f"No element for {strategy}={value!r}")
        return _element_id(ref)

    # -- reading --------------------------------------------------------
    def attr(self, element: str, name: str) -> Optional[str]:
        return self._value("GET", f"/element/{element}/attribute/{name}")

    def tag(self, element: str) -> Optional[str]:
        return self._value("GET", f"/element/{element}/name")

    def text(self, element: str) -> Optional[str]:
        return self._value("GET", f"/element/{element}/text")

    def is_enabled(self, element: str) -> bool:
        return bool(self._value("GET", f"/element/{element}/enabled"))

    def is_displayed(self, element: str) -> bool:
        return bool(self._value("GET", f"/element/{element}/displayed"))

    def rect(self, element: str) -> dict:
        return self._value("GET", f"/element/{element}/rect") or {}

    def page_source(self, timeout: float = 180.0) -> str:
        """Full UIA tree of this session's window as XML.

        Far cheaper than walking the tree element by element: one request
        instead of several thousand.
        """
        return self._value("GET", "/source", timeout=timeout) or ""

    # -- acting ---------------------------------------------------------
    def click(self, element: str) -> None:
        self._call("POST", f"/element/{element}/click", {})

    def send_keys(self, element: str, keys: str) -> None:
        self._call("POST", f"/element/{element}/value", {"value": [keys]})

    def clear(self, element: str) -> None:
        self._call("POST", f"/element/{element}/clear", {})


# --------------------------------------------------------------------------
# Session factories
# --------------------------------------------------------------------------


def status(base_url: str = DEFAULT_URL, timeout: float = 10.0) -> dict:
    """Ping WinAppDriver. Raises WadError if it isn't reachable."""
    result = _request("GET", f"{base_url.rstrip('/')}/status", None, timeout)
    return result if isinstance(result, dict) else {"raw": result}


def _new_session(caps: dict, base_url: str, timeout: float) -> Session:
    # Note the *unprefixed* keys inside desiredCapabilities -- this is the
    # whole reason this module exists. See the module docstring.
    payload = {"desiredCapabilities": {"platformName": "Windows", **caps}}
    result = _request("POST", f"{base_url.rstrip('/')}/session", payload, timeout)
    session_id = None
    if isinstance(result, dict):
        session_id = result.get("sessionId") or (
            result.get("value", {}).get("sessionId")
            if isinstance(result.get("value"), dict) else None
        )
    if not session_id:
        raise WadError(f"WinAppDriver did not return a sessionId: {str(result)[:400]}")
    return Session(base_url, session_id, timeout)


def root(base_url: str = DEFAULT_URL, timeout: float = 60.0) -> Session:
    """Session attached to the desktop, for enumerating top-level windows."""
    return _new_session({"app": "Root"}, base_url, timeout)


def attach(handle: int, base_url: str = DEFAULT_URL, timeout: float = 180.0) -> Session:
    """Attach to an existing top-level window by its native window handle.

    WinAppDriver wants `appTopLevelWindow` as a hex string, so a decimal
    handle read from UIA's NativeWindowHandle must be converted.
    """
    return _new_session({"appTopLevelWindow": hex(int(handle))}, base_url, timeout)


# --------------------------------------------------------------------------
# Window discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowInfo:
    title: str
    handle: int
    pid: int
    process: Optional[str]
    class_name: str

    def __str__(self) -> str:
        return (f"{self.title!r} [handle=0x{self.handle:x} pid={self.pid} "
                f"proc={self.process} class={self.class_name!r}]")


def desktop_windows(base_url: str = DEFAULT_URL) -> list[WindowInfo]:
    """List top-level windows currently on the desktop.

    Uses xpath `/*/*` -- the desktop root is `/*`, so its children are the
    top-level windows. (`/*` on its own only returns "Desktop 1".)
    """
    windows: list[WindowInfo] = []
    with root(base_url) as session:
        for eid in session.find_all("xpath", "/*/*"):
            try:
                handle = session.attr(eid, "NativeWindowHandle")
                if not handle or handle == "0":
                    continue
                pid = int(session.attr(eid, "ProcessId") or 0)
                windows.append(WindowInfo(
                    title=session.attr(eid, "Name") or "",
                    handle=int(handle),
                    pid=pid,
                    process=process_name(pid),
                    class_name=session.attr(eid, "ClassName") or "",
                ))
            except WadError:
                continue  # window closed mid-enumeration
    return windows


def windows_of_process(name: str, base_url: str = DEFAULT_URL) -> list[WindowInfo]:
    """Top-level windows belonging to a process, e.g. "UNICORNClient.exe"."""
    wanted = name.lower().removesuffix(".exe")
    return [
        w for w in desktop_windows(base_url)
        if w.process and w.process.lower().removesuffix(".exe") == wanted
    ]


# --------------------------------------------------------------------------
# XML tree helpers (for use with Session.page_source)
# --------------------------------------------------------------------------


def parse_source(xml_source: str) -> Any:
    """Parse a page_source string into an ElementTree root node."""
    import xml.etree.ElementTree as ET

    # page_source declares encoding="utf-16" but hands us a str; ET refuses
    # that combination, so strip the declaration before parsing.
    if xml_source.startswith("<?xml"):
        xml_source = xml_source[xml_source.index("?>") + 2:]
    return ET.fromstring(xml_source)


def iter_tree_from(node: Any, depth: int = 0) -> Iterator[tuple[int, Any]]:
    """Yield (depth, element) for `node` and all its descendants."""
    yield depth, node
    for child in node:
        yield from iter_tree_from(child, depth + 1)


def iter_tree(xml_source: str) -> Iterator[tuple[int, Any]]:
    """Yield (depth, element) for every node in a page_source XML tree."""
    yield from iter_tree_from(parse_source(xml_source))


def describe(node: Any) -> str:
    """One-line summary of a page_source node."""
    name = node.get("Name") or ""
    aid = node.get("AutomationId") or ""
    parts = [node.tag]
    if name:
        parts.append(f"Name={name!r}")
    if aid:
        parts.append(f"AutomationId={aid!r}")
    if node.get("IsEnabled") == "False":
        parts.append("DISABLED")
    if node.get("IsOffscreen") == "True":
        parts.append("OFFSCREEN")
    return " | ".join(parts)
