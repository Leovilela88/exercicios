from datetime import date, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

from calories import estimate_calories
from db import Base, SessionLocal, engine, get_db
from models import Athlete, Settings, Workout

SPORTS = ("corrida", "natacao", "musculacao", "outro")


def _init_db() -> None:
    """Cria tabelas e migra dados legados de Settings -> Athlete."""
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)

    # Coluna athlete_id pode não existir em bases pré-migração.
    if "workouts" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("workouts")}
        if "athlete_id" not in cols:
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    conn.execute(text(
                        "ALTER TABLE workouts ADD COLUMN athlete_id INTEGER "
                        "REFERENCES athletes(id)"
                    ))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_workouts_athlete_id "
                        "ON workouts(athlete_id)"
                    ))
                else:
                    conn.execute(text("ALTER TABLE workouts ADD COLUMN athlete_id INTEGER"))

    db = SessionLocal()
    try:
        if not db.query(Athlete).first():
            weight = 82.0
            try:
                legacy = db.query(Settings).filter(Settings.id == 1).first()
                if legacy and legacy.weight_kg:
                    weight = float(legacy.weight_kg)
            except Exception:
                pass
            leo = Athlete(name="Leonardo", weight_kg=weight)
            db.add(leo)
            db.commit()
            db.refresh(leo)
            db.query(Workout).filter(Workout.athlete_id.is_(None)).update(
                {"athlete_id": leo.id}, synchronize_session=False
            )
            db.commit()
    finally:
        db.close()


_init_db()

app = FastAPI(title="Exercícios")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ------------- helpers -------------

def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", ".")))
    except ValueError:
        return None


def get_active_athlete(request: Request, db: Session = Depends(get_db)) -> Athlete:
    aid = request.cookies.get("athlete_id")
    athlete = None
    if aid and aid.isdigit():
        athlete = db.query(Athlete).filter(Athlete.id == int(aid)).first()
    if not athlete:
        athlete = db.query(Athlete).order_by(Athlete.id).first()
    if not athlete:
        # fallback final (raro): cria um atleta vazio
        athlete = Athlete(name="Atleta", weight_kg=70.0)
        db.add(athlete)
        db.commit()
        db.refresh(athlete)
    return athlete


def get_all_athletes(db: Session) -> list[Athlete]:
    return db.query(Athlete).order_by(Athlete.id).all()


def _current_streak(active_dates: set[date], today: date) -> int:
    streak = 0
    d = today
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


# ------------- dashboard -------------

@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    range_: str = Query("1m", alias="range"),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    today = date.today()
    start, range_key = _resolve_range(range_, today)
    span_days = (today - start).days + 1
    bucket = "day" if span_days <= 90 else "week"

    aq = lambda q: q.filter(Workout.athlete_id == athlete.id)

    totals = {
        "corrida_km": aq(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "corrida").scalar() or 0.0,
        "natacao_km": aq(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "natacao").scalar() or 0.0,
        "musculacao_min": aq(db.query(func.coalesce(func.sum(Workout.duration_min), 0.0)))
        .filter(Workout.sport == "musculacao").scalar() or 0.0,
        "musculacao_sessoes": aq(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "musculacao").scalar() or 0,
        "calorias": aq(db.query(func.coalesce(func.sum(Workout.calories), 0.0))).scalar() or 0.0,
        "dias_ativos": aq(db.query(func.count(func.distinct(Workout.date)))).scalar() or 0,
        "total_treinos": aq(db.query(func.count(Workout.id))).scalar() or 0,
    }

    all_dates = {r[0] for r in aq(db.query(func.distinct(Workout.date))).all()}
    streak = _current_streak(all_dates, today)

    week_start = today - timedelta(days=today.weekday())
    week_rows = aq(
        db.query(Workout.sport, func.coalesce(func.sum(Workout.distance_km), 0.0),
                 func.coalesce(func.sum(Workout.duration_min), 0.0),
                 func.coalesce(func.sum(Workout.calories), 0.0),
                 func.count(Workout.id))
    ).filter(Workout.date >= week_start).group_by(Workout.sport).all()
    week = {"corrida_km": 0.0, "natacao_km": 0.0, "musculacao_min": 0.0,
            "musculacao_sessoes": 0, "calorias": 0.0}
    for sport, km, dur, cal, count in week_rows:
        if sport == "corrida":
            week["corrida_km"] = float(km or 0)
        elif sport == "natacao":
            week["natacao_km"] = float(km or 0)
        elif sport == "musculacao":
            week["musculacao_min"] = float(dur or 0)
            week["musculacao_sessoes"] = int(count or 0)
        week["calorias"] += float(cal or 0)

    range_rows = aq(
        db.query(
            Workout.date,
            func.coalesce(func.sum(Workout.calories), 0.0),
            func.count(Workout.id),
        )
    ).filter(Workout.date >= start, Workout.date <= today).group_by(Workout.date).all()
    by_day = {r[0]: {"cal": float(r[1] or 0), "count": int(r[2])} for r in range_rows}

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
        first_monday = start - timedelta(days=start.weekday())
        cur = first_monday
        while cur <= today:
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
            active_series.append(active_days)
            cur += timedelta(days=7)

    evo_rows = aq(
        db.query(Workout.date, Workout.sport, Workout.distance_km)
    ).filter(
        Workout.date >= start, Workout.date <= today,
        Workout.sport.in_(["corrida", "natacao"]),
    ).all()
    evo_labels = labels
    corrida_evo = [0.0] * len(labels)
    natacao_evo = [0.0] * len(labels)
    if bucket == "day":
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

    breakdown_rows = aq(
        db.query(Workout.sport, func.count(Workout.id))
    ).filter(Workout.date >= start, Workout.date <= today).group_by(Workout.sport).all()
    breakdown = {"corrida": 0, "natacao": 0, "musculacao": 0, "outro": 0}
    for sport, count in breakdown_rows:
        if sport in breakdown:
            breakdown[sport] = int(count)

    recent = (
        aq(db.query(Workout)).order_by(Workout.date.desc(), Workout.id.desc()).limit(10).all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
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


# ------------- treinos -------------

@app.get("/novo", response_class=HTMLResponse)
def new_form(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "today": date.today().isoformat(),
        },
    )


