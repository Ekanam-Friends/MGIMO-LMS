"""Ошибки внешних портов, независимые от HTTP-клиента и конкретной модели."""


class ProviderError(RuntimeError):
    pass
