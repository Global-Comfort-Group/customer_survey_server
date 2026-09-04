import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
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
        # Landscape: a survey row is base columns plus one per question, which
        # never fitted portrait A4 — the table ran off both page edges and the
        # reader saw an arbitrary middle slice.
        doc = SimpleDocTemplate(
            buf, pagesize=landscape(A4),
            topMargin=28, bottomMargin=28, leftMargin=24, rightMargin=24,
        )
        styles = getSampleStyleSheet()
        cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7.5, leading=9.5)
        head = ParagraphStyle("head", parent=cell, textColor=colors.white,
                              fontName="Helvetica-Bold")

        elements = [
            Paragraph(f"Survey Responses: {survey.title}", styles["Title"]),
            Paragraph(f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
            Spacer(1, 12),
        ]

        avail = doc.width
        n_questions = max(0, len(headers) - 3)
        BASE_WIDTHS = [58, 78, 66]          # id (shortened), timestamp, respondent
        MIN_Q_WIDTH = 62
        per_question = (avail - sum(BASE_WIDTHS)) / n_questions if n_questions else 0

        def _short_id(v):
            return str(v)[:8]

        if n_questions and per_question < MIN_Q_WIDTH:
            # Too many questions to fit any legible grid. A wide table here is
            # what produced overlapping, unreadable blocks, so switch layout:
            # one block per response, questions down the page.
            for r_i, row in enumerate(rows):
                meta = (
                    f"<b>Response</b> {_short_id(row[0])} &nbsp;|&nbsp; "
                    f"<b>Submitted</b> {row[1]} &nbsp;|&nbsp; <b>Respondent</b> {row[2]}"
                )
                elements.append(Paragraph(meta, cell))
                elements.append(Spacer(1, 4))
                qa = [
                    [Paragraph(str(headers[i]), cell), Paragraph(str(row[i]), cell)]
                    for i in range(3, len(headers))
                ]
                t = Table(qa, colWidths=[avail * 0.32, avail * 0.68])
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]))
                elements.append(t)
                if r_i != len(rows) - 1:
                    elements.append(Spacer(1, 14))
        else:
            col_widths = BASE_WIDTHS + [per_question] * n_questions
            # Every cell is a Paragraph so long answers wrap instead of
            # overflowing into neighbouring columns.
            table_data = [[Paragraph(str(h), head) for h in headers]] + [
                [Paragraph(_short_id(row[0]), cell)]
                + [Paragraph(str(v), cell) for v in row[1:]]
                for row in rows
            ]
            t = Table(table_data, colWidths=col_widths, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            elements.append(t)

        if not rows:
            elements.append(Paragraph("No responses yet.", styles["Normal"]))

        doc.build(elements)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}_responses.pdf"'},
        )
