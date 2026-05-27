from datetime import date, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from calories import estimate_calories
from db import Base, engine, get_db
from models import Settings, Workout

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


def get_settings(db: Session) -> Settings:
    s = db.query(Settings).filter(Settings.id == 1).first()
    if not s:
        s = Settings(id=1, weight_kg=82.0)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _current_streak(active_dates: set[date], today: date) -> int:
    streak = 0
    d = today
    # Permite que a sequência comece "hoje ou ontem" (caso ainda não tenha treinado hoje)
    if d not in active_dates and (d - timedelta(days=1)) in active_dates:
        d = d - timedelta(days=1)
    while d in active_dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


RANGE_OPTIONS = [
    ("1w", "1 semana"),
    ("1m", "1 mês"),
    ("3m", "3 meses"),
    ("6m", "6 meses"),
    ("1y", "1 ano"),
    ("ytd", "No ano"),
]


def _resolve_range(range_key: str, today: date) -> tuple[date, str]:
    """Retorna (data_inicial, label_normalizado). Janela rolante até hoje."""
    key = range_key if range_key in dict(RANGE_OPTIONS) else "1m"
    if key == "1w":
        start = today - timedelta(days=6)
    elif key == "1m":
        start = today - timedelta(days=29)
    elif key == "3m":
        start = today - timedelta(days=89)
    elif key == "6m":
        start = today - timedelta(days=179)
    elif key == "1y":
        start = today - timedelta(days=364)
    elif key == "ytd":
        start = date(today.year, 1, 1)
    else:
        start = today - timedelta(days=29)
    return start, key


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    range_: str = Query("1m", alias="range"),
    db: Session = Depends(get_db),
):
    today = date.today()
    start, range_key = _resolve_range(range_, today)
    span_days = (today - start).days + 1
    bucket = "day" if span_days <= 90 else "week"

    totals = {
        "corrida_km": db.query(func.coalesce(func.sum(Workout.distance_km), 0.0))
        .filter(Workout.sport == "corrida").scalar() or 0.0,
        "natacao_km": db.query(func.coalesce(func.sum(Workout.distance_km), 0.0))
        .filter(Workout.sport == "natacao").scalar() or 0.0,
        "calorias": db.query(func.coalesce(func.sum(Workout.calories), 0.0)).scalar() or 0.0,
        "dias_ativos": db.query(func.count(func.distinct(Workout.date))).scalar() or 0,
        "total_treinos": db.query(func.count(Workout.id)).scalar() or 0,
    }

    # Sequência atual (streak)
    all_dates = {r[0] for r in db.query(func.distinct(Workout.date)).all()}
    streak = _current_streak(all_dates, today)

    # Esta semana (segunda → domingo) — sempre fixo, independente do range
    week_start = today - timedelta(days=today.weekday())
    week_rows = (
        db.query(Workout.sport, func.coalesce(func.sum(Workout.distance_km), 0.0),
                 func.coalesce(func.sum(Workout.calories), 0.0))
        .filter(Workout.date >= week_start)
        .group_by(Workout.sport)
        .all()
    )
    week = {"corrida_km": 0.0, "natacao_km": 0.0, "calorias": 0.0}
    for sport, km, cal in week_rows:
        if sport == "corrida":
            week["corrida_km"] = float(km or 0)
        elif sport == "natacao":
            week["natacao_km"] = float(km or 0)
        week["calorias"] += float(cal or 0)

    # Dados brutos no range
    range_rows = (
        db.query(
            Workout.date,
            func.coalesce(func.sum(Workout.calories), 0.0),
            func.count(Workout.id),
        )
        .filter(Workout.date >= start, Workout.date <= today)
        .group_by(Workout.date)
        .all()
    )
    by_day = {r[0]: {"cal": float(r[1] or 0), "count": int(r[2])} for r in range_rows}

    # Buckets de dias ativos / calorias
    labels = []
    cal_series = []
    active_series = []
    if bucket == "day":
        days = (today - start).days + 1
        for i in range(days):
            d = start + timedelta(days=i)
            info = by_day.get(d, {"cal": 0.0, "count": 0})
            labels.append(d.strftime("%d/%m"))
            cal_series.append(info["cal"])
            active_series.append(1 if info["count"] > 0 else 0)
    else:
        # Agrupar por semana (segunda como início). Bucket começa na segunda da semana de `start`.
        first_monday = start - timedelta(days=start.weekday())
        cur = first_monday
        while cur <= today:
            week_end = cur + timedelta(days=6)
            cal_sum = 0.0
            active_days = 0
            for j in range(7):
                d = cur + timedelta(days=j)
                if d < start or d > today:
                    continue
                info = by_day.get(d)
                if info:
                    cal_sum += info["cal"]
                    if info["count"] > 0:
                        active_days += 1
            labels.append(cur.strftime("%d/%m"))
            cal_series.append(cal_sum)
            active_series.append(active_days)  # 0–7
            cur += timedelta(days=7)

    # Evolução: km por esporte, mesmo agrupamento
    evo_rows = (
        db.query(Workout.date, Workout.sport, Workout.distance_km)
        .filter(Workout.date >= start, Workout.date <= today,
                Workout.sport.in_(["corrida", "natacao"]))
        .all()
    )
    if bucket == "day":
        evo_labels = labels
        corrida_evo = [0.0] * len(labels)
        natacao_evo = [0.0] * len(labels)
        for d, sport, km in evo_rows:
            if not km:
                continue
            idx = (d - start).days
            if 0 <= idx < len(labels):
                if sport == "corrida":
                    corrida_evo[idx] += float(km)
                else:
                    natacao_evo[idx] += float(km)
    else:
        evo_labels = labels
        corrida_evo = [0.0] * len(labels)
        natacao_evo = [0.0] * len(labels)
        first_monday = start - timedelta(days=start.weekday())
        for d, sport, km in evo_rows:
            if not km:
                continue
            idx = (d - first_monday).days // 7
            if 0 <= idx < len(labels):
                if sport == "corrida":
                    corrida_evo[idx] += float(km)
                else:
                    natacao_evo[idx] += float(km)

    # Breakdown por esporte (donut) — também respeita o range
    breakdown_rows = (
        db.query(Workout.sport, func.count(Workout.id))
        .filter(Workout.date >= start, Workout.date <= today)
        .group_by(Workout.sport).all()
    )
    breakdown = {"corrida": 0, "natacao": 0, "outro": 0}
    for sport, count in breakdown_rows:
        if sport in breakdown:
            breakdown[sport] = int(count)

    recent = (
        db.query(Workout).order_by(Workout.date.desc(), Workout.id.desc()).limit(10).all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "totals": totals,
            "week": week,
            "streak": streak,
            "labels": labels,
            "cal_series": cal_series,
            "active_series": active_series,
            "evo_labels": evo_labels,
            "corrida_evo": corrida_evo,
            "natacao_evo": natacao_evo,
            "bucket": bucket,
            "breakdown": breakdown,
            "recent": recent,
            "today": today.isoformat(),
            "range_options": RANGE_OPTIONS,
            "range_key": range_key,
        },
    )


