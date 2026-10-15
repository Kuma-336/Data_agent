from html import escape
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..config import settings
from ..models import ChartArtifact, QueryResult, Report, ReportBlock, ReportVersion, User

ALLOWED_BLOCKS = {"markdown", "query_table", "chart", "metric", "divider"}

def _owned_query(db: Session, user: User, query_id: str | None):
    return db.query(QueryResult).filter(QueryResult.id == query_id, QueryResult.user_id == user.id).first() if query_id else None

def _owned_chart(db: Session, user: User, chart_id: str | None):
    return db.query(ChartArtifact).filter(ChartArtifact.id == chart_id, ChartArtifact.user_id == user.id).first() if chart_id else None

def serialize_report(db: Session, user: User, report: Report) -> dict:
    blocks = db.query(ReportBlock).filter(ReportBlock.report_id == report.id).order_by(ReportBlock.block_order).all()
    payload = []
    for block in blocks:
        stale = bool(block.query_id and not _owned_query(db, user, block.query_id))
        payload.append({"id": block.id, "type": block.block_type, "content": block.content, "query_id": block.query_id, "chart_id": block.chart_id, "stale": stale})
    return {"id": report.id, "title": report.title, "chat_id": report.chat_id, "blocks": payload, "updated_at": report.updated_at}

def save_report(db: Session, user: User, title: str, blocks: list[dict], chat_id: str | None = None, report: Report | None = None) -> Report:
    if not title.strip(): raise HTTPException(422, "Report title is required")
    if not blocks or blocks[0].get("type") != "markdown" or not str((blocks[0].get("content") or {}).get("text", "")).strip():
        raise HTTPException(422, "Reports must begin with a non-empty markdown narrative")
    if report is None:
        report = Report(user_id=user.id, chat_id=chat_id, title=title[:200]); db.add(report); db.flush()
    else:
        report.title = title[:200]
        db.query(ReportBlock).filter(ReportBlock.report_id == report.id).delete()
    for order, block in enumerate(blocks):
        kind = str(block.get("type", ""))
        if kind not in ALLOWED_BLOCKS: raise HTTPException(422, "Unsupported report block type")
        query_id, chart_id = block.get("query_id"), block.get("chart_id")
        if query_id and not _owned_query(db, user, query_id): raise HTTPException(404, "Referenced query not found")
        if chart_id and not _owned_chart(db, user, chart_id): raise HTTPException(404, "Referenced chart not found")
        db.add(ReportBlock(report_id=report.id, block_order=order, block_type=kind, content=block.get("content", {}), query_id=query_id, chart_id=chart_id))
    db.flush(); snapshot = serialize_report(db, user, report)
    version = (db.query(func.max(ReportVersion.version)).filter(ReportVersion.report_id == report.id).scalar() or 0) + 1
    db.add(ReportVersion(report_id=report.id, version=version, title=report.title, blocks=snapshot["blocks"])); db.commit(); db.refresh(report)
    return report

def export_report(db: Session, user: User, report: Report, format: str) -> Path:
    data = serialize_report(db, user, report); base = settings.data_dir / "reports" / user.id; base.mkdir(parents=True, exist_ok=True)
    if format == "html":
        path = base / f"{report.id}.html"; parts = [f"<h1>{escape(data['title'])}</h1>"]
        for block in data["blocks"]:
            if block["type"] == "markdown": parts.append(f"<p>{escape(str(block['content'].get('text', '')))}</p>")
            elif block["type"] == "metric": parts.append(f"<h2>{escape(str(block['content'].get('label', 'Metric')))}: {escape(str(block['content'].get('value', '')))}</h2>")
            else: parts.append(f"<p>[{block['type']} block: {escape(str(block.get('query_id') or block.get('chart_id') or ''))}]</p>")
        path.write_text("<!doctype html><html><body>" + "\n".join(parts) + "</body></html>", encoding="utf-8"); return path
    if format == "pdf":
        path = base / f"{report.id}.pdf"; pdf = canvas.Canvas(str(path), pagesize=A4); y = 800; pdf.setFont("Helvetica-Bold", 18); pdf.drawString(48, y, report.title); y -= 36; pdf.setFont("Helvetica", 10)
        for block in data["blocks"]:
            text = str(block["content"].get("text", block["content"].get("value", f"[{block['type']}]")))
            for line in text[:1000].splitlines() or [text]: pdf.drawString(48, y, line[:100]); y -= 15;
            if y < 60: pdf.showPage(); y = 800
        pdf.save(); return path
    raise HTTPException(422, "Supported formats are html and pdf")