@app.post("/novo")
def create_workout(
    request: Request,
    sport: str = Form(...),
    workout_date: str = Form(...),
    distance_km: Optional[str] = Form(None),
    duration_min: Optional[str] = Form(None),
    calories: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if sport not in SPORTS:
        sport = "outro"
    try:
        d = date.fromisoformat(workout_date)
    except ValueError:
        d = date.today()

    athlete = get_active_athlete(request, db)
    dist = _to_float(distance_km)
    dur = _to_float(duration_min)
    cal = _to_float(calories)

    if cal is None:
        cal = estimate_calories(sport, athlete.weight_kg, dist, dur)

    workout = Workout(
        athlete_id=athlete.id,
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
def delete_workout(
    request: Request,
    workout_id: int,
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w:
        db.delete(w)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


# ------------- atletas -------------

@app.get("/atletas", response_class=HTMLResponse)
def athletes_page(request: Request, db: Session = Depends(get_db)):
    active = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "athletes.html",
        {
            "request": request,
            "athlete": active,
            "athletes": get_all_athletes(db),
        },
    )


@app.post("/atletas")
def athletes_create(
    name: str = Form(...),
    weight_kg: str = Form(...),
    height_cm: Optional[str] = Form(None),
    age: Optional[str] = Form(None),
    sex: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    w = _to_float(weight_kg) or 70.0
    a = Athlete(
        name=name.strip()[:80] or "Atleta",
        weight_kg=w,
        height_cm=_to_float(height_cm),
        age=_to_int(age),
        sex=(sex.upper() if sex in ("M", "F", "m", "f") else None),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    resp = RedirectResponse(url="/atletas", status_code=303)
    resp.set_cookie("athlete_id", str(a.id), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/atletas/{aid}/edit")
def athletes_edit(
    aid: int,
    name: str = Form(...),
    weight_kg: str = Form(...),
    height_cm: Optional[str] = Form(None),
    age: Optional[str] = Form(None),
    sex: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if a:
        a.name = name.strip()[:80] or a.name
        w = _to_float(weight_kg)
        if w and w > 0:
            a.weight_kg = w
        a.height_cm = _to_float(height_cm)
        a.age = _to_int(age)
        a.sex = sex.upper() if sex in ("M", "F", "m", "f") else None
        db.commit()
    return RedirectResponse(url="/atletas", status_code=303)


@app.post("/atletas/{aid}/select")
def athletes_select(aid: int, db: Session = Depends(get_db)):
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    resp = RedirectResponse(url="/", status_code=303)
    if a:
        resp.set_cookie("athlete_id", str(a.id), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/atletas/{aid}/delete")
def athletes_delete(request: Request, aid: int, db: Session = Depends(get_db)):
    total = db.query(Athlete).count()
    if total <= 1:
        # nunca apaga o último atleta
        return RedirectResponse(url="/atletas", status_code=303)
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if a:
        db.query(Workout).filter(Workout.athlete_id == aid).delete(synchronize_session=False)
        db.delete(a)
        db.commit()
    resp = RedirectResponse(url="/atletas", status_code=303)
    # se acabou de excluir o ativo, limpa o cookie
    if request.cookies.get("athlete_id") == str(aid):
        resp.delete_cookie("athlete_id")
    return resp


# ------------- config (redirect legado) -------------

@app.get("/config")
def config_redirect():
    return RedirectResponse(url="/atletas", status_code=307)


# ------------- API -------------

@app.get("/api/estimate")
def api_estimate(
    request: Request,
    sport: str,
    distance_km: Optional[float] = None,
    duration_min: Optional[float] = None,
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    kcal = estimate_calories(sport, athlete.weight_kg, distance_km, duration_min)
    return JSONResponse({
        "kcal": kcal,
        "weight_kg": athlete.weight_kg,
        "athlete": athlete.name,
    })
