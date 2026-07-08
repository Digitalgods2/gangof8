"""Task Intake — validates the incoming request and opens a session."""

from __future__ import annotations

from typing import Optional

from .models import Budgets, Session
from .sessions import SessionManager


def receive(text: str, source: str, manager: SessionManager, budgets: Optional[Budgets] = None) -> Session:
    text = (text or "").strip()
    if not text:
        raise ValueError("task text is empty")
    return manager.create(text, source=source, budgets=budgets)
