"""Hard spending cap for any LLM client.

Wraps the provider client (inside the replay layer, so replayed responses cost nothing).
Before each real call it counts the request's input tokens (free endpoint), projects the
call at its maximum output, and refuses it if spent + projection would pass the limit.
"""

from __future__ import annotations

from collections.abc import Callable

from poi.extract.llm.base import LLMClient, LLMRequest, LLMResponse, Usage


class BudgetExceededError(RuntimeError):
    """The next call could take spending past the cap; nothing was sent."""


class BudgetGuard:
    def __init__(
        self, inner: LLMClient, limit_usd: float, price: Callable[[str, Usage], float]
    ) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.limit_usd = limit_usd
        self.price = price
        self.spent_usd = 0.0

    def extract(self, request: LLMRequest) -> LLMResponse:
        counted = self.inner.count_tokens(request)
        projected = self.price(
            request.model, Usage(input_tokens=counted, output_tokens=request.max_output_tokens)
        )
        if self.spent_usd + projected > self.limit_usd:
            raise BudgetExceededError(
                f"stopped before {request.key}: spent ${self.spent_usd:.4f}, next call up to "
                f"${projected:.4f}, cap ${self.limit_usd:.2f}"
            )
        response = self.inner.extract(request)
        self.spent_usd += self.price(request.model, response.usage)
        return response

    def count_tokens(self, request: LLMRequest) -> int:
        return self.inner.count_tokens(request)
