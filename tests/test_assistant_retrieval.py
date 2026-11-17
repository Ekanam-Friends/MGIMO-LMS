"""Регрессии реальных отказов: разговорные вопросы, контекст и пустой выбор модели."""
from app.assistant.bootstrap import create_assistant
from app.assistant.conversation import Conversation
from app.assistant.corpus import Corpus
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
