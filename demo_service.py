"""
DemoService — intentionally generates realistic failures and writes real logs via logging.

These code paths execute on the host process; log lines land in app.log and are consumed
by the triage pipeline (tail + Gemini). This is not a string mock: exceptions run here.
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Literal

# Logger name appears in log configuration trees; messages include DemoService for auto-triage.
logger = logging.getLogger("demo_service")

# --- New classes for the fix ---
class MockPaymentResponse:
    """
    A mock object to simulate an HTTP response from payments.internal,
    allowing for various status codes, headers, and body content (or lack thereof).
    """
    def __init__(self, status_code: int, headers: dict, body: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self._text = text

    def json(self) -> dict | None:
        """
        Attempts to return the response body as a JSON dictionary.
        Raises ValueError if the body is not JSON or not available.
        """
        if self._body is not None:
            return self._body
        if self._text is not None:
            try:
                return json.loads(self._text)
            except json.JSONDecodeError:
                raise ValueError("Response text is not valid JSON")
        raise ValueError("No JSON body available")

    @property
    def text(self) -> str:
        """Returns the raw text content of the response."""
        if self._text is not None:
            return self._text
        if self._body is not None:
            try:
                return json.dumps(self._body)
            except TypeError:
                return str(self._body) # Fallback for non-serializable body
        return ""

class CircuitBreaker:
    """
    A simplified circuit breaker implementation to prevent cascading failures
    to the payments.internal API.
    """
    def __init__(self, failure_threshold: int = 3, reset_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.last_failure_time = 0
        self.is_open = False

    def _check_state(self) -> None:
        """
        Checks if the circuit should transition from OPEN to HALF-OPEN (or CLOSED).
        In this simplified version, after `reset_timeout`, it moves directly to CLOSED
        to allow the next request to test the upstream service.
        """
        if self.is_open and (time.time() - self.last_failure_time > self.reset_timeout):
            logger.info("DemoService Circuit Breaker: Reset timeout reached. Moving to HALF-OPEN/CLOSED state.")
            self.is_open = False # Allow next request to test
            self.failures = 0 # Reset failure count

    def record_success(self) -> None:
        """Records a successful call, closing the circuit if it was open or half-open."""
        if self.is_open:
            logger.info("DemoService Circuit Breaker: Success in HALF-OPEN state. Closing circuit.")
        self.is_open = False
        self.failures = 0
        self.last_failure_time = 0

    def record_failure(self) -> None:
        """Records a failed call, potentially opening the circuit."""
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            if not self.is_open:
                logger.error(
                    "DemoService Circuit Breaker: Failure threshold reached (%s). Opening circuit.",
                    self.failure_threshold
                )
            self.is_open = True

    def allow_request(self) -> bool:
        """Checks if a request is allowed by the circuit breaker."""
        self._check_state()
        if self.is_open:
            logger.warning("DemoService Circuit Breaker: Circuit is OPEN. Preventing request.")
            return False
        return True

# Global instance for the payment API circuit breaker
payment_api_circuit_breaker = CircuitBreaker()

# --- End new classes ---

ScenarioKind = Literal["timeout", "api_failure", "retry_exhaustion", "random"]
PaymentApiScenario = Literal[
    "success",
    "400_bad_request",
    "500_internal_error",
    "malformed_json",
    "no_response",
    "null_data",
    "circuit_breaker_open" # Scenario to explicitly test the breaker
]

DEFAULT_RUNBOOKS: dict[str, str] = {
    "timeout": (
        "If DemoService reports upstream timeouts, increase max_retries in timeout_handler "
        "from 3 to 5 and consider raising the per-attempt timeout budget."
    ),
    "api_failure": ( # This entry is still here, but the scenario will map to 'payment_api_error'
        "If payment API returns non-200, enable circuit breaker backoff and verify API key "
        "rotation; add structured error logging before failing the request."
    ),
    "retry_exhaustion": (
        "When retries are exhausted, escalate severity and increase max_attempts or fix "
        "the root cause of repeated 503 responses from the dependency."
    ),
    "payment_api_error": ( # New runbook entry for comprehensive payment API issues
        "Investigate payments.internal API for non-200 responses, malformed data, or null critical fields. "
        "Check structured logs for full response details. If circuit breaker is open, wait for reset or manual intervention."
    ),
}


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


def _fake_payment_api_call(scenario: PaymentApiScenario) -> MockPaymentResponse | None:
    """
    Simulates a call to payments.internal with various outcomes based on the scenario.
    """
    time.sleep(0.02) # Simulate some network latency

    if scenario == "success":
        return MockPaymentResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body={"status": "success", "transaction_id": "txn_12345", "amount": 1000}
        )
    elif scenario == "400_bad_request":
        return MockPaymentResponse(
            status_code=400,
            headers={"Content-Type": "application/json"},
            body={"error": "Invalid amount", "code": "INVALID_AMOUNT"}
        )
    elif scenario == "500_internal_error":
        return MockPaymentResponse(
            status_code=500,
            headers={"Content-Type": "application/json"},
            body={"error": "Internal server error", "code": "SERVER_ERROR"}
        )
    elif scenario == "malformed_json":
        # Simulate a response that's not valid JSON, even if status is 200
        return MockPaymentResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text="<html>Error: Malformed JSON response</html>" # Not JSON
        )
    elif scenario == "no_response":
        return None # Simulate connection drop or upstream timeout before response
    elif scenario == "null_data":
        # Simulate a 200 response but with critical data fields missing or null
        return MockPaymentResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body={"status": "success", "transaction_id": None, "amount": 1000} # transaction_id is None
        )
    elif scenario == "circuit_breaker_open":
        # This scenario is handled by the caller (call_payment_api) directly
        # by checking the circuit breaker state before making the call.
        # This branch should ideally not be reached if the breaker is open.
        return None
    else:
        raise ValueError(f"Unknown payment API scenario: {scenario}")


def call_payment_api(scenario: PaymentApiScenario = "success") -> dict:
    """
    Calls the simulated payments.internal API with robust error handling,
    explicit null checks, structured logging, and circuit breaker integration.

    Raises ConnectionError for network-level issues or non-200 responses.
    Raises ValueError for malformed responses or missing critical data.
    """
    logger.info("DemoService payment charge started order_id=demo-order-42 with scenario=%s", scenario)

    # --- Circuit Breaker Check ---
    if not payment_api_circuit_breaker.allow_request():
        logger.error(
            "DemoService payment pipeline failed: Circuit breaker is OPEN for payments.internal. "
            "Preventing request to avoid cascading failures."
        )
        raise ConnectionError("DemoService payment API call prevented by circuit breaker.")

    response: MockPaymentResponse | None = None
    try:
        response = _fake_payment_api_call(scenario)

        # --- Explicit null checks and robust error handling ---
        if response is None:
            payment_api_circuit_breaker.record_failure()
            logger.error(
                "DemoService payment pipeline failed: No response received from payments.internal. "
                "Possible connection error or upstream timeout."
            )
            raise ConnectionError("DemoService payment API returned no response.")

        if response.status_code != 200:
            payment_api_circuit_breaker.record_failure()
            # Structured logging for non-200 responses
            logger.error(
                "DemoService payment pipeline failed: payments.internal returned non-200 status. "
                "status_code=%s, headers=%s, body=%s",
                response.status_code,
                response.headers,
                response.text,
                extra={
                    "payment_api_response": {
                        "status_code": response.status_code,
                        "headers": response.headers,
                        "body": response.text,
                    }
                }
            )
            raise ConnectionError(
                f"DemoService payment API returned {response.status_code} from payments.internal"
            )

        # Attempt to parse JSON and handle malformed responses
        response_data: dict | None = None
        try:
            response_data = response.json()
        except ValueError as e: # Catches "No JSON body available" or "Response text is not valid JSON"
            payment_api_circuit_breaker.record_failure()
            logger.error(
                "DemoService payment pipeline failed: Malformed JSON response from payments.internal. "
                "Error: %s, status_code=%s, headers=%s, raw_body=%s",
                e,
                response.status_code,
                response.headers,
                response.text,
                exc_info=True, # Include stack trace for parsing errors
                extra={
                    "payment_api_response": {
                        "status_code": response.status_code,
                        "headers": response.headers,
                        "body": response.text,
                        "error_parsing_json": str(e),
                    }
                }
            )
            raise ValueError("Malformed JSON response from payments.internal") from e

        if response_data is None:
            payment_api_circuit_breaker.record_failure()
            logger.error(
                "DemoService payment pipeline failed: JSON response body is None from payments.internal. "
                "status_code=%s, headers=%s, raw_body=%s",
                response.status_code,
                response.headers,
                response.text,
                extra={
                    "payment_api_response": {
                        "status_code": response.status_code,
                        "headers": response.headers,
                        "body": response.text,
                    }
                }
            )
            raise ValueError("Empty JSON response from payments.internal")

        # Explicit null checks for critical data fields
        transaction_id = response_data.get("transaction_id")
        status = response_data.get("status")

        if transaction_id is None or status is None:
            payment_api_circuit_breaker.record_failure()
            logger.error(
                "DemoService payment pipeline failed: Critical data (transaction_id or status) is missing or null in response. "
                "status_code=%s, headers=%s, parsed_body=%s",
                response.status_code,
                response.headers,
                response_data,
                extra={
                    "payment_api_response": {
                        "status_code": response.status_code,
                        "headers": response.headers,
                        "body": response_data,
                        "missing_fields": [f for f in ["transaction_id", "status"] if response_data.get(f) is None],
                    }
                }
            )
            raise ValueError("Missing critical data in payment API response.")

        if status != "success":
            payment_api_circuit_breaker.record_failure()
            logger.error(
                "DemoService payment pipeline failed: Payment status is not 'success'. "
                "status_code=%s, headers=%s, parsed_body=%s",
                response.status_code,
                response.headers,
                response_data,
                extra={
                    "payment_api_response": {
                        "status_code": response.status_code,
                        "headers": response.headers,
                        "body": response_data,
                    }
                }
            )
            raise ValueError(f"Payment not successful: {status}")

        payment_api_circuit_breaker.record_success()
        logger.info(
            "DemoService payment charge succeeded: transaction_id=%s, status=%s",
            transaction_id,
            status,
            extra={"transaction_id": transaction_id, "payment_status": status}
        )
        return response_data

    except Exception as e:
        # Catch any other unexpected errors during processing that weren't caught above.
        # ConnectionError and ValueError are already handled and logged specifically.
        if not isinstance(e, (ConnectionError, ValueError)):
            payment_api_circuit_breaker.record_failure()
            logger.error(
                "DemoService payment pipeline failed with unexpected error: %s",
                e,
                exc_info=True,
                extra={
                    "payment_api_response": {
                        "status_code": response.status_code if response else "N/A",
                        "headers": response.headers if response else "N/A",
                        "body": response.text if response else "N/A",
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                    }
                }
            )
        raise # Re-raise the exception after logging and recording failure


def simulate_payment_api_failure() -> None:
    """
    Simulates a downstream HTTP-style failure without real network I/O.
    This function now acts as a wrapper to call `call_payment_api` with a specific failure scenario.
    """
    logger.info("DemoService payment API failure scenario triggered (order_id=demo-order-42)")
    try:
        # For the 'api_failure' scenario, let's simulate a 500 Internal Server Error.
        call_payment_api(scenario="500_internal_error")
    except (ConnectionError, ValueError) as e:
        # The call_payment_api already logs the error with structured details.
        # Re-raise to propagate the failure up to run_failure_scenario.
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
            # This scenario now triggers a 500 error, which is handled by call_payment_api
            # and then re-raised as a ConnectionError or ValueError.
            simulate_payment_api_failure()
        except (ConnectionError, ValueError): # Catch both potential exceptions from call_payment_api
            return (
                "DemoService payment API failure (non-200, malformed, or null data from payments.internal)",
                "payment_api_error", # Use the new runbook entry
            )
        # This line should ideally not be reached if simulate_payment_api_failure is called with a failure scenario.
        return "DemoService API scenario completed without error", "payment_api_error"

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