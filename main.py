from datetime import date, timedelta
from typing import Optional

import io
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:  # Pillow é opcional — sem ele, fotos são salvas sem resize
    Image = ImageOps = None
    HAS_PIL = False

from calories import estimate_calories
from db import Base, SessionLocal, engine, get_db
from metrics import bmi, bmi_category, pace, sport_label
from models import Athlete, Goal, Settings, WeightLog, Workout
from strava_import import parse_strava_csv
import achievements
import stats

SPORTS = ("corrida", "natacao", "musculacao", "trilha", "bike", "outro")


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

    # Colunas de foto em athletes podem não existir em bases pré-migração.
    if "athletes" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("athletes")}
        photo_type = "BYTEA" if engine.dialect.name == "postgresql" else "BLOB"
        with engine.begin() as conn:
            if "photo" not in cols:
                conn.execute(text(f"ALTER TABLE athletes ADD COLUMN photo {photo_type}"))
            if "photo_mime" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN photo_mime VARCHAR(50)"))

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


def _ensure_pwa_icons() -> None:
    """Gera ícones PNG do app (192/512px) se não existirem.
    iOS Safari precisa de PNG para apple-touch-icon. No-op sem Pillow —
    os PNGs já vão commitados no repo, então isso é só um fallback."""
    if not HAS_PIL:
        return
    import os
    from PIL import ImageDraw

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    for size in (192, 512):
        path = os.path.join(static_dir, f"icon-{size}.png")
        if os.path.exists(path):
            continue
        img = Image.new("RGB", (size, size), (10, 16, 32))
        draw = ImageDraw.Draw(img)
        s = size / 512
        stroke = max(int(36 * s), 6)

        def line(x1, y1, x2, y2):
            draw.line([(int(x1 * s), int(y1 * s)), (int(x2 * s), int(y2 * s))],
                      fill=(96, 165, 250), width=stroke)

        line(138, 138, 138, 374)
        line(374, 138, 374, 374)
        line(104, 200, 104, 312)
        line(408, 200, 408, 312)
        line(138, 256, 374, 256)
        try:
            img.save(path, format="PNG", optimize=True)
        except Exception:
            pass


_ensure_pwa_icons()


app = FastAPI(title="Exercícios")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Helpers disponíveis nos templates Jinja
templates.env.globals["pace"] = pace
templates.env.globals["sport_label"] = sport_label
templates.env.globals["bmi"] = bmi
templates.env.globals["bmi_category"] = bmi_category


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
    ("all", "Total"),
]


def _resolve_range(
    range_key: str, today: date, earliest: Optional[date] = None
) -> tuple[date, str]:
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
    elif key == "all":
        start = earliest if earliest else today - timedelta(days=29)
    else:
        start = today - timedelta(days=29)
    return start, key


def _month_range(start: date, end: date):
    """Yield (primeiro_do_mes, ultimo_do_mes) inclusive de start até end."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = date(y, m, 1)
        if m == 12:
            next_first = date(y + 1, 1, 1)
        else:
            next_first = date(y, m + 1, 1)
        yield first, next_first - timedelta(days=1)
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


# ------------- dashboard -------------

@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    range_: str = Query("1m", alias="range"),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    today = date.today()
    earliest = (
        db.query(func.min(Workout.date))
        .filter(Workout.athlete_id == athlete.id)
        .scalar()
    )
    start, range_key = _resolve_range(range_, today, earliest=earliest)
    span_days = (today - start).days + 1
    if span_days <= 90:
        bucket = "day"
    elif span_days <= 365 * 2:  # até ~2 anos: semanal
        bucket = "week"
    else:
        bucket = "month"

    aq = lambda q: q.filter(Workout.athlete_id == athlete.id)

    # Filtros do período (mesmo recorte dos gráficos)
    period_filter = lambda q: q.filter(
        Workout.athlete_id == athlete.id,
        Workout.date >= start,
        Workout.date <= today,
    )

    totals = {
        "corrida_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "corrida").scalar() or 0.0,
        "natacao_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "natacao").scalar() or 0.0,
        "musculacao_min": period_filter(db.query(func.coalesce(func.sum(Workout.duration_min), 0.0)))
        .filter(Workout.sport == "musculacao").scalar() or 0.0,
        "musculacao_sessoes": period_filter(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "musculacao").scalar() or 0,
        "trilha_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "trilha").scalar() or 0.0,
        "trilha_sessoes": period_filter(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "trilha").scalar() or 0,
        "bike_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "bike").scalar() or 0.0,
        "bike_sessoes": period_filter(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "bike").scalar() or 0,
        "calorias": period_filter(db.query(func.coalesce(func.sum(Workout.calories), 0.0))).scalar() or 0.0,
        "dias_ativos": period_filter(db.query(func.count(func.distinct(Workout.date)))).scalar() or 0,
        "total_treinos": period_filter(db.query(func.count(Workout.id))).scalar() or 0,
    }

    # Streak permanece all-time (é uma métrica de "agora", não de período)
    all_dates = {stats._as_date(r[0]) for r in aq(db.query(Workout.date).distinct()).all()}
    streak = _current_streak(all_dates, today)

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
    elif bucket == "week":
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
    else:  # month
        for first, last in _month_range(start, today):
            cal_sum = 0.0
            active_days = 0
            for d, info in by_day.items():
                if first <= d <= min(last, today):
                    cal_sum += info["cal"]
                    if info["count"] > 0:
                        active_days += 1
            labels.append(first.strftime("%b/%y"))
            cal_series.append(cal_sum)
            active_series.append(active_days)

    evo_rows = aq(
        db.query(Workout.date, Workout.sport, Workout.distance_km, Workout.duration_min)
    ).filter(
        Workout.date >= start, Workout.date <= today,
        Workout.sport.in_(["corrida", "natacao", "trilha", "bike", "musculacao"]),
    ).all()
    evo_labels = labels
    corrida_evo = [0.0] * len(labels)
    natacao_evo = [0.0] * len(labels)
    trilha_evo = [0.0] * len(labels)
    bike_evo = [0.0] * len(labels)
    musculacao_evo = [0.0] * len(labels)  # em minutos
    km_series = {"corrida": corrida_evo, "natacao": natacao_evo,
                 "trilha": trilha_evo, "bike": bike_evo}

    def _bucket_idx(d: date) -> int:
        if bucket == "day":
            return (d - start).days
        if bucket == "week":
            first_monday = start - timedelta(days=start.weekday())
            return (d - first_monday).days // 7
        # month
        return (d.year - start.year) * 12 + (d.month - start.month)

    for d, sport, km, dur in evo_rows:
        idx = _bucket_idx(d)
        if not (0 <= idx < len(labels)):
            continue
        if sport == "musculacao":
            if dur:
                musculacao_evo[idx] += float(dur)
        elif km and sport in km_series:
            km_series[sport][idx] += float(km)

    breakdown_rows = aq(
        db.query(Workout.sport, func.count(Workout.id))
    ).filter(Workout.date >= start, Workout.date <= today).group_by(Workout.sport).all()
    breakdown = {"corrida": 0, "natacao": 0, "musculacao": 0, "trilha": 0, "bike": 0, "outro": 0}
    for sport, count in breakdown_rows:
        if sport in breakdown:
            breakdown[sport] = int(count)

    recent = (
        aq(db.query(Workout)).order_by(Workout.date.desc(), Workout.id.desc()).limit(10).all()
    )

    # Engajamento: comparativo, recordes, metas, heatmap, badges, tendência de pace
    comparison = stats.period_comparison(db, athlete.id, today)
    records = stats.personal_records(db, athlete.id)
    goals = (
        db.query(Goal).filter(Goal.athlete_id == athlete.id)
        .order_by(Goal.id).all()
    )
    goals_progress = [stats.goal_progress(db, athlete.id, g, today) for g in goals]
    calendars = stats.monthly_calendars(db, athlete.id, today)
    badges = achievements.evaluate(db, athlete.id)
    pace_run = stats.pace_trend(db, athlete.id, today, "corrida")
    pace_swim = stats.pace_trend(db, athlete.id, today, "natacao")
    insights = stats.insights(db, athlete.id, today)
    predictions = stats.race_predictions(db, athlete.id, today)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "totals": totals,
            "streak": streak,
            "comparison": comparison,
            "records": records,
            "goals_progress": goals_progress,
            "calendars": calendars,
            "badges": badges,
            "pace_run": pace_run,
            "pace_swim": pace_swim,
            "insights": insights,
            "predictions": predictions,
            "labels": labels,
            "cal_series": cal_series,
            "active_series": active_series,
            "evo_labels": evo_labels,
            "corrida_evo": corrida_evo,
            "natacao_evo": natacao_evo,
            "trilha_evo": trilha_evo,
            "bike_evo": bike_evo,
            "musculacao_evo": musculacao_evo,
            "bucket": bucket,
            "breakdown": breakdown,
            "recent": recent,
            "today": today.isoformat(),
            "range_options": RANGE_OPTIONS,
            "range_key": range_key,
        },
    )


# ------------- treinos -------------

def _parse_workout_form(
    sport: str, workout_date: str, distance_m: Optional[str],
    duration_min: Optional[str], calories: Optional[str], weight_kg: float,
) -> dict:
    """Normaliza os campos do form e calcula calorias quando vazio."""
    if sport not in SPORTS:
        sport = "outro"
    try:
        d = date.fromisoformat(workout_date)
    except ValueError:
        d = date.today()
    dist_m_val = _to_float(distance_m)
    dist = (dist_m_val / 1000.0) if dist_m_val else None
    dur = _to_float(duration_min)
    cal = _to_float(calories)
    if cal is None:
        cal = estimate_calories(sport, weight_kg, dist, dur)
    return {"date": d, "sport": sport, "distance_km": dist,
            "duration_min": dur, "calories": cal}


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
            "workout": None,
            "action": "/novo",
        },
    )


@app.post("/novo")
def create_workout(
    request: Request,
    sport: str = Form(...),
    workout_date: str = Form(...),
    distance_m: Optional[str] = Form(None),
    duration_min: Optional[str] = Form(None),
    calories: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    fields = _parse_workout_form(sport, workout_date, distance_m,
                                 duration_min, calories, athlete.weight_kg)
    db.add(Workout(athlete_id=athlete.id, notes=(notes or None), **fields))
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/treino/{workout_id}/editar", response_class=HTMLResponse)
def edit_form(request: Request, workout_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if not w:
        return RedirectResponse(url="/treinos", status_code=303)
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "today": date.today().isoformat(),
            "workout": w,
            "action": f"/treino/{w.id}/editar",
        },
    )


@app.post("/treino/{workout_id}/editar")
def edit_workout(
    request: Request,
    workout_id: int,
    sport: str = Form(...),
    workout_date: str = Form(...),
    distance_m: Optional[str] = Form(None),
    duration_min: Optional[str] = Form(None),
    calories: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w:
        fields = _parse_workout_form(sport, workout_date, distance_m,
                                     duration_min, calories, athlete.weight_kg)
        for k, v in fields.items():
            setattr(w, k, v)
        w.notes = notes or None
        db.commit()
    return RedirectResponse(url="/treinos", status_code=303)


@app.post("/delete/{workout_id}")
def delete_workout(
    request: Request,
    workout_id: int,
    redirect: str = Form("/"),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w:
        db.delete(w)
        db.commit()
    dest = redirect if redirect in ("/", "/treinos") else "/"
    return RedirectResponse(url=dest, status_code=303)


_BIKE_HINTS = ("ride", "bike", "pedal", "cicl", "bicicl")


@app.post("/admin/limpar-outro")
def admin_clean_outro(request: Request, db: Session = Depends(get_db)):
    """Reclassifica treinos 'outro' de ciclismo para 'bike' e remove o restante
    de 'outro' do atleta ativo. Operação pontual de limpeza pós-import."""
    athlete = get_active_athlete(request, db)
    outros = db.query(Workout).filter(
        Workout.athlete_id == athlete.id, Workout.sport == "outro"
    ).all()
    reclass, removed = 0, 0
    for w in outros:
        note = (w.notes or "").lower()
        if any(h in note for h in _BIKE_HINTS):
            w.sport = "bike"
            reclass += 1
        else:
            db.delete(w)
            removed += 1
    db.commit()
    return RedirectResponse(
        url=f"/treinos?cleaned_reclass={reclass}&cleaned_removed={removed}",
        status_code=303,
    )


PAGE_SIZE = 25


@app.get("/treinos", response_class=HTMLResponse)
def workouts_history(
    request: Request,
    sport: Optional[str] = None,
    page: int = 1,
    cleaned_reclass: Optional[int] = None,
    cleaned_removed: Optional[int] = None,
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    q = db.query(Workout).filter(Workout.athlete_id == athlete.id)
    if sport in SPORTS:
        q = q.filter(Workout.sport == sport)
    total = q.count()
    page = max(1, page)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = (
        q.order_by(Workout.date.desc(), Workout.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    )
    return templates.TemplateResponse(
        "workouts.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "sport": sport if sport in SPORTS else None,
            "sports": SPORTS,
            "cleaned_reclass": cleaned_reclass,
            "cleaned_removed": cleaned_removed,
        },
    )


# ------------- metas -------------

GOAL_METRICS = ("distance", "count", "duration", "calories")
GOAL_PERIODS = ("week", "month")


@app.get("/metas", response_class=HTMLResponse)
def goals_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    today = date.today()
    goals = (
        db.query(Goal).filter(Goal.athlete_id == athlete.id)
        .order_by(Goal.id).all()
    )
    progress = [stats.goal_progress(db, athlete.id, g, today) for g in goals]
    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "progress": progress,
            "sports": SPORTS,
            "metrics": stats.GOAL_METRICS,
            "periods": stats.GOAL_PERIODS,
        },
    )


@app.post("/metas")
def goals_create(
    request: Request,
    metric: str = Form(...),
    period: str = Form(...),
    target: str = Form(...),
    sport: str = Form(""),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    tgt = _to_float(target)
    sport_val = sport if sport in SPORTS else None
    # Distância não se aplica a esportes sem deslocamento (musculação/outro)
    if metric == "distance" and sport_val in ("musculacao", "outro"):
        metric = "duration"
    if metric in GOAL_METRICS and period in GOAL_PERIODS and tgt and tgt > 0:
        db.add(Goal(
            athlete_id=athlete.id,
            sport=sport_val,
            metric=metric, period=period, target=tgt,
        ))
        db.commit()
    return RedirectResponse(url="/metas", status_code=303)


@app.post("/metas/{goal_id}/delete")
def goals_delete(request: Request, goal_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    g = db.query(Goal).filter(
        Goal.id == goal_id, Goal.athlete_id == athlete.id
    ).first()
    if g:
        db.delete(g)
        db.commit()
    return RedirectResponse(url="/metas", status_code=303)


# ------------- ranking & conquistas -------------

@app.get("/ranking", response_class=HTMLResponse)
def ranking_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    athletes = get_all_athletes(db)
    today = date.today()
    rows = stats.ranking(db, athletes, today)
    return templates.TemplateResponse(
        "ranking.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": athletes,
            "rows": rows,
            "month_label": today.strftime("%B de %Y"),
        },
    )


@app.get("/conquistas", response_class=HTMLResponse)
def achievements_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    badges = achievements.evaluate(db, athlete.id)
    return templates.TemplateResponse(
        "achievements.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "badges": badges,
        },
    )


# ------------- peso -------------

@app.get("/peso", response_class=HTMLResponse)
def weight_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    logs = (
        db.query(WeightLog).filter(WeightLog.athlete_id == athlete.id)
        .order_by(WeightLog.date.asc(), WeightLog.id.asc()).all()
    )
    series = [{"date": l.date.isoformat(), "label": l.date.strftime("%d/%m/%y"),
               "weight": l.weight_kg, "id": l.id} for l in logs]
    first = logs[0].weight_kg if logs else None
    last = logs[-1].weight_kg if logs else None
    delta = round(last - first, 1) if (first is not None and last is not None) else None
    return templates.TemplateResponse(
        "weight.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "logs": list(reversed(logs)),
            "series": series,
            "delta": delta,
            "today": date.today().isoformat(),
        },
    )


@app.post("/peso")
def weight_create(
    request: Request,
    weight_kg: str = Form(...),
    log_date: str = Form(...),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = _to_float(weight_kg)
    try:
        d = date.fromisoformat(log_date)
    except ValueError:
        d = date.today()
    if w and w > 0:
        # um registro por dia: atualiza se já existe
        existing = db.query(WeightLog).filter(
            WeightLog.athlete_id == athlete.id, WeightLog.date == d
        ).first()
        if existing:
            existing.weight_kg = w
        else:
            db.add(WeightLog(athlete_id=athlete.id, date=d, weight_kg=w))
        # mantém o peso "atual" do atleta = registro mais recente
        latest = db.query(func.max(WeightLog.date)).filter(
            WeightLog.athlete_id == athlete.id
        ).scalar()
        if latest is None or d >= latest:
            athlete.weight_kg = w
        db.commit()
    return RedirectResponse(url="/peso", status_code=303)


@app.post("/peso/{log_id}/delete")
def weight_delete(request: Request, log_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    l = db.query(WeightLog).filter(
        WeightLog.id == log_id, WeightLog.athlete_id == athlete.id
    ).first()
    if l:
        db.delete(l)
        db.commit()
    return RedirectResponse(url="/peso", status_code=303)


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
            "db_dialect": engine.dialect.name,
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


@app.get("/atletas/{aid}/foto")
def athletes_photo(aid: int, db: Session = Depends(get_db)):
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if not a or not a.photo:
        raise HTTPException(status_code=404)
    return Response(content=a.photo, media_type=a.photo_mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/atletas/{aid}/foto")
async def athletes_photo_upload(
    aid: int, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if not a:
        return RedirectResponse(url="/atletas", status_code=303)
    raw = await file.read()
    if not raw or len(raw) > 8 * 1024 * 1024:  # 8 MB hard cap
        return RedirectResponse(url="/atletas", status_code=303)

    if HAS_PIL:
        # Caminho ideal: crop quadrado central + resize 512x512 JPEG (~20KB)
        try:
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)  # respeita orientação EXIF do celular
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((512, 512), Image.LANCZOS)
            if img.mode in ("RGBA", "P", "LA"):
                bg = Image.new("RGB", img.size, (10, 16, 32))
                bg.paste(img, mask=img.convert("RGBA").split()[-1] if img.mode != "P" else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            a.photo = out.getvalue()
            a.photo_mime = "image/jpeg"
            db.commit()
        except Exception:
            pass
    else:
        # Fallback sem Pillow: salva o original (limitado a 2 MB para não estourar
        # o banco) com o mime informado pelo navegador.
        if len(raw) <= 2 * 1024 * 1024 and (file.content_type or "").startswith("image/"):
            a.photo = raw
            a.photo_mime = file.content_type
            db.commit()
    return RedirectResponse(url="/atletas", status_code=303)


@app.post("/atletas/{aid}/foto/delete")
def athletes_photo_delete(aid: int, db: Session = Depends(get_db)):
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if a:
        a.photo = None
        a.photo_mime = None
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


# ------------- importar -------------

@app.get("/importar", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "import.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "result": None,
        },
    )


@app.post("/importar", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    content = await file.read()

    if len(content) > 10 * 1024 * 1024:  # 10 MB
        result = {"error": "Arquivo maior que 10 MB. Suba um CSV menor."}
        return templates.TemplateResponse(
            "import.html",
            {"request": request, "athlete": athlete,
             "athletes": get_all_athletes(db), "result": result},
        )

    parsed = parse_strava_csv(content)
    if not parsed.detected_columns.get("date") or not parsed.detected_columns.get("type"):
        result = {
            "error": ("Não consegui identificar as colunas 'Data' e 'Tipo de atividade' "
                      "no CSV. Confirma que é o activities.csv do export oficial do Strava?"),
            "headers_found": list(parsed.detected_columns.values()) or None,
        }
        return templates.TemplateResponse(
            "import.html",
            {"request": request, "athlete": athlete,
             "athletes": get_all_athletes(db), "result": result},
        )

    # Dedup: chave (date, sport, duração arredondada, distância arredondada)
    existing = db.query(
        Workout.date, Workout.sport, Workout.duration_min, Workout.distance_km
    ).filter(Workout.athlete_id == athlete.id).all()

    def key(d, sport, dur, dist):
        return (
            d,
            sport,
            round(dur, 0) if dur is not None else None,
            round(dist, 2) if dist is not None else None,
        )

    existing_keys = {key(*row) for row in existing}

    inserted = 0
    skipped_dup = 0
    by_sport: dict[str, int] = {}

    for pw in parsed.parsed:
        k = key(pw.date, pw.sport, pw.duration_min, pw.distance_km)
        if k in existing_keys:
            skipped_dup += 1
            continue

        cal = pw.calories
        if cal is None:
            cal = estimate_calories(pw.sport, athlete.weight_kg,
                                    pw.distance_km, pw.duration_min)

        db.add(Workout(
            athlete_id=athlete.id,
            date=pw.date,
            sport=pw.sport,
            distance_km=pw.distance_km,
            duration_min=pw.duration_min,
            calories=cal,
            notes=pw.notes,
        ))
        existing_keys.add(k)
        inserted += 1
        by_sport[pw.sport] = by_sport.get(pw.sport, 0) + 1

    db.commit()

    result = {
        "ok": True,
        "filename": file.filename,
        "total_rows": parsed.total_rows,
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "skipped_bad": parsed.skipped_bad_row,
        "by_sport": by_sport,
        "detected_columns": parsed.detected_columns,
    }
    return templates.TemplateResponse(
        "import.html",
        {"request": request, "athlete": athlete,
         "athletes": get_all_athletes(db), "result": result},
    )


# ------------- config (redirect legado) -------------

@app.get("/config")
def config_redirect():
    return RedirectResponse(url="/atletas", status_code=307)


# ------------- API -------------

@app.get("/api/db-info")
def api_db_info(db: Session = Depends(get_db)):
    """Diagnóstico: mostra qual banco está em uso e contagem de registros.
    Útil para confirmar se DATABASE_URL do Postgres está ativo no Railway."""
    dialect = engine.dialect.name
    # URL sem credenciais
    url = str(engine.url).split("@")[-1] if "@" in str(engine.url) else str(engine.url)
    return {
        "dialect": dialect,
        "persistente": dialect == "postgresql",
        "host": url,
        "athletes": db.query(func.count(Athlete.id)).scalar() or 0,
        "workouts": db.query(func.count(Workout.id)).scalar() or 0,
    }


@app.get("/api/estimate")
def api_estimate(
    request: Request,
    sport: str,
    distance_km: Optional[float] = None,
    distance_m: Optional[float] = None,
    duration_min: Optional[float] = None,
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    if distance_km is None and distance_m is not None:
        distance_km = distance_m / 1000.0
    kcal = estimate_calories(sport, athlete.weight_kg, distance_km, duration_min)
    return JSONResponse({
        "kcal": kcal,
        "weight_kg": athlete.weight_kg,
        "athlete": athlete.name,
    })
