from utils import knowledge_db


def test_knowledge_upsert_is_idempotent_and_searchable(tmp_path, monkeypatch):
    db_path = tmp_path / "knowledge.sqlite3"
    monkeypatch.setattr(knowledge_db, "KNOWLEDGE_DB_FILE", db_path)

    assert knowledge_db.upsert_message(
        "42", chat_id=-100123, thread_id=0, message_id=7,
        text="Настройка часового пояса через timezone мск",
        chat_title="Работа", sender_name="Анна", sent_at="2026-08-01T10:00:00+00:00",
    ) is True
    assert knowledge_db.upsert_message(
        "42", chat_id=-100123, thread_id=0, message_id=7,
        text="Настройка часового пояса через timezone Europe/Moscow",
        chat_title="Работа", sender_name="Анна", sent_at="2026-08-01T10:00:00+00:00",
    ) is False

    assert knowledge_db.count_messages("42") == 1
    results = knowledge_db.search("42", "часового пояса")
    assert len(results) == 1
    assert "Europe/Moscow" in results[0]["text"]
    assert knowledge_db.search("other-user", "часового") == []


def test_knowledge_collection_state_is_durable(tmp_path, monkeypatch):
    db_path = tmp_path / "knowledge.sqlite3"
    monkeypatch.setattr(knowledge_db, "KNOWLEDGE_DB_FILE", db_path)

    knowledge_db.begin_collection("42", progress_message_id=55)
    knowledge_db.update_collection("42", total_dialogs=9, completed_dialogs=2, status="paused_manual")
    knowledge_db.upsert_dialog("42", -100123, title="Работа", status="running", oldest_processed_id=91)

    state = knowledge_db.get_collection("42")
    dialog = knowledge_db.get_dialog("42", -100123)
    assert state["progress_message_id"] == 55
    assert state["status"] == "paused_manual"
    assert dialog["oldest_processed_id"] == 91


def test_knowledge_search_can_be_limited_to_selected_chats(tmp_path, monkeypatch):
    db_path = tmp_path / "knowledge.sqlite3"
    monkeypatch.setattr(knowledge_db, "KNOWLEDGE_DB_FILE", db_path)
    knowledge_db.upsert_message("42", chat_id=10, thread_id=0, message_id=1, text="общая заметка")
    knowledge_db.upsert_message("42", chat_id=20, thread_id=0, message_id=1, text="общая заметка")

    results = knowledge_db.search("42", "общая", chat_ids={20})
    assert [result["chat_id"] for result in results] == [20]
    assert knowledge_db.search("42", "общая", chat_ids=set()) == []
