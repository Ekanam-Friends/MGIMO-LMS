"""Исключение сертификата ограничено конкретным хостом и не следует редиректам."""
import httpx
import pytest

from app.schedule.ruz_client import RuzClient, RuzError


@pytest.mark.parametrize("url", ["http://ruz.mgimo.ru/api/", "https://other.test/api/",
    "https://ruz.mgimo.ru.other.test/api/", "https://ruz.mgimo.ru:444/api/",
    "https://user:password@ruz.mgimo.ru/api/"])
def test_tls_exception_rejects_other_origins(url):
    with pytest.raises(ValueError, match="Исключение TLS"):
        RuzClient(url, verify_tls=False)


async def test_tls_exception_does_not_follow_redirect(caplog):
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.test/data"})
    async with RuzClient("https://ruz.mgimo.ru/api/", verify_tls=False, delay=0,
                         transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuzError):
            await client.faculties()
    assert requested == ["https://ruz.mgimo.ru/api/dictionary/faculties"]
    assert "проверка TLS отключена" in caplog.text


async def test_default_keeps_tls_verification(monkeypatch):
    original = httpx.AsyncClient
    flags = []
    class Recorder(original):
        def __init__(self, *args, **kwargs):
            flags.append(kwargs["verify"])
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", Recorder)
    async with RuzClient("https://ruz.mgimo.ru/api/"):
        pass
    assert flags == [True]
