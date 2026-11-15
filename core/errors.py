"""Exception types shared by the core layer.

Each one maps to a distinct failure the UI reports differently, so the runner
can tell "the token is wrong" apart from "this one feed failed" without
matching on message text.
"""


class ConfigError(Exception):
    """Configuration or credentials are missing or invalid."""


class TokenError(Exception):
    """HTTP 401/403 — the token is invalid or lacks permission."""


class GosError(Exception):
    """The Global Online Status file could not be obtained or parsed."""


class ExcelError(Exception):
    """The Excel report could not be written."""


class Cancelled(Exception):
    """Raised at a cooperative cancellation point."""
