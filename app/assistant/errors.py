"""Безопасные диагностические коды внешнего провайдера."""


class ProviderError(RuntimeError):
    def __init__(self, message, *, code="provider_error", retryable=False, retry_after=0):
        super().__init__(message)
        self.code, self.retryable, self.retry_after = code, retryable, retry_after
