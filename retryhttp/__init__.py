from typing import (
    TypeVar,
    Callable,
    Any,
    Tuple,
    Type,
    Union,
    Optional,
    Sequence,
)

import httpx
import tenacity
from tenacity.retry import retry_base
from tenacity.wait import wait_base


F = TypeVar("F", bound=Callable[..., Any])

# Default maximum attempts.
MAX_ATTEMPTS = 3

# Potentially transient HTTP 5xx error statuses to retry.
RETRY_SERVER_ERROR_CODES = (
    httpx.codes.INTERNAL_SERVER_ERROR,
    httpx.codes.BAD_GATEWAY,
    httpx.codes.GATEWAY_TIMEOUT,
    httpx.codes.SERVICE_UNAVAILABLE,
)

# Potentially transient network errors to retry.
# We could just use httpx.NetworkError, but since httpx.CloseError isn't
# usually important to retry, we use these instead.
RETRY_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
)

RETRY_NETWORK_TIMEOUTS = (
    httpx.TimeoutException,  # Includes all network timeouts.
)


def _is_rate_limited(exc: Union[BaseException, None]) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == httpx.codes.TOO_MANY_REQUESTS
    return False


def _is_server_error(
    exc: Optional[BaseException],
    status_codes: Union[Sequence[int], int] = tuple(range(500, 600)),
) -> bool:
    if isinstance(status_codes, int):
        status_codes = [status_codes]
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in status_codes
    return False


class retry_if_rate_limited(retry_base):
    """Retry if rate limited (429 Too Many Requests)."""

    def __call__(self, retry_state: tenacity.RetryCallState) -> bool:
        if retry_state.outcome:
            return _is_rate_limited(retry_state.outcome.exception())
        return False


class retry_if_network_error(retry_base):
    """Retry network errors."""

    def __init__(
        self,
        errors: Union[
            Type[BaseException], Tuple[Type[BaseException], ...], None
        ] = None,
    ) -> None:
        if errors is None:
            errors = RETRY_NETWORK_ERRORS
        if isinstance(errors, BaseException):
            errors = tuple(errors)
        self.errors = errors

    def __call__(self, retry_state: tenacity.RetryCallState) -> bool:
        if retry_state.outcome:
            exc = retry_state.outcome.exception()
            return isinstance(exc, self.errors)
        return False


class retry_if_network_timeout(retry_base):
    """Retry network timeouts."""

    def __init__(
        self,
        timeouts: Union[
            Type[BaseException], Tuple[Type[BaseException], ...], None
        ] = None,
    ) -> None:
        if timeouts is None:
            timeouts = RETRY_NETWORK_TIMEOUTS
        if isinstance(timeouts, BaseException):
            timeouts = tuple(timeouts)
        self.timeouts = timeouts

    def __call__(self, retry_state: tenacity.RetryCallState) -> bool:
        if retry_state.outcome:
            exc = retry_state.outcome.exception()
            return isinstance(exc, self.timeouts)
        return False


class retry_if_server_error(retry_base):
    """Retry certain server errors (5xx).

    Accepts a list or tuple of status codes to retry (5xx only).
    """

    def __init__(
        self,
        server_error_codes: Union[Sequence[int], int, None] = None,
    ) -> None:
        if server_error_codes is None:
            server_error_codes = RETRY_SERVER_ERROR_CODES
        self.server_error_codes = server_error_codes

    def __call__(self, retry_state: tenacity.RetryCallState) -> bool:
        if retry_state.outcome:
            exc = retry_state.outcome.exception()
            return _is_server_error(exc, self.server_error_codes)
        return False


class wait_from_header(wait_base):
    """Wait strategy that derives the value from an HTTP header, if present.

    Fallback is used if header is not present.
    """

    def __init__(
        self,
        header: str,
        fallback: wait_base = tenacity.wait_exponential_jitter(initial=1, max=15),
    ) -> None:
        self.header = header
        self.fallback = fallback

    def __call__(self, retry_state: tenacity.RetryCallState) -> float:
        if retry_state.outcome:
            exc = retry_state.outcome.exception()
            if isinstance(exc, httpx.HTTPStatusError):
                return float(
                    exc.response.headers.get(self.header, self.fallback(retry_state))
                )
        return self.fallback(retry_state)


class wait_retry_after_header(wait_from_header):
    def __init__(
        self,
        header: str = "Retry-After",
        fallback: wait_base = tenacity.wait_exponential_jitter(initial=1, max=15),
    ) -> None:
        super().__init__(header, fallback)


class wait_context_aware(wait_base):
    """Applies different wait strategies based on the type of error."""

    def __init__(
        self,
        wait_server_errors: wait_base = tenacity.wait_exponential_jitter(),
        wait_network_errors: wait_base = tenacity.wait_exponential(),
        wait_network_timeouts: wait_base = tenacity.wait_exponential_jitter(),
        wait_rate_limited: wait_base = wait_retry_after_header(),
        server_error_codes: Union[Sequence[int], int, None] = None,
        network_errors: Union[Tuple[Type[BaseException], ...], None] = None,
        network_timeouts: Union[Tuple[Type[BaseException], ...], None] = None,
    ) -> None:
        if server_error_codes is None:
            server_error_codes = RETRY_SERVER_ERROR_CODES
        if network_errors is None:
            network_errors = RETRY_NETWORK_ERRORS
        if network_timeouts is None:
            network_timeouts = RETRY_NETWORK_TIMEOUTS
        self.wait_server_errors = wait_server_errors
        self.wait_network_errors = wait_network_errors
        self.wait_network_timeouts = wait_network_timeouts
        self.wait_rate_limited = wait_rate_limited
        self.server_error_codes = server_error_codes
        self.network_errors = network_errors
        self.network_timeouts = network_timeouts

    def __call__(self, retry_state: tenacity.RetryCallState) -> float:
        if retry_state.outcome:
            exc = retry_state.outcome.exception()
            if _is_server_error(exc=exc, status_codes=self.server_error_codes):
                return self.wait_server_errors(retry_state)
            if isinstance(exc, self.network_errors):
                return self.wait_network_errors(retry_state)
            if isinstance(exc, self.network_timeouts):
                return self.wait_network_timeouts(retry_state)
            if _is_rate_limited(exc):
                return self.wait_rate_limited(retry_state)
        return 0


def retry_http_errors(
    max_attempt_number: int = 3,
    retry_server_errors: bool = True,
    retry_network_errors: bool = True,
    retry_network_timeouts: bool = True,
    retry_rate_limited: bool = True,
    wait_server_errors: wait_base = tenacity.wait_exponential_jitter(initial=1, max=15),
    wait_network_errors: wait_base = tenacity.wait_exponential(multiplier=1, max=15),
    wait_network_timeouts: wait_base = tenacity.wait_exponential_jitter(
        initial=1, max=15
    ),
    wait_rate_limited: wait_base = wait_retry_after_header(),
    server_error_codes: Union[Sequence[int], int, None] = None,
    network_errors: Union[
        Type[BaseException], Tuple[Type[BaseException], ...], None
    ] = None,
    network_timeouts: Union[
        Type[BaseException], Tuple[Type[BaseException], ...], None
    ] = None,
    *dargs,
    **dkw,
) -> Any:
    """Retry potentially-transient HTTP errors with sensible default behavior.

    Wraps tenacity.retry() with retry, wait, and stop strategies optimized for
    retrying potentially-transient HTTP errors with sensible defaults, which are
    all configurable.

    """
    if server_error_codes is None:
        server_error_codes = RETRY_SERVER_ERROR_CODES
    if network_errors is None:
        network_errors = RETRY_NETWORK_ERRORS
    if network_timeouts is None:
        network_timeouts = RETRY_NETWORK_TIMEOUTS

    retry_strategies = []
    if retry_server_errors:
        retry_strategies.append(
            retry_if_server_error(server_error_codes=server_error_codes)
        )
    if retry_network_errors:
        retry_strategies.append(retry_if_network_error(errors=network_errors))
    if retry_network_timeouts:
        retry_strategies.append(retry_if_network_timeout(timeouts=network_timeouts))
    if retry_rate_limited:
        retry_strategies.append(retry_if_rate_limited())

    retry = dkw.get("retry") or tenacity.retry_any(*retry_strategies)

    # We don't need to conditionally build our wait strategy since each strategy
    # will only apply if the corresponding retry strategy is in use.
    wait = dkw.get("wait") or wait_context_aware(
        wait_server_errors=wait_server_errors,
        wait_network_errors=wait_network_errors,
        wait_network_timeouts=wait_network_timeouts,
        wait_rate_limited=wait_rate_limited,
    )

    stop = dkw.get("stop") or tenacity.stop_after_attempt(max_attempt_number)

    def decorator(func: F) -> F:
        return tenacity.retry(retry=retry, wait=wait, stop=stop, *dargs, **dkw)(func)

    return decorator


__all__ = [
    "retry_http_errors",
]