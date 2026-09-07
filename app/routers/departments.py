from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
import uuid

from ..database import get_db
from ..models import Department, AuditLog, User, Survey, Response, Question, QuestionType
from ..schemas import DepartmentCreate, DepartmentUpdate, DepartmentOut, department_code
from ..security import require_admin, require_any

router = APIRouter(prefix="/api/departments", tags=["departments"])


def _log(db, user_id, action, resource_id, detail, ip):
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        user_id=user_id,
        action=action,
        resource="department",
        resource_id=resource_id,
        detail=detail,
        ip_address=ip,
    ))


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rating_question_ids(db: Session, survey_ids: list[str]) -> set[str]:
    if not survey_ids:
        return set()
    rows = (
        db.query(Question.id)
        .filter(Question.survey_id.in_(survey_ids), Question.type == QuestionType.rating)
        .all()
    )
    return {r[0] for r in rows}


def _departments_with_stats(db: Session) -> list[DepartmentOut]:
    """The master list plus everything its detail pane shows.

    One pass over surveys and responses rather than a query per department —
    the list is small but this is the screen's only call.
    """
    depts = db.query(Department).order_by(Department.name.asc()).all()
    surveys = db.query(Survey).filter(Survey.department_id.isnot(None)).all()

    by_dept: dict[str, list[Survey]] = {}
    for s in surveys:
        by_dept.setdefault(s.department_id, []).append(s)

    all_ids = [s.id for s in surveys]
    responses = (
        db.query(Response).filter(Response.survey_id.in_(all_ids)).all() if all_ids else []
    )
    responses_by_survey: dict[str, list[Response]] = {}
    for r in responses:
        responses_by_survey.setdefault(r.survey_id, []).append(r)

    rating_qids = _rating_question_ids(db, all_ids)

    # Two departments can share initials ("Customer Service" / "Corporate
    # Sales"); the later one gets a numeric suffix so the tiles stay distinct.
    seen: dict[str, int] = {}

    out = []
    for d in depts:
        own = by_dept.get(d.id, [])
        mix = [0, 0, 0, 0, 0]
        response_count = 0
        for s in own:
            for r in responses_by_survey.get(s.id, []):
                response_count += 1
                for qid, val in (r.answers or {}).items():
                    if qid not in rating_qids:
                        continue
                    try:
                        bucket = int(round(float(val)))
                    except (TypeError, ValueError):
                        continue
                    if 1 <= bucket <= 5:
                        mix[bucket - 1] += 1

        rated = sum(mix)
        csat = (
            round(sum((i + 1) * n for i, n in enumerate(mix)) / rated, 1) if rated else None
        )

        row = DepartmentOut.from_orm_department(d)
        seen[row.code] = seen.get(row.code, 0) + 1
        if seen[row.code] > 1:
            row.code = f"{row.code}{seen[row.code]}"
        row.headName = d.head.full_name if d.head else None
        row.surveyCount = len(own)
        row.publishedCount = sum(1 for s in own if s.status == "published")
        row.responseCount = response_count
        row.csat = csat
        row.ratingMix = mix
        out.append(row)
    return out


@router.get("", response_model=list[DepartmentOut])
def list_departments(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_any),
):
    return _departments_with_stats(db)


@router.post("", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Department name is required")
    existing = db.query(Department).filter(Department.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Department already exists")

    dept = Department(id=str(uuid.uuid4()), name=name)
    db.add(dept)
    db.flush()
    _log(db, current_user.id, "CREATE_DEPARTMENT", dept.id, f"Created: {dept.name}", _ip(request))
    db.commit()
    db.refresh(dept)
    return DepartmentOut.from_orm_department(dept)


@router.put("/{department_id}", response_model=DepartmentOut)
def update_department(
    department_id: str,
    payload: DepartmentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Department name cannot be empty")
        if db.query(Department).filter(Department.name == new_name, Department.id != department_id).first():
            raise HTTPException(status_code=400, detail="Another department with this name already exists")
        dept.name = new_name
    _log(db, current_user.id, "UPDATE_DEPARTMENT", dept.id, f"Updated: {dept.name}", _ip(request))
    db.commit()
    db.refresh(dept)
    return DepartmentOut.from_orm_department(dept)


@router.delete("/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(
    department_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    _log(db, current_user.id, "DELETE_DEPARTMENT", dept.id, f"Deleted: {dept.name}", _ip(request))
    db.delete(dept)
    db.commit()
