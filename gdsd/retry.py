"""Retentativas com espera exponencial + jitter e limitador de taxa da API."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import requests

from .errors import Interrupted, TransientError

log = logging.getLogger(__name__)
T = TypeVar("T")

NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionResetError,
    TimeoutError,
)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (TransientError, *NETWORK_ERRORS))


@dataclass
class RetryPolicy:
    """Política de retentativa. ``sleep`` é injetável para testes."""

    max_retries: int = 6
    base: float = 1.0
    cap: float = 64.0
    sleep: Callable[[float, threading.Event | None], None] | None = None
    rng: random.Random = field(default_factory=random.Random)

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Espera para a tentativa ``attempt`` (1, 2, 3...): exponencial com
        "full jitter", nunca menor que ``Retry-After`` quando informado."""
        exp = min(self.cap, self.base * (2 ** (attempt - 1)))
        value = self.rng.uniform(exp / 2, exp)
        if retry_after is not None:
            value = max(value, min(retry_after, 3600.0))
        return value

    def wait(self, seconds: float, stop_event: threading.Event | None) -> None:
        if self.sleep is not None:
            self.sleep(seconds, stop_event)
            return
        if stop_event is not None:
            if stop_event.wait(seconds):
                raise Interrupted("Operação interrompida.")
        else:
            time.sleep(seconds)


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    stop_event: threading.Event | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    description: str = "requisição",
) -> T:
    """Executa ``fn`` repetindo em erros temporários (429, 5xx, rede)."""
    attempt = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            raise Interrupted("Operação interrompida.")
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - filtrado abaixo
            if not is_transient(exc) or attempt >= policy.max_retries:
                if is_transient(exc) and not isinstance(exc, TransientError):
                    raise TransientError(
                        f"Falha de rede persistente em {description} após "
                        f"{attempt + 1} tentativa(s): {type(exc).__name__}"
                    ) from exc
                raise
            attempt += 1
            retry_after = getattr(exc, "retry_after", None)
            delay = policy.delay(attempt, retry_after)
            log.warning(
                "erro temporário; nova tentativa",
                extra={"ctx": {"what": description, "attempt": attempt, "delay": round(delay, 2),
                               "error": type(exc).__name__, "status": getattr(exc, "status", None)}},
            )
            if on_retry:
                on_retry(attempt, delay, exc)
            policy.wait(delay, stop_event)


class RateLimiter:
    """Token bucket compartilhado entre threads para respeitar as cotas da API."""

    def __init__(self, rate: float, burst: int | None = None, clock: Callable[[], float] = time.monotonic):
        self.rate = rate
        self.capacity = float(burst or max(1, int(rate)))
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                needed = (1 - self._tokens) / self.rate
            time.sleep(needed)
