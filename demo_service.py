"""
DemoService — intentionally generates realistic failures and writes real logs via logging.

These code paths execute on the host process; log lines land in app.log and are consumed
by the triage pipeline (tail + Gemini). This is not a string mock: exceptions run here.
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Literal, Any

# Logger name appears in log configuration trees; messages include DemoService for auto-triage.
logger = logging.getLogger("demo_service")

ScenarioKind = Literal["timeout", "api_failure", "retry_exhaustion", "random"]

DEFAULT_RUNBOOKS: dict[str, str] = {
    "timeout": (
        "If DemoService reports upstream timeouts, increase max_retries in timeout_handler "
        "from 3 to 5 and consider raising the per-attempt timeout budget."
    ),
    "api_failure": (
        "If payment API returns non-200, enable circuit breaker backoff and verify API key "
        "rotation; add structured error logging before failing the request."
    ),
    "retry_exhaustion": (
        "When retries are exhausted, escalate severity and increase max_attempts or fix "
        "the root cause of repeated 503 responses from the dependency."
    ),
}

# --- Circuit Breaker and Mock Response Definitions ---
class MockPaymentResponse:
    """A mock response object for simulating API calls."""
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("No JSON body available")
        return self._body

    def text(self) -> str:
        return str(self._body) if self._body is not None else ""

class InvalidResponse:
    """A mock object to simulate an invalid API response (e.g., missing status_code)."""
    pass

# Circuit Breaker state variables
CIRCUIT_BREAKER_STATE: Literal["CLOSED", "OPEN", "HALF_OPEN"] = "CLOSED"
FAILURE_COUNT: int = 0
LAST_FAILURE_TIME: float = 0.0
FAILURE_THRESHOLD: int = 3
RESET_TIMEOUT_BASE: float = 5.0  # Initial time circuit stays OPEN
BACKOFF_FACTOR: float = 2.0      # Multiplier for reset timeout
CURRENT_RESET_TIMEOUT: float = RESET_TIMEOUT_BASE # Current calculated reset timeout for OPEN state

def _call_payments_internal() -> MockPaymentResponse | Any | None:
    """
    Simulates a call to payments.internal, returning a mock response,
    an invalid object, or None, based on a weighted random choice.
    """
    # Simulate various failure modes
    choice = random.choices(
        ["success", "none_response", "invalid_response", "4xx", "5xx"],
        weights=[0.6, 0.05, 0.05, 0.15, 0.15],
        k=1
    )[0]

    if choice == "success":
        return MockPaymentResponse(200, {"status": "success", "transaction_id": f"txn-{random.randint(1000, 9999)}"})
    elif choice == "none_response":
        logger.warning("DemoService payments.internal returned a None response object.")
        return None
    elif choice == "invalid_response":
        logger.warning("DemoService payments.internal returned an invalid response object (missing status_code).")
        return InvalidResponse() # Return an object without status_code
    elif choice == "4xx":
        status = random.choice([400, 401, 403, 404])
        return MockPaymentResponse(status, {"error": "Client error", "code": status, "message": "Bad request data"})
    elif choice == "5xx":
        status = random.choice([500, 502, 503, 504])
        return MockPaymentResponse(status, {"error": "Server error", "code": status, "message": "Upstream service unavailable"})
    return None # Should not be reached
# --- End Circuit Breaker and Mock Response Definitions ---


def _fake_upstream_call(should_timeout: bool) -> None:
    """Simulated I/O: either succeeds quickly or blocks past the budget."""
    if should_timeout:
        time.sleep(0.05)
        raise TimeoutError("DemoService upstream auth call timed out after 30.0s (correlation_id=demo-upstream-1)")
    time.sleep(0.01)


def timeout_handler(max_retries: int = 3, per_attempt_timeout: bool = True) -> None:
    """
    Real retry loop with logging at each step. Final failure emits ERROR with stack trace.
    """
    logger.info("DemoService timeout_handler starting max_retries=%s", max_retries)
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        logger.info("DemoService outbound attempt %s/%s", attempt, max_retries)
        try:
            _fake_upstream_call(should_timeout=per_attempt_timeout)
            logger.info("DemoService attempt %s succeeded", attempt)
            return
        except TimeoutError as e:
            last_exc = e
            logger.warning(
                "DemoService attempt %s/%s failed: %s — will retry",
                attempt,
                max_retries,
                e,
            )
    logger.error(
        "DemoService ERROR: all %s retries exhausted for upstream auth: %s",
        max_retries,
        last_exc,
        exc_info=last_exc is not None,
    )
    if last_exc:
        raise last_exc


def simulate_payment_api_failure() -> None:
    """
    Simulates a downstream HTTP-style failure with robust error handling,
    circuit breaker, and enhanced logging for payments.internal.
    """
    global CIRCUIT_BREAKER_STATE, FAILURE_COUNT, LAST_FAILURE_TIME, CURRENT_RESET_TIMEOUT

    logger.info("DemoService payment charge started order_id=demo-order-42")

    # Circuit Breaker Logic
    if CIRCUIT_BREAKER_STATE == "OPEN":
        elapsed_time = time.time() - LAST_FAILURE_TIME
        if elapsed_time < CURRENT_RESET_TIMEOUT:
            logger.warning(
                "DemoService payments.internal circuit is OPEN. Skipping call. "
                "Will retry after %.2f seconds (%.2f elapsed). Current backoff: %.2f s",
                CURRENT_RESET_TIMEOUT, elapsed_time, CURRENT_RESET_TIMEOUT
            )
            raise ConnectionError(
                f"DemoService payments.internal circuit is OPEN. "
                f"Skipping call for {CURRENT_RESET_TIMEOUT:.2f}s due to previous failures."
            )
        else:
            logger.info(
                "DemoService payments.internal circuit is HALF_OPEN. "
                "Attempting a single request to check health."
            )
            CIRCUIT_BREAKER_STATE = "HALF_OPEN"

    response = None
    try:
        # Simulate latency
        time.sleep(random.uniform(0.05, 0.4))
        logger.warning("DemoService payment API latency p99 elevated (420ms)")

        response = _call_payments_internal()

        # Robust error handling for response object: check for None or invalid structure
        if response is None:
            raise ValueError("Received None response object from payments.internal")
        if not hasattr(response, 'status_code'):
            raise ValueError(f"Invalid response object from payments.internal: missing status_code attribute. Object type: {type(response)}")

        if 200 <= response.status_code < 300:
            logger.info(
                "DemoService payments.internal call succeeded (status=%s). "
                "Resetting circuit breaker.",
                response.status_code
            )
            # Reset circuit breaker on success
            CIRCUIT_BREAKER_STATE = "CLOSED"
            FAILURE_COUNT = 0
            CURRENT_RESET_TIMEOUT = RESET_TIMEOUT_BASE # Reset backoff delay
            return # Success path

        # Non-200 response handling: enhance error logging
        error_message = f"DemoService payments.internal returned non-2xx status: {response.status_code}"
        response_details = {}
        try:
            response_details = response.json()
            log_message = f"{error_message}. Full response: status={response.status_code}, body={response_details}"
        except (AttributeError, ValueError): # response.json() might not exist or fail
            response_text = response.text() if hasattr(response, 'text') else "N/A"
            log_message = f"{error_message}. Full response (text): status={response.status_code}, body={response_text}"

        logger.error(log_message, exc_info=False) # No stack trace for expected API errors
        raise ConnectionError(error_message)

    except (ValueError, ConnectionError, TimeoutError) as e:
        # This block handles simulated network errors, invalid response objects, and non-2xx statuses
        logger.error("DemoService payment pipeline failed: %s", e, exc_info=True)

        # Circuit Breaker failure logic
        FAILURE_COUNT += 1
        if CIRCUIT_BREAKER_STATE == "HALF_OPEN":
            logger.warning(
                "DemoService payments.internal failed in HALF_OPEN state. "
                "Re-opening circuit breaker and increasing backoff."
            )
            CIRCUIT_BREAKER_STATE = "OPEN"
            LAST_FAILURE_TIME = time.time()
            CURRENT_RESET_TIMEOUT *= BACKOFF_FACTOR # Increase backoff
        elif FAILURE_COUNT >= FAILURE_THRESHOLD and CIRCUIT_BREAKER_STATE == "CLOSED":
            logger.error(
                "DemoService payments.internal reached %s consecutive failures. "
                "Opening circuit breaker for %.2f seconds.",
                FAILURE_THRESHOLD, CURRENT_RESET_TIMEOUT
            )
            CIRCUIT_BREAKER_STATE = "OPEN"
            LAST_FAILURE_TIME = time.time()
            # CURRENT_RESET_TIMEOUT retains its value (RESET_TIMEOUT_BASE or increased from previous OPEN state)
        else:
            logger.warning(
                "DemoService payments.internal failed (%s/%s failures). "
                "Circuit breaker remains %s.",
                FAILURE_COUNT, FAILURE_THRESHOLD, CIRCUIT_BREAKER_STATE
            )

        raise e # Re-raise the original exception to propagate failure

    except Exception as e:
        # Catch any other unexpected errors
        logger.critical("DemoService payment pipeline encountered unexpected error: %s", e, exc_info=True)
        # Treat as a failure for circuit breaker
        FAILURE_COUNT += 1
        if CIRCUIT_BREAKER_STATE == "HALF_OPEN":
            CIRCUIT_BREAKER_STATE = "OPEN"
            LAST_FAILURE_TIME = time.time()
            CURRENT_RESET_TIMEOUT *= BACKOFF_FACTOR
        elif FAILURE_COUNT >= FAILURE_THRESHOLD and CIRCUIT_BREAKER_STATE == "CLOSED":
            CIRCUIT_BREAKER_STATE = "OPEN"
            LAST_FAILURE_TIME = time.time()
            # CURRENT_RESET_TIMEOUT retains its value
        raise e


def simulate_retry_exhaustion_on_503() -> None:
    """Multiple 503s from a dependency, then final ERROR after exhausting backoff."""
    logger.info("DemoService inventory sync job started")
    attempts = 4
    for i in range(1, attempts + 1):
        logger.warning(
            "DemoService inventory dependency returned 503 (attempt %s/%s)",
            i,
            attempts,
        )
        time.sleep(0.02)
    logger.error(
        "DemoService ERROR: retry budget exhausted after %s consecutive 503 responses from inventory.internal",
        attempts,
    )


def run_failure_scenario(kind: ScenarioKind | str = "random") -> tuple[str, str]:
    """
    Execute one failure scenario. Logs are written as a side effect of real control flow.

    Returns (alert_text, resolved_kind) so callers can attach the right runbook.
    """
    resolved = kind
    if resolved == "random":
        resolved = random.choice(["timeout", "api_failure", "retry_exhaustion"])

    if resolved == "timeout":
        try:
            timeout_handler(max_retries=3, per_attempt_timeout=True)
        except TimeoutError:
            return (
                "DemoService upstream timeout after retries (auth dependency)",
                "timeout",
            )
        return "DemoService timeout scenario completed without error", "timeout"

    if resolved == "api_failure":
        try:
            simulate_payment_api_failure()
        except ConnectionError:
            return (
                "DemoService payment API failure (502 from payments.internal)",
                "api_failure",
            )
        except ValueError: # Catch ValueError for invalid response objects
            return (
                "DemoService payment API failure (invalid response from payments.internal)",
                "api_failure",
            )
        return "DemoService API scenario completed without error", "api_failure"

    if resolved == "retry_exhaustion":
        simulate_retry_exhaustion_on_503()
        return (
            "DemoService inventory sync failed — 503 retry exhaustion",
            "retry_exhaustion",
        )

    raise ValueError(f"Unknown failure scenario: {kind!r}")


def get_source_for_patch_context() -> str:
    """Return current on-disk source of this module for patch generation context."""
    return Path(__file__).read_text(encoding="utf-8")