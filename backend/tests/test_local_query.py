from app.db import Base, SessionLocal, engine
from app.models import Chat, UploadedFile, User
from app.services.query import execute_local
from fastapi import HTTPException
import pytest


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_csv_can_be_exposed_as_data_view_and_queried(tmp_path):
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("product,amount\nA,10\nB,25\n", encoding="utf-8")
    with SessionLocal() as db:
        user = User(email="csv@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        chat = Chat(user_id=user.id, title="CSV test")
        file = UploadedFile(user_id=user.id, original_name="sales.csv", storage_path=str(csv_path), media_type="text/csv", size_bytes=csv_path.stat().st_size, file_schema={})
        db.add_all([chat, file])
        db.commit()
        result = execute_local(db, user, chat.id, file, "SELECT SUM(amount) AS total FROM data")
        assert result.columns == ["total"]
        assert result.rows == [{"total": 35}]


def test_semicolon_csv_and_reserved_column_name_can_be_queried(tmp_path):
    csv_path = tmp_path / "bank.csv"
    csv_path.write_text('age;"default"\n56;"no"\n57;"unknown"\n', encoding="utf-8")
    with SessionLocal() as db:
        user = User(email="semicolon@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        chat = Chat(user_id=user.id, title="semicolon test")
        file = UploadedFile(user_id=user.id, original_name="bank.csv", storage_path=str(csv_path), media_type="text/csv", size_bytes=csv_path.stat().st_size, file_schema={})
        db.add_all([chat, file])
        db.commit()
        result = execute_local(db, user, chat.id, file, 'SELECT COUNT("default") AS populated FROM data')
        assert result.rows == [{"populated": 2}]


def test_local_file_query_rejects_invented_rowid(tmp_path):
    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    with SessionLocal() as db:
        user = User(email="rowid@example.com", password_hash="unused")
        db.add(user); db.flush()
        chat = Chat(user_id=user.id, title="rowid test")
        file = UploadedFile(user_id=user.id, original_name="rows.csv", storage_path=str(csv_path), media_type="text/csv", size_bytes=csv_path.stat().st_size, file_schema={})
        db.add_all([chat, file]); db.commit()
        with pytest.raises(HTTPException, match="no implicit rowid"):
            execute_local(db, user, chat.id, file, "SELECT rowid FROM data")
