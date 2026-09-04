import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

from ..database import get_db
from ..models import Survey, Response, User, UserRole
from ..security import require_admin_or_manager

def _render_answer(val, question_type=None):
    """Flatten an answer into a spreadsheet cell.

    File answers hold a list of attachment ids, which would otherwise print as
    a Python repr. They render as a count and the dashboard links through to
    the files. Other list-valued answers (multi-select) keep their contents.
    """
    if val is None:
        return ""
    qt = getattr(question_type, "value", question_type)
    if qt == "file":
        ids = val if isinstance(val, list) else [val]
        ids = [i for i in ids if i]
        return f"{len(ids)} file(s)" if ids else ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if isinstance(val, dict):
        return ", ".join(f"{k}: {v}" for k, v in val.items())
    return val


router = APIRouter(prefix="/api/export", tags=["export"])


def _get_survey_and_responses(survey_id: str, db: Session):
    survey = db.query(Survey).filter(Survey.id == survey_id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    responses = db.query(Response).filter(Response.survey_id == survey_id).order_by(Response.submitted_at).all()
    return survey, responses


def _build_rows(survey, responses):
    """Return (headers, rows) for a survey's responses."""
    q_headers = [q.text for q in survey.questions]
    headers = ["Response ID", "Submitted At", "Respondent"] + q_headers

    rows = []
    for r in responses:
        respondent = "Anonymous" if r.is_anonymous else (r.respondent_name or "—")
        row = [
            r.id,
            r.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if r.submitted_at else "",
            respondent,
        ]
        for q in survey.questions:
            val = _render_answer(r.answers.get(q.id, ""), q.type)
            row.append(str(val))
        rows.append(row)

    return headers, rows


@router.get("/responses/{survey_id}")
def export_responses(
    survey_id: str,
    format: str = Query("csv", pattern="^(csv|xlsx|pdf)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_or_manager),
):
    survey, responses = _get_survey_and_responses(survey_id, db)
    if current_user.role != UserRole.admin and survey.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="You can only export responses for surveys you created")
    headers, rows = _build_rows(survey, responses)
    filename_base = survey.title.replace(" ", "_")[:40]

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}_responses.csv"'},
        )

    elif format == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Responses"
        ws.append(headers)
        for row in rows:
            ws.append(row)

        # Style header row
        for cell in ws[1]:
            cell.font = cell.font.copy(bold=True)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}_responses.xlsx"'},
        )

    elif format == "pdf":
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30)
        styles = getSampleStyleSheet()
        elements = []

        elements.append(Paragraph(f"Survey Responses: {survey.title}", styles["Title"]))
        elements.append(Paragraph(f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]))
        elements.append(Spacer(1, 12))

        table_data = [headers] + rows
        t = Table(table_data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        doc.build(elements)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}_responses.pdf"'},
        )
