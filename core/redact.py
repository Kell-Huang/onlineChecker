"""Token redaction.

Every string that can reach a log file, a chart tooltip or the Excel report
goes through ``redact_token`` first. The Productsup token is a bearer
credential; leaking it into a shared report is the one mistake that cannot be
undone by rerunning.
"""
import re

_TOKEN_HEADER_RE = re.compile(r"(X-Auth-Token\s*:\s*)\S+", re.IGNORECASE)
_TOKEN_QUERY_RE = re.compile(r"(\btoken=)[^&\s]+", re.IGNORECASE)


def redact_token(text) -> str:
    """Replace any auth token occurrence with '***'."""
    if text is None:
        return ""
    value = str(text)
    value = _TOKEN_HEADER_RE.sub(r"\1***", value)
    value = _TOKEN_QUERY_RE.sub(r"\1***", value)
    return value
