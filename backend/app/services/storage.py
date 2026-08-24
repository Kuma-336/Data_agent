from pathlib import Path
import shutil
import uuid
import pandas as pd
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from ..config import settings
from ..models import UploadedFile, User

ALLOWED_SUFFIXES = {".csv", ".xlsx"}


def save_upload(db: Session, user: User, upload: UploadFile) -> UploadedFile:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, "Only CSV and XLSX files are supported")
    target_dir = settings.data_dir / "uploads" / user.id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4()}{suffix}"
    with target.open("wb") as out:
        shutil.copyfileobj(upload.file, out)
    size = target.stat().st_size
    if size > settings.max_upload_mb * 1024 * 1024:
        target.unlink(missing_ok=True)
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    schema = inspect_file(target)
    record = UploadedFile(
        user_id=user.id,
        original_name=upload.filename or target.name,
        storage_path=str(target),
        media_type=upload.content_type or "application/octet-stream",
        size_bytes=size,
        file_schema=schema,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def inspect_file(path: Path) -> dict:
    if path.suffix.lower() == ".csv":
        # Use Python's CSV sniffer so European semicolon-delimited exports do
        # not become one malformed column when the default comma is assumed.
        frame = pd.read_csv(path, sep=None, engine="python", nrows=20)
        return {"sheets": [{"name": "data", "columns": list(frame.columns), "sample_rows": frame.fillna("").to_dict("records")}]} 
    workbook = pd.ExcelFile(path)
    sheets = []
    for name in workbook.sheet_names:
        frame = pd.read_excel(path, sheet_name=name, nrows=20)
        sheets.append({"name": name, "columns": [str(x) for x in frame.columns], "sample_rows": frame.fillna("").to_dict("records")})
    return {"sheets": sheets}
