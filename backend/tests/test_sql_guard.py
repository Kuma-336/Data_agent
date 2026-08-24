import pytest
from fastapi import HTTPException
from app.services.sql_guard import validate_readonly_sql


def test_adds_limit_to_select():
    assert validate_readonly_sql("SELECT * FROM sales", 50).endswith("LIMIT 50")


@pytest.mark.parametrize("sql", ["DELETE FROM sales", "SELECT 1; DELETE FROM sales", "COPY sales TO '/tmp/x'"])
def test_rejects_unsafe_sql(sql: str):
    with pytest.raises(HTTPException):
        validate_readonly_sql(sql, 50)
