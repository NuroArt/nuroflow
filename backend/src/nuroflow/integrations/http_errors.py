"""One error-classification policy for every outbound integration.

| Outcome                       | Retryable | Why                                            |
|-------------------------------|-----------|------------------------------------------------|
| timeout / connection error    | yes       | network blips heal                             |
| 408, 409, 425, 429            | yes       | provider asked us to slow down or try again    |
| 5xx, Anthropic 529 overloaded | yes       | provider-side incident                         |
| other 4xx (401, 400, 404 …)   | **no**    | bad key / bad request: retrying burns money    |
"""

from __future__ import annotations

import httpx

RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= 500


def error_detail(response: httpx.Response, *, limit: int = 300) -> str:
    """Best-effort provider error message, never the request (which may hold secrets)."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:limit]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and "message" in err:
            return str(err["message"])[:limit]
        if "description" in body:  # Telegram
            return str(body["description"])[:limit]
    return str(body)[:limit]
