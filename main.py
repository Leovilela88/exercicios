from datetime import date, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from db import Base, engine, get_db
from models import Workout

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Exercícios")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    totals = {
        "corrida_km": db.query(func.coalesce(func.sum(Workout.distance_km), 0.0))
        .filter(Workout.sport == "corrida").scalar() or 0.0,
        "natacao_km": db.query(func.coalesce(func.sum(Workout.distance_km), 0.0))
        .filter(Workout.sport == "natacao").scalar() or 0.0,
        "calorias": db.query(func.coalesce(func.sum(Workout.calories), 0.0)).scalar() or 0.0,
        "dias_ativos": db.query(func.count(func.distinct(Workout.date))).scalar() or 0,
        "total_treinos": db.query(func.count(Workout.id)).scalar() or 0,
    }

    # Últimos 30 dias — dias ativos (bar) e calorias por dia
    today = date.today()
    start = today - timedelta(days=29)
    rows = (
        db.query(
            Workout.date,
            func.coalesce(func.sum(Workout.calories), 0.0),
            func.count(Workout.id),
        )
        .filter(Workout.date >= start)
        .group_by(Workout.date)
        .all()
    )
    by_day = {r[0]: {"cal": float(r[1] or 0), "count": int(r[2])} for r in rows}
    labels_30 = []
    cal_30 = []
    active_30 = []
    for i in range(30):
        d = start + timedelta(days=i)
        labels_30.append(d.strftime("%d/%m"))
        info = by_day.get(d, {"cal": 0.0, "count": 0})
        cal_30.append(info["cal"])
        active_30.append(1 if info["count"] > 0 else 0)

    # Evolução — soma de km por semana, separado por esporte (últimas 12 semanas)
    weeks_back = 12
    start_week = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_back - 1)
    evo_rows = (
        db.query(Workout.date, Workout.sport, Workout.distance_km)
        .filter(Workout.date >= start_week, Workout.sport.in_(["corrida", "natacao"]))
        .all()
    )
    week_labels = []
    corrida_per_week = [0.0] * weeks_back
    natacao_per_week = [0.0] * weeks_back
    for i in range(weeks_back):
        wk = start_week + timedelta(weeks=i)
        week_labels.append(wk.strftime("%d/%m"))
    for d, sport, km in evo_rows:
        idx = (d - start_week).days // 7
        if 0 <= idx < weeks_back and km:
            if sport == "corrida":
                corrida_per_week[idx] += float(km)
            elif sport == "natacao":
                natacao_per_week[idx] += float(km)

    recent = (
        db.query(Workout).order_by(Workout.date.desc(), Workout.id.desc()).limit(10).all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "totals": totals,
            "labels_30": labels_30,
            "cal_30": cal_30,
            "active_30": active_30,
            "week_labels": week_labels,
            "corrida_per_week": corrida_per_week,
            "natacao_per_week": natacao_per_week,
            "recent": recent,
            "today": today.isoformat(),
        },
    )


@app.get("/novo", response_class=HTMLResponse)
def new_form(request: Request):
    return templates.TemplateResponse(
        "form.html", {"request": request, "today": date.today().isoformat()}
    )


@app.post("/novo")
def create_workout(
    sport: str = Form(...),
    workout_date: str = Form(...),
    distance_km: Optional[str] = Form(None),
    duration_min: Optional[str] = Form(None),
    calories: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    try:
        d = date.fromisoformat(workout_date)
    except ValueError:
        d = date.today()
    workout = Workout(
        date=d,
        sport=sport,
        distance_km=_to_float(distance_km),
        duration_min=_to_float(duration_min),
        calories=_to_float(calories),
        notes=(notes or None),
    )
    db.add(workout)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{workout_id}")
def delete_workout(workout_id: int, db: Session = Depends(get_db)):
    w = db.query(Workout).filter(Workout.id == workout_id).first()
    if w:
        db.delete(w)
        db.commit()
    return RedirectResponse(url="/", status_code=303)
