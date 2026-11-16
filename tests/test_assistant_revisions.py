from datetime import date
from types import SimpleNamespace

from app.assistant.revisions import current_published

TODAY = date(2026, 11, 16)


def act(key, issued, family="scholarships", kind="base", related=(), start=None, end=None):
    return SimpleNamespace(document_id=key, document_date=issued, document_family=family,
        act_kind=kind, related_document_ids=list(related), publication_checked_at=TODAY,
        effective_from=start, effective_to=end)


def test_latest_main_act_replaces_only_same_family_and_keeps_amendment():
    rows = [act("old", date(2020,8,5)),act("new", date(2026,9,1)),
            act("change",date(2026,10,1),kind="amendment",related=["new"]),
            act("dorm",date(2023,1,1),family="dorm")]
    assert [r.document_id for r in current_published(rows,TODAY)] == ["new","change","dorm"]


def test_future_replacement_does_not_remove_current_act():
    rows = [act("current",date(2020,1,1)),act("future",date(2026,11,1),start=date(2027,1,1))]
    selected = current_published(rows,TODAY)
    assert "current" in [r.document_id for r in selected]


def test_expired_latest_act_does_not_resurrect_previous_or_amendments():
    rows = [act("old",date(2020,1,1)),act("latest",date(2025,1,1),end=date(2026,1,1)),
            act("change",date(2025,2,1),kind="amendment",related=["latest"])]
    assert current_published(rows,TODAY) == []


def test_explicit_repeal_and_unknown_dates_do_not_guess_by_filename():
    rows = [act("base",date(2020,1,1)),act("unclear",None),
            act("repeal",date(2026,1,1),kind="repeal",related=["base"])]
    assert [r.document_id for r in current_published(rows,TODAY)] == ["unclear","repeal"]


def test_same_date_keeps_both_for_conflict_check():
    rows = [act("first",TODAY),act("second",TODAY)]
    assert current_published(rows,TODAY) == rows
