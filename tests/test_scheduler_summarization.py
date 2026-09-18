"""
Test matrix: Memory
  "Long history summarizes in bounded chunks using only new messages."

Tests scheduler_worker._summarize_user()'s cursor logic (only messages
after last_summarized_message_id) and its char-budget chunking (a long
history gets split into multiple LLM calls, not one unbounded prompt).
The LLM call itself is mocked - this is about the surrounding logic,
not summary quality.
"""
from unittest.mock import AsyncMock

import pytest

from scheduler_worker import SchedulerWorker
from app.repositories.message_repository import MessageRepository
from app.models.database import Message, MemorySummary


@pytest.mark.asyncio
async def test_only_messages_after_the_cursor_are_summarized(db_session, make_user, mocker):
    user = make_user()
    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")

    for i in range(15):
        MessageRepository.create(db_session, conversation.id, user.id, "user", f"old message {i}", "web")
    db_session.commit()
    old_last_id = db_session.query(Message).order_by(Message.id.desc()).first().id

    db_session.add(MemorySummary(
        user_id=user.id, summary_text="Old summary.", last_summarized_message_id=old_last_id,
    ))
    db_session.commit()

    for i in range(12):
        MessageRepository.create(db_session, conversation.id, user.id, "user", f"new message {i}", "web")
    db_session.commit()

    mock_summarize = mocker.patch(
        "app.services.llm_service.llm_service.summarize_messages",
        new_callable=AsyncMock, return_value="Updated summary.",
    )

    await SchedulerWorker()._summarize_user(db_session, user)

    call_messages = mock_summarize.call_args.args[0]
    assert len(call_messages) == 12
    assert all("new message" in m["content"] for m in call_messages)
    assert all("old message" not in m["content"] for m in call_messages)

    latest = db_session.query(MemorySummary).order_by(MemorySummary.created_at.desc()).first()
    assert latest.summary_text == "Updated summary."
    assert latest.last_summarized_message_id > old_last_id


@pytest.mark.asyncio
async def test_fewer_than_10_new_messages_skips_summarization(db_session, make_user, mocker):
    user = make_user()
    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")
    for i in range(5):
        MessageRepository.create(db_session, conversation.id, user.id, "user", f"msg {i}", "web")
    db_session.commit()

    mock_summarize = mocker.patch(
        "app.services.llm_service.llm_service.summarize_messages",
        new_callable=AsyncMock,
    )

    await SchedulerWorker()._summarize_user(db_session, user)

    mock_summarize.assert_not_called()
    assert db_session.query(MemorySummary).count() == 0


@pytest.mark.asyncio
async def test_long_history_is_split_into_multiple_chunks(db_session, make_user, mocker):
    """
    20 messages of ~1900 chars each (~38,000 chars total) well exceeds
    the 12,000-char chunk budget - must produce more than one LLM call,
    not one unbounded prompt.
    """
    user = make_user()
    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")

    long_text = "x" * 1900
    for _ in range(20):
        MessageRepository.create(db_session, conversation.id, user.id, "user", long_text, "web")
    db_session.commit()

    call_count = 0

    async def fake_summarize(messages, previous_summary=None, usage_context=None):
        nonlocal call_count
        call_count += 1
        return f"summary-after-chunk-{call_count}"

    mocker.patch("app.services.llm_service.llm_service.summarize_messages", side_effect=fake_summarize)

    await SchedulerWorker()._summarize_user(db_session, user)

    assert call_count >= 2  # had to chunk, not one giant call

    latest = db_session.query(MemorySummary).order_by(MemorySummary.created_at.desc()).first()
    assert latest.summary_text == f"summary-after-chunk-{call_count}"  # folded forward through every chunk


@pytest.mark.asyncio
async def test_chunking_folds_previous_summary_forward_between_chunks(db_session, make_user, mocker):
    """Each chunk call must receive the PRIOR chunk's output as previous_summary, not None every time."""
    user = make_user()
    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")

    long_text = "y" * 1900
    for _ in range(20):
        MessageRepository.create(db_session, conversation.id, user.id, "user", long_text, "web")
    db_session.commit()

    seen_previous_summaries = []

    async def fake_summarize(messages, previous_summary=None, usage_context=None):
        seen_previous_summaries.append(previous_summary)
        return f"summary-{len(seen_previous_summaries)}"

    mocker.patch("app.services.llm_service.llm_service.summarize_messages", side_effect=fake_summarize)

    await SchedulerWorker()._summarize_user(db_session, user)

    assert len(seen_previous_summaries) >= 2
    assert seen_previous_summaries[0] is None  # first chunk: no prior summary
    assert seen_previous_summaries[1] == "summary-1"  # second chunk: folded forward from the first


# ------------------------------------------------------------------ #
# Test matrix: "Newest summary selection, preference array parsing,
# prompt-like memory isolation, and complete deletion/reset." - this
# closes the specific gap of never having proven that _summarize_user
# actually TRIGGERS preference extraction for a premium account (the
# JSON-array parsing itself was already covered separately in
# test_llm_service_json.py/test_llm_service_integration.py - this is
# the pipeline wiring around it).
# ------------------------------------------------------------------ #

from app.repositories.user_repository import UserRepository


@pytest.mark.asyncio
async def test_premium_account_triggers_preference_extraction(db_session, make_user, mocker):
    user = make_user()
    profile = UserRepository.get_or_create_profile(db_session, user.id)
    profile.account_tier = "premium"
    db_session.commit()

    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")
    for i in range(12):
        MessageRepository.create(db_session, conversation.id, user.id, "user", f"message {i}", "web")
    db_session.commit()

    mocker.patch(
        "app.services.llm_service.llm_service.summarize_messages",
        new_callable=AsyncMock, return_value="summary",
    )
    mock_prefs = mocker.patch.object(
        SchedulerWorker, "_extract_and_store_preferences", new_callable=AsyncMock,
    )

    await SchedulerWorker()._summarize_user(db_session, user)

    mock_prefs.assert_called_once()


@pytest.mark.asyncio
async def test_free_account_does_not_trigger_preference_extraction(db_session, make_user, mocker):
    user = make_user()
    profile = UserRepository.get_or_create_profile(db_session, user.id)
    profile.account_tier = "free"
    db_session.commit()

    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")
    for i in range(12):
        MessageRepository.create(db_session, conversation.id, user.id, "user", f"message {i}", "web")
    db_session.commit()

    mocker.patch(
        "app.services.llm_service.llm_service.summarize_messages",
        new_callable=AsyncMock, return_value="summary",
    )
    mock_prefs = mocker.patch.object(
        SchedulerWorker, "_extract_and_store_preferences", new_callable=AsyncMock,
    )

    await SchedulerWorker()._summarize_user(db_session, user)

    mock_prefs.assert_not_called()


@pytest.mark.asyncio
async def test_no_profile_at_all_does_not_trigger_preference_extraction(db_session, make_user, mocker):
    """A user with no UserProfile row yet must not crash or be treated as premium."""
    user = make_user()  # no get_or_create_profile call - deliberately no profile row

    conversation = MessageRepository.get_or_create_conversation(db_session, user.id, "web")
    for i in range(12):
        MessageRepository.create(db_session, conversation.id, user.id, "user", f"message {i}", "web")
    db_session.commit()

    mocker.patch(
        "app.services.llm_service.llm_service.summarize_messages",
        new_callable=AsyncMock, return_value="summary",
    )
    mock_prefs = mocker.patch.object(
        SchedulerWorker, "_extract_and_store_preferences", new_callable=AsyncMock,
    )

    await SchedulerWorker()._summarize_user(db_session, user)

    mock_prefs.assert_not_called()