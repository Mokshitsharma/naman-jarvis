"""Reading and pressing buttons inside other apps' windows (Windows UI Automation).

Searches use UIA's native FindFirst, which runs inside the target app: ~50 ms for a
page with 2,000 elements, where walking the tree from Python takes seconds. Chromium
apps (Chrome, web apps, Spotify) only build their accessibility tree after the first
request, so callers poll with `wait_for` rather than giving up on the first miss.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any, TypeVar

T = TypeVar("T")

NAME, CONTROL_TYPE = 30005, 30003  # UIA property ids
BUTTON, DATA_ITEM, EDIT = 50000, 50029, 50004  # UIA control type ids
DESCENDANTS, CHILDREN = 4, 2  # TreeScope


@lru_cache(maxsize=1)
def _uia() -> Any:
    import uiautomation.uiautomation as um

    return um._AutomationClient.instance().IUIAutomation


def element(hwnd: int) -> Any:
    return _uia().ElementFromHandle(hwnd)


def find(root: Any, name: str | None = None, control: int | None = None, scope: int = DESCENDANTS) -> Any:
    """First element under `root` with this exact name and/or control type, else None."""
    uia = _uia()
    conds = []
    if name is not None:
        conds.append(uia.CreatePropertyCondition(NAME, name))
    if control is not None:
        conds.append(uia.CreatePropertyCondition(CONTROL_TYPE, control))
    cond = conds[0] if len(conds) == 1 else uia.CreateAndCondition(*conds)
    try:
        return root.FindFirst(scope, cond) or None
    except Exception:  # the element went away mid-search (page changed)
        return None


def children_names(root: Any) -> list[str]:
    try:
        kids = root.FindAll(CHILDREN, _uia().CreateTrueCondition())
        return [kids.GetElement(i).CurrentName or "" for i in range(kids.Length)]
    except Exception:
        return []


def name_of(el: Any) -> str:
    try:
        return el.CurrentName or ""
    except Exception:
        return ""


def invoke(el: Any) -> None:
    import uiautomation as auto

    auto.Control.CreateControlFromElement(el).GetInvokePattern().Invoke()


def focused_name() -> str:
    try:
        return name_of(_uia().GetFocusedElement())
    except Exception:
        return ""


def value_of(el: Any) -> str:
    import uiautomation as auto

    try:
        return auto.Control.CreateControlFromElement(el).GetValuePattern().Value or ""
    except Exception:
        return ""


def wait_for(probe: Callable[[], T], timeout: float, every: float = 0.1) -> T | None:
    """Call `probe` until it returns something truthy or `timeout` seconds pass."""
    end = time.monotonic() + timeout
    while True:
        result = probe()
        if result:
            return result
        if time.monotonic() >= end:
            return None
        time.sleep(every)
