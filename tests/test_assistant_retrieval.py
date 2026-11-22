"""Регрессии реальных отказов: разговорные вопросы, контекст и пустой выбор модели."""
import pytest

from app.assistant.bootstrap import create_assistant
from app.assistant.conversation import Conversation
from app.assistant.corpus import Corpus
from app.assistant.errors import ProviderError
from app.assistant.retrieval import evidence_context
from tests.test_assistant import chunk, config, write_corpus, FakeProvider, fixed_today


def test_long_question_does_not_discard_direct_rule():
    rule = chunk(id="illness", title="Текущий контроль", text=
        "Пропущенное по болезни контрольное мероприятие переносится по заявлению и медицинской справке.")
    c = Corpus([rule], "snapshot")
    assert c.search("Можно ли принести справку о болезни в деканат и получить освобождение от контрольной точки?", c.chunks, 5)


def test_short_rule_survives_long_documents_and_colloquial_expulsion():
    rule = chunk(id="expulsion", document_id="charter", title="Устав", text=
        "Отчисление применяется как дисциплинарное взыскание при нарушении правил внутреннего распорядка.")
    others = [chunk(id=f"other-{i}", document_id=f"other-{i}", title="Методические рекомендации", text=
        "В моем вузе могут использоваться методы исследования и другие приемы оформления работы. " * 12) for i in range(3)]
    c = Corpus([*others, rule], "snapshot")
    assert c.search("За что меня могут отчислить из моего ВУЗа?", c.chunks, 1)[0].id == rule.id


def test_context_includes_conditions_but_never_other_document_or_revision():
    before = chunk(id="conditions", text="Обязательные условия и исключения применяются к следующему пункту.")
    hit = chunk(id="hit")
    after = chunk(id="continued", text="Продолжение процедуры содержит дополнительные ограничения и сроки.")
    other = chunk(id="other", document_id="other")
    old = chunk(id="old", revision="2025")
    rows = [before, other, hit, old, after]
    result = evidence_context([hit], rows)
    assert {r.id for r in result} == {"conditions", "hit", "continued"}
    assert result[0] == hit
    assert evidence_context([hit], rows, budget=10) == []
    assert all(r.text == next(c.text for c in rows if c.id == r.id) for r in result)


def test_context_budget_counts_serialized_metadata_and_preserves_whole_excerpt():
    import json
    from app.assistant.retrieval import source_payload
    rows = [chunk(id=f"c-{i}", related_document_ids=["x" * 100] * 15) for i in range(5)]
    result = evidence_context([rows[2]], rows, budget=4000)
    assert result and len(result) < len(rows)
    assert len(json.dumps([source_payload(r) for r in result], ensure_ascii=False)) <= 4000
    assert all(r.text == chunk().text for r in result)


async def test_empty_draft_retried_once_without_inventing_evidence(tmp_path):
    write_corpus(tmp_path, [chunk()])
    empty = {"status": "partial", "summary_id": None, "steps": []}
    provider = FakeProvider(empty, empty)
    result = await create_assistant(config(tmp_path), provider).ask(1, "Академический отпуск", [], None)
    assert result["status"] == "not_found" and result["summary"] is None
    assert len(provider.calls) == 2


