"""
Tool-call error handling.

Firestore's client library already retries transient errors (DeadlineExceeded,
ServiceUnavailable, InternalServerError, ResourceExhausted) internally, with
exponential backoff — see google.cloud.firestore_v1.services.firestore.
transports.base's per-method default_retry config (get_document, run_query,
commit, etc. all have default_retry configured). This module handles what's
left after those retries are exhausted, or for non-retryable errors (e.g.
PermissionDenied, malformed documents): turning an unhandled exception into a
graceful, structured response instead of letting it propagate as a raw
traceback into ADK's function-calling layer.
"""

import functools
import logging

from google.api_core import exceptions as google_exceptions

logger = logging.getLogger(__name__)

# Firestore's own retries are already exhausted by the time this fires, so
# this message reflects a genuinely unavailable dependency, not a fixable
# client-side issue.
_SERVICE_UNAVAILABLE_MESSAGE = "I'm having trouble accessing that information right now. Please try again in a moment."
_UNEXPECTED_ERROR_MESSAGE = "Something went wrong while processing that request. Please try again."


def tool_error_handler(func):
    """Wrap a tool function so any unhandled exception becomes a graceful
    ``{"status": "error", "message": ...}`` response instead of an unhandled
    exception reaching ADK's function-calling layer.

    Apply this as the OUTERMOST decorator (listed first) so it also catches
    exceptions raised by any inner decorator, e.g. ``requires_order_ownership``'s
    own Firestore fetch:

        @tool_error_handler
        @requires_order_ownership
        def track_order(...): ...
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except google_exceptions.GoogleAPICallError as e:
            logger.error("%s: Firestore call failed after retries: %s", func.__name__, e)
            return {"status": "error", "message": _SERVICE_UNAVAILABLE_MESSAGE}
        except Exception as e:
            logger.error("%s: unexpected error: %s", func.__name__, e, exc_info=True)
            return {"status": "error", "message": _UNEXPECTED_ERROR_MESSAGE}

    return wrapper
