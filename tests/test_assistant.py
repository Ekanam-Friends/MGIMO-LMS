"""Контракты безопасности и регрессии RAG. GigaChat заменён MockTransport/заглушкой."""
import asyncio
import hashlib
import json
import time
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from app.assistant.corpus import Chunk, Corpus, CorpusError, public_url
from app.assistant.repositories import load_corpus
from app.assistant.ingest import prepare
from app.assistant.provider import GigaChat, ProviderError
from app.assistant.bootstrap import create_assistant as Assistant
from app.assistant.service import BusyError
from app.core.config import Settings

TODAY = date(2026, 11, 15)
TEXT = "Для оформления академического отпуска обучающийся подаёт заявление в деканат. Решение принимается в течение 10 рабочих дней."


def chunk(**values):
    return Chunk(**(dict(id="leave-1", document_id="leave", revision="2026", title="Академический отпуск",
        source_url="https://mgimo.ru/sveden/document/leave.pdf", source_sha256="a"*64,
        document_date="2026-01-01", effective_from="2026-02-01", checked_at="2026-11-15",
        downloaded_at="2026-11-14", reviewed_public=True, page=3, clause="п. 2.1", text=TEXT) | values))


def config(tmp_path, **values):
    return Settings(_env_file=None, **(dict(database_url="sqlite+aiosqlite:///:memory:",
        llm_provider="gigachat", rag_corpus_dir=str(tmp_path), rag_use_embeddings=True) | values))


def write_corpus(tmp_path, rows):
    (tmp_path / "chunks.jsonl").write_text("\n".join(c.model_dump_json() for c in rows), encoding="utf-8")


class FakeProvider:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    async def complete(self, system, data, **kwargs):
        self.calls.append((system, data))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return json.dumps(result, ensure_ascii=False)

    async def embed(self, texts, model):
        self.calls.append(("embedding", texts))
        return [[1., 0.] for _ in texts]

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.assistant.service.moscow_today", lambda: TODAY)


@pytest.mark.parametrize("url", ["http://mgimo.ru/x", "https://mgimo.ru.evil.test/x", "javascript:alert(1)",
    "https://127.0.0.1/x", "https://mgimo.ru@evil.test/x", "https://mgimo.ru:8443/x", "https://mgimo.ru/x#page=2"])