async def test_empty_draft_can_recover_but_still_requires_audit(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider({"status": "answer", "steps": []},
        {"status": "answer", "summary_id": "leave-1"}, {"accepted_ids": [], "complete": False})
    result = await create_assistant(config(tmp_path), provider).ask(1, "Академический отпуск", [], None)
    assert result["status"] == "not_found" and result["summary"] is None
    assert len(provider.calls) == 3


async def test_capabilities_come_only_from_active_corpus_without_personal_access(tmp_path):
    write_corpus(tmp_path, [chunk(), chunk(id="hidden", document_id="hidden", reviewed_public=False, title="Закрытый документ")])
    provider = FakeProvider({"scopes": ["public"], "intent": "capabilities"})
    conversation = Conversation("owner", float("inf"))
    result = await create_assistant(config(tmp_path), provider).chat(
        1, None, "На что может ответить ассистент?", [], None, conversation, None)
    assert result["status"] == "answer"
    assert [f["text"] for f in result["facts"]] == ["Академический отпуск"]
    assert not conversation.grades_allowed and len(provider.calls) == 1


async def test_search_rewrite_does_not_replace_question_seen_by_auditor(tmp_path):
    write_corpus(tmp_path, [chunk()])
    question = "При каких обстоятельствах я могу прервать учебу?"
    provider = FakeProvider({"scopes": ["public"], "search_query": "академический отпуск"},
        {"status": "answer", "summary_id": "leave-1"}, {"accepted_ids": ["leave-1"], "complete": True})
    result = await create_assistant(config(tmp_path), provider).chat(
        1, None, question, [], None, Conversation("owner", float("inf")), None)
    assert result["status"] == "answer"
    assert all(data["question"] == question for _, data in provider.calls)


@pytest.mark.parametrize("question", [
    "Что будет если я голым приду на пару?",
    "А что если я приду голым на пару?",
])
async def test_model_searches_dress_requirement_for_colloquial_scenario(tmp_path, question):
    rule = chunk(id="dress", document_id="conduct", title="Правила внутреннего распорядка",
        text="Обучающиеся должны быть опрятными и придерживаться меры в одежде.")
    measures = chunk(id="measures", document_id="measures", title="Меры ответственности",
        text="Дисциплинарные взыскания применяются с учётом обстоятельств проступка.")
    write_corpus(tmp_path, [rule, measures])
    corpus = Corpus([rule, measures], "snapshot")
    assert not corpus.search(question, corpus.chunks, 5)
    provider = FakeProvider(
        {"scopes": ["public"], "search_query": "внешний вид",
         "consequences_query": "дисциплинарные взыскания"},
        {"status": "partial", "summary_id": rule.id},
        {"accepted_ids": [rule.id], "complete": False},
    )
    result = await create_assistant(config(tmp_path, rag_use_embeddings=False), provider).chat(
        1, 1, question, [], None, Conversation("owner", float("inf")), evidence_reader=None)
    assert result["status"] == "partial" and result["summary"]["id"] == rule.id
    assert result["steps"] == [] and len(provider.calls) == 3
    assert all(data["question"] == question for _, data in provider.calls)
    assert [source["id"] for source in provider.calls[1][1]["sources"]] == [rule.id, measures.id]


@pytest.mark.parametrize("question", [
    "Что будет если я голым приду на пару?",
    "А что если я приду голым на пару?",
])
async def test_draft_refusal_shows_original_search_results(tmp_path, question):
    rule = chunk(id="dress", document_id="conduct", clause="п. 2.4",
                 text="Обучающиеся должны соблюдать правила внешнего вида и опрятности в одежде.")
    sanction = chunk(id="measures", document_id="measures", clause="п. 5.2",
                     text="Дисциплинарные взыскания применяются с учётом обстоятельств проступка.")
    hidden = chunk(id="hidden", document_id="hidden", reviewed_public=False, text=rule.text)
    write_corpus(tmp_path, [rule, sanction, hidden])
    provider = FakeProvider(
        {"scopes": ["public"], "search_query": "внешний вид", "consequences_query": "дисциплинарные взыскания"},
        ProviderError("Отказ", code="generation_refused"),
    )
    assistant = create_assistant(config(tmp_path, rag_use_embeddings=False), provider)
    conversation = Conversation("owner", float("inf"))
    answer = await assistant.chat(1, 1, question, [], None, conversation, None)

    assert answer["status"] == "sources_only" and answer["summary"] is None
    assert [step["text"] for step in answer["steps"]] == [rule.text, sanction.text]
    assert [step["url"] for step in answer["steps"]] == [rule.original_url, sanction.original_url]
    assert not assistant._cacheable(answer, False) and not conversation.dialogue()
    assert len(provider.calls) == 2


async def test_consequences_keep_context_space_before_rule_neighbors(tmp_path, monkeypatch):
    rules = [chunk(id=f"rule-{i}", text="а" * 6000) for i in range(6)]
    sanction = chunk(id="sanction", document_id="sanction", text="б" * 4000)
    write_corpus(tmp_path, [*rules, sanction])

    def search(self, query, rows, limit, *args, **kwargs):
        by_id = {row.id: row for row in rows}
        ids = ("rule-1", "rule-4") if query == "требование к одежде" else ("sanction",)
        return [by_id[id_] for id_ in ids]

    monkeypatch.setattr(Corpus, "search", search)
    provider = FakeProvider(
        {"scopes": ["public"], "search_query": "требование к одежде", "consequences_query": "меры взыскания"},
        {"status": "partial", "summary_id": "rule-1"},
        {"accepted_ids": ["rule-1"], "complete": False},
    )
    answer = await create_assistant(config(tmp_path, rag_use_embeddings=False), provider).chat(
        1, 1, "Какое правило и какие последствия?", [], None, Conversation("owner", float("inf")), None)
    assert answer["status"] == "partial"
    sources = provider.calls[1][1]["sources"]
    assert [source["id"] for source in sources[:2]] == ["rule-1", "sanction"]
    assert sources[1]["text"] == sanction.text


async def test_route_recovers_from_empty_structured_generation(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider(ProviderError("Пустой ответ", code="empty_response"),
        {"scopes": ["public"], "search_query": "академический отпуск"},
        {"status": "answer", "summary_id": "leave-1"},
        {"accepted_ids": ["leave-1"], "complete": True})
    result = await create_assistant(config(tmp_path), provider).chat(
        1, 1, "Как мне взять академ?", [], None, Conversation("owner", float("inf")), None)
    assert result["status"] == "answer" and result["summary"]["id"] == "leave-1"
    assert len(provider.calls) == 4 and provider.calls[0] == provider.calls[1]


async def test_greeting_needs_neither_corpus_nor_personal_data(tmp_path):
    provider = FakeProvider()
    result = await create_assistant(config(tmp_path), provider).chat(
        1, None, "Привет", [], None, Conversation("owner", float("inf")), None)
    assert result["status"] == "help" and "Здравствуйте" in result["message"]
    assert result["snapshot"] is None and result["summary"] is None
    assert not provider.calls


async def test_greeting_with_substantive_question_still_routes_to_corpus(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider({"scopes": ["public"], "search_query": "академический отпуск"},
                            {"status": "not_found"})
    result = await create_assistant(config(tmp_path), provider).chat(
        1, None, "Привет, какие условия академического отпуска?", [], None,
        Conversation("owner", float("inf")), None)
    assert result["status"] == "not_found" and len(provider.calls) == 2
