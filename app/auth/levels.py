from contextvars import ContextVar
from typing import Literal

Level = Literal["employee", "manager"]
LEVELS: tuple[Level, ...] = ("employee", "manager")

# Unset means no transport vouched for anyone, so it fails closed to the narrower view.
viewer_level: ContextVar[Level] = ContextVar("viewer_level", default="employee")


def visible_levels() -> list[str]:
    """ The `required_level` values the current viewer may read: a manager reads everything."""
    return list(LEVELS) if viewer_level.get() == "manager" else ["employee"]
