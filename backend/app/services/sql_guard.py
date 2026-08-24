import re
from fastapi import HTTPException
import sqlglot
from sqlglot import exp

WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge)


def validate_readonly_sql(sql: str, max_rows: int) -> str:
    normalized = sql.strip()
    if not normalized:
        raise HTTPException(400, "SQL is empty")
    if ";" in normalized.rstrip(";"):
        raise HTTPException(400, "Only one SQL statement is allowed")
    try:
        parsed = sqlglot.parse(normalized, read="postgres")
    except Exception as exc:
        raise HTTPException(400, f"Invalid SQL: {exc}") from exc
    if len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.Union, exp.With, exp.Subquery)):
        raise HTTPException(400, "Only SELECT or WITH queries are allowed")
    tree = parsed[0]
    if any(tree.find(node) for node in WRITE_NODES):
        raise HTTPException(400, "Write operations are not allowed")
    if re.search(r"\b(copy|pg_read_file|dblink|lo_export)\b", normalized, re.I):
        raise HTTPException(400, "Unsafe database function is not allowed")
    if not tree.find(exp.Limit):
        normalized = normalized.rstrip(";") + f" LIMIT {max_rows}"
    return normalized