def test_source_urls_reject_untrusted_origins(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_metadata_intervals_and_source_freshness(tmp_path):
    old = chunk(id="old", revision="2025", document_date="2025-01-01", effective_from="2025-01-01", effective_to="2026-02-01")
    current = chunk()
    corpus = Corpus([old, current, chunk(id="unreviewed", document_id="other", reviewed_public=False)], "sha")
    assert [c.id for c in corpus.active(date(2026, 1, 31), TODAY, 30)] == ["old"]
    assert [c.id for c in corpus.active(date(2026, 2, 1), TODAY, 30)] == ["leave-1"]
    assert not Corpus([chunk(checked_at="2026-10-01", downloaded_at="2026-09-30")], "sha").active(TODAY, TODAY, 30)
    assert not Corpus([chunk(checked_at="2026-11-16")], "sha").active(TODAY, TODAY, 30)
    with pytest.raises(CorpusError):
        Corpus([old.model_copy(update={"effective_to": None}), current], "sha").active(TODAY, TODAY, 30)


def test_snapshot_has_no_invented_dates_and_is_not_historical(tmp_path):
    row = chunk(document_date=None, effective_from=None, verification_status="snapshot")
    write_corpus(tmp_path, [row])
    corpus = load_corpus(tmp_path / "chunks.jsonl")
    assert corpus.active(TODAY, TODAY, 30)
    assert not corpus.active(date(2026,11,14), TODAY, 30)
    with pytest.raises(ValidationError):
        chunk(document_date=None, effective_from=None, verification_status="verified")


async def test_snapshot_can_never_be_presented_as_verified_full_answer(tmp_path):
    write_corpus(tmp_path, [chunk(document_date=None, effective_from=None, verification_status="snapshot", page_end=4)])
    provider = FakeProvider({"status":"answer", "summary_id":"leave-1", "steps":[]},
                            {"accepted_ids":["leave-1"], "complete":True})
    result = await Assistant(config(tmp_path), provider).ask(1,"Академический отпуск",[],None)
    assert result["status"] == "partial" and result["summary"]["effective_from"] is None
    assert result["summary"]["page_end"] == 4 and "OCR" in result["message"]


def test_index_rejects_duplicate_ids_and_conflicting_metadata(tmp_path):
    for rows in [[chunk(), chunk()], [chunk(), chunk(id="leave-2", checked_at="2026-11-14")]]:
        write_corpus(tmp_path, rows)
        with pytest.raises(CorpusError):
            load_corpus(tmp_path / "chunks.jsonl")


def test_two_stage_search_does_not_retrieve_transfer_for_academic_leave():
    transfer = chunk(id="transfer", document_id="transfer", title="Переход на бесплатное обучение",
        text="Обучающийся без академической задолженности подаёт заявление о переходе на бесплатное обучение в деканат.")
    corpus = Corpus([chunk(), transfer], "sha")
    assert corpus.search("Как оформить академический отпуск?", corpus.chunks, 5)[0].id == "leave-1"
    assert not corpus.search("Как купить криптовалюту?", corpus.chunks, 5)


def test_dense_vectors_must_match_dimension_and_be_finite():
    corpus = Corpus([chunk(embedding_model="Embeddings", embedding=[1, 0])], "sha")
    with pytest.raises(CorpusError):
        corpus.search("Академический отпуск", corpus.chunks, 5, [1, 0, 0])
    with pytest.raises(ValidationError):
        chunk(embedding_model="Embeddings", embedding=[float("nan")])


def test_semantic_search_is_not_gated_by_lexical_matches():
    target = chunk(embedding_model="Embeddings", embedding=[1., 0.])
    other = chunk(id="transfer", document_id="transfer", title="Переход на бюджет",
                  text="Заявление о переходе на бесплатное обучение подаётся в установленном порядке.",
                  embedding_model="Embeddings", embedding=[0., 1.])
    corpus = Corpus([target, other], "sha")
    assert not corpus.search("Прервать учёбу временно", corpus.chunks, 1)
    assert corpus.search("Прервать учёбу временно", corpus.chunks, 1, [1., 0.])[0].id == target.id


def test_retroactive_revision_requires_explicit_reviewed_basis():
    with pytest.raises(ValidationError):
        chunk(effective_from="2025-01-01")
    assert chunk(effective_from="2025-01-01", retroactive_basis="Пункт 1 приказа распространяет действие на этот период")


async def test_verified_answer_uses_source_text_and_server_urls(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider({"status":"answer", "summary_id":"leave-1", "steps":[
        {"source_id":"leave-1", "deadline":"в течение 10 рабочих дней", "form":"заявление"}]},
        {"accepted_ids":["leave-1"], "complete":True})
    result = await Assistant(config(tmp_path), provider).ask(1, "Как оформить академический отпуск?", [], None)
    assert result["status"] == "answer"
    assert result["summary"]["text"] == TEXT
    assert result["steps"][0]["url"] == chunk().original_url
    assert result["steps"][0]["checked_at"] == "2026-11-15"
    assert len(result["snapshot"]) == 64 and len(provider.calls) == 2


async def test_nonexistent_sources_and_invented_deadline_are_not_shown(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider({"status":"answer", "summary_id":"leave-1", "steps":[
        {"source_id":"unknown"}, {"source_id":"leave-1", "deadline":"30 дней"}]},
        {"accepted_ids":["leave-1"], "complete":True})
    result = await Assistant(config(tmp_path), provider).ask(1, "Академический отпуск", [], None)
    assert result["status"] == "partial" and result["steps"] == [] and result["dropped_steps"] == 2


async def test_valid_citation_to_wrong_procedure_is_rejected_by_audit(tmp_path):
    transfer = chunk(id="transfer", document_id="transfer", title="Переход на бесплатное обучение",
        text="Обучающийся без академической задолженности подаёт заявление о переходе на бесплатное обучение в деканат.")
    write_corpus(tmp_path, [transfer])
    provider = FakeProvider({"status":"answer", "summary_id":"transfer", "steps":[]},
        {"accepted_ids":[], "complete":False})
    result = await Assistant(config(tmp_path), provider).ask(1, "Академический отпуск", [], None)
    assert result["status"] == "not_found" and result["summary"] is None
    assert len(provider.calls) == 2  # кандидат был найден; отказ получен именно после аудита


@pytest.mark.parametrize("draft", [
    {"status":"answer", "summary_id":"hacked", "steps":[]},
    {"status":"not_found", "summary_id":"leave-1", "steps":[]},
    {"status":"answer", "summary_id":"leave-1", "url":"javascript:alert(1)"},
    {"status":"clarify", "steps":[]},
])
async def test_malformed_drafts_fail_closed(tmp_path, draft):
    write_corpus(tmp_path, [chunk()])
    result = await Assistant(config(tmp_path), FakeProvider(draft)).ask(1, "Академический отпуск", [], None)
    assert result["status"] == "error" and not result["steps"]


async def test_provider_failure_is_not_a_refusal_or_nearest_document_fallback(tmp_path):
    write_corpus(tmp_path, [chunk()])
    result = await Assistant(config(tmp_path), FakeProvider(ProviderError("secret-token"))).ask(1, "Академический отпуск", [], None)
    assert result["status"] == "error" and result["summary"] is None
    assert "secret-token" not in json.dumps(result)


async def test_lexical_mode_no_evidence_refuses_without_generation(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider()
    result = await Assistant(config(tmp_path), provider).ask(1, "Как купить криптовалюту?", [], None)
    assert result["status"] == "not_found" and not provider.calls


async def test_missing_or_stale_corpus_is_unavailable_not_not_found(tmp_path):
    for rows in [None, [chunk(checked_at="2026-10-01", downloaded_at="2026-09-30")]]:
        if rows:
            write_corpus(tmp_path, rows)
        result = await Assistant(config(tmp_path), FakeProvider()).ask(1, "Академический отпуск", [], None)
        assert result["status"] == "unavailable"


async def test_data_minimization_and_no_model_answers_in_history(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider({"status":"not_found", "steps":[]})
    await Assistant(config(tmp_path), provider).ask(1,
        "Академический отпуск, Иванов Иван Иванович, a@example.test +7 999 111-22-33", [], None)
    sent = json.dumps(provider.calls, ensure_ascii=False)
    for secret in ["Иванов Иван Иванович", "a@example.test", "+7 999 111-22-33", "student_id", "password", "grades"]:
        assert secret not in sent


async def test_rate_limit_is_per_authenticated_user(tmp_path):
    write_corpus(tmp_path, [chunk()])
    service = Assistant(config(tmp_path, rag_requests_per_minute=1), FakeProvider())
    await service.ask(1, "Криптовалюта", [], None)
    with pytest.raises(BusyError):
        await service.ask(1, "Криптовалюта", [], None)
    assert (await service.ask(2, "Криптовалюта", [], None))["status"] == "not_found"


async def test_concurrent_request_and_cancellation_release_slots(tmp_path, monkeypatch):
    service = Assistant(config(tmp_path), FakeProvider())
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        started.set()
        await finish.wait()
        return {"status":"answer"}
    monkeypatch.setattr(service, "_answer", slow)
    task = asyncio.create_task(service.ask(1, "Академический отпуск", [], None))
    await started.wait()
    with pytest.raises(BusyError):
        await service.ask(1, "Другой вопрос", [], None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._slots == 0 and not service._active


async def test_gigachat_oauth_refresh_after_401_and_no_secret_in_error():
    auths, calls = [], []
    async def transport(request):
        calls.append(request)
        if request.url.path.endswith("oauth"):
            auths.append(request)
            return httpx.Response(200, json={"access_token":f"token{len(auths)}", "expires_at":(time.time()+1800)*1000})
        if request.headers["authorization"] == "Bearer token1":
            return httpx.Response(401, text="sensitive body")
        return httpx.Response(200, json={"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]})
    provider = GigaChat("secret-key", "GIGACHAT_API_PERS", "GigaChat-2", transport=httpx.MockTransport(transport))
    assert await provider.complete("system", {}) == "{}"
    assert len(auths) == 2 and auths[0].headers["rquid"] != auths[1].headers["rquid"]
    assert calls[-1].url.host == "api.giga.chat"
    await provider.aclose()


@pytest.mark.parametrize("payload", [{"choices":[]}, {"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]},
    {"choices":[{"message":{"content":"refused"},"finish_reason":"blacklist"}]}])
async def test_incomplete_or_filtered_generation_is_provider_error(payload):
    async def transport(request):
        if request.url.path.endswith("oauth"):
            return httpx.Response(200, json={"access_token":"token", "expires_at":(time.time()+1800)*1000})
        return httpx.Response(200, json=payload)
    provider = GigaChat("key", "GIGACHAT_API_PERS", "model", transport=httpx.MockTransport(transport))
    with pytest.raises(ProviderError):
        await provider.complete("system", {})
    await provider.aclose()


@pytest.mark.parametrize("content", ['{"scopes":["public"]}', '{"scopes":["public"]'])
async def test_gigachat_length_requires_a_closed_json_object_for_structured_output(content):
    async def transport(request):
        if request.url.path.endswith("oauth"):
            return httpx.Response(200, json={"access_token": "token", "expires_at": (time.time() + 1800) * 1000})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "length"}]})

    provider = GigaChat("key", "GIGACHAT_API_PERS", "model", transport=httpx.MockTransport(transport))
    try:
        if content.endswith("}"):
            assert await provider.complete("system", {}, response_schema={"type": "object"}) == content
        else:
            with pytest.raises(ProviderError, match="Генерация не завершена"):
                await provider.complete("system", {}, response_schema={"type": "object"})
    finally:
        await provider.aclose()


def test_ingest_keeps_all_clauses_after_short_heading(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Rules\n1. A student applies for academic leave at the dean office.\n2. The decision is issued within ten working days.")
    source = tmp_path / "rules.pdf"
    doc.save(source)
    doc.close()
    meta = chunk().model_dump(mode="json", exclude={"id","page","clause","text","embedding","embedding_model","source_sha256"})
    from app.assistant.ingest import pdf_pages
    extracted = pdf_pages(source, "")
    meta.update(file="rules.pdf", reviewed_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                reviewed_text_sha256=hashlib.sha256("\n\f\n".join(extracted).encode()).hexdigest())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([meta]), encoding="utf-8")
    chunks = prepare(manifest)
    joined = "\n".join(c.text for c in chunks)
    assert "A student applies" in joined and "The decision is issued" in joined
    assert chunks[0].page == 1
