import csv
import json
from collections import Counter
from pathlib import Path
import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session
from ..config import settings
from ..models import ChartArtifact, DataProfile, ExportArtifact, QueryResult, UploadedFile, User

ALLOWED_CHARTS = {"bar", "line", "pie", "scatter", "area", "table"}


def profile_file(db: Session, user: User, file: UploadedFile) -> DataProfile:
    path = Path(file.storage_path)
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path, sep=None, engine="python")
    else:
        frame = pd.read_excel(path, sheet_name=0)
    profile = _profile_frame(frame)
    record = DataProfile(user_id=user.id, asset_kind="file", asset_id=file.id, profile=profile)
    db.add(record); db.commit(); db.refresh(record)
    return record


def _profile_frame(frame: pd.DataFrame) -> dict:
    fields = []
    for name in frame.columns:
        series = frame[name]
        item = {"name": str(name), "dtype": str(series.dtype), "null_count": int(series.isna().sum()), "distinct_count": int(series.nunique(dropna=True)), "sample_values": [str(value) for value in series.dropna().head(5).tolist()]}
        if pd.api.types.is_numeric_dtype(series):
            item["min"] = None if series.dropna().empty else float(series.min())
            item["max"] = None if series.dropna().empty else float(series.max())
            item["mean"] = None if series.dropna().empty else float(series.mean())
        fields.append(item)
    return {"row_count": int(len(frame)), "column_count": int(len(frame.columns)), "duplicate_rows": int(frame.duplicated().sum()), "fields": fields}


def create_chart(db: Session, user: User, chat_id: str, query_id: str, spec: dict) -> ChartArtifact:
    result = db.query(QueryResult).filter(QueryResult.id == query_id, QueryResult.user_id == user.id).first()
    if not result:
        raise HTTPException(404, "Query result not found")
    kind = spec.get("type", "bar")
    x_key, y_keys = spec.get("x_key"), spec.get("y_keys", [])
    if kind not in ALLOWED_CHARTS or (x_key and x_key not in result.columns) or any(key not in result.columns for key in y_keys):
        raise HTTPException(422, "Chart references unsupported type or missing query columns")
    sanitized = {"title": str(spec.get("title", "Analysis"))[:160], "type": kind, "x_key": x_key, "y_keys": y_keys[:5], "query_id": query_id}
    artifact = ChartArtifact(user_id=user.id, chat_id=chat_id, query_id=query_id, spec=sanitized)
    db.add(artifact); db.commit(); db.refresh(artifact)
    return artifact


def export_csv(db: Session, user: User, chat_id: str, query_id: str) -> ExportArtifact:
    result = db.query(QueryResult).filter(QueryResult.id == query_id, QueryResult.user_id == user.id).first()
    if not result:
        raise HTTPException(404, "Query result not found")
    export_dir = settings.data_dir / "exports" / user.id
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{query_id}.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=result.columns)
        writer.writeheader(); writer.writerows(result.rows)
    artifact = ExportArtifact(user_id=user.id, chat_id=chat_id, query_id=query_id, format="csv", storage_path=str(path))
    db.add(artifact); db.commit(); db.refresh(artifact)
    return artifact