@app.get("/novo", response_class=HTMLResponse)
def new_form(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    return templates.TemplateResponse(
        "form.html",
        {"request": request, "today": date.today().isoformat(), "weight_kg": settings.weight_kg},
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

    dist = _to_float(distance_km)
    dur = _to_float(duration_min)
    cal = _to_float(calories)

    if cal is None:
        settings = get_settings(db)
        cal = estimate_calories(sport, settings.weight_kg, dist, dur)

    workout = Workout(
        date=d,
        sport=sport,
        distance_km=dist,
        duration_min=dur,
        calories=cal,
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


@app.get("/config", response_class=HTMLResponse)
def config_get(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    return templates.TemplateResponse(
        "config.html", {"request": request, "settings": settings}
    )


@app.post("/config")
def config_post(weight_kg: str = Form(...), db: Session = Depends(get_db)):
    settings = get_settings(db)
    w = _to_float(weight_kg)
    if w and w > 0:
        settings.weight_kg = w
        db.commit()
    return RedirectResponse(url="/config", status_code=303)


@app.get("/api/estimate")
def api_estimate(
    sport: str,
    distance_km: Optional[float] = None,
    duration_min: Optional[float] = None,
    db: Session = Depends(get_db),
):
    settings = get_settings(db)
    kcal = estimate_calories(sport, settings.weight_kg, distance_km, duration_min)
    return JSONResponse({"kcal": kcal, "weight_kg": settings.weight_kg})
