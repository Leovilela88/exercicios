from datetime import date, timedelta
from typing import Optional

import io
from dataclasses import dataclass
from typing import Callable
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
from models import Athlete, Settings, Workout
from strava_import import parse_strava_csv

SPORTS = ("corrida", "natacao", "musculacao", "trilha", "outro")


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
        "calorias": period_filter(db.query(func.coalesce(func.sum(Workout.calories), 0.0))).scalar() or 0.0,
        "dias_ativos": period_filter(db.query(func.count(func.distinct(Workout.date)))).scalar() or 0,
        "total_treinos": period_filter(db.query(func.count(Workout.id))).scalar() or 0,
    }

    # Streak permanece all-time (é uma métrica de "agora", não de período)
    all_dates = {r[0] for r in aq(db.query(func.distinct(Workout.date))).all()}
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
        Workout.sport.in_(["corrida", "natacao", "trilha", "musculacao"]),
    ).all()
    evo_labels = labels
    corrida_evo = [0.0] * len(labels)
    natacao_evo = [0.0] * len(labels)
    trilha_evo = [0.0] * len(labels)
    musculacao_evo = [0.0] * len(labels)  # em minutos

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
        else:
            if km:
                if sport == "corrida":
                    corrida_evo[idx] += float(km)
                elif sport == "natacao":
                    natacao_evo[idx] += float(km)
                elif sport == "trilha":
                    trilha_evo[idx] += float(km)

    breakdown_rows = aq(
        db.query(Workout.sport, func.count(Workout.id))
    ).filter(Workout.date >= start, Workout.date <= today).group_by(Workout.sport).all()
    breakdown = {"corrida": 0, "natacao": 0, "musculacao": 0, "trilha": 0, "outro": 0}
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
            "streak": streak,
            "labels": labels,
            "cal_series": cal_series,
            "active_series": active_series,
            "evo_labels": evo_labels,
            "corrida_evo": corrida_evo,
            "natacao_evo": natacao_evo,
            "trilha_evo": trilha_evo,
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
    distance_m: Optional[str] = Form(None),
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
    dist_m_val = _to_float(distance_m)
    dist = (dist_m_val / 1000.0) if dist_m_val else None
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
def athletes_page(
    request: Request,
    seeded: Optional[int] = None,
    kind: Optional[str] = None,
    db: Session = Depends(get_db),
):
    active = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "athletes.html",
        {
            "request": request,
            "athlete": active,
            "athletes": get_all_athletes(db),
            "seeded": seeded,
            "seed_kind": kind,
            "seed_types": SEED_TYPES,
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


# ------------- seed / agenda fixa -------------

@dataclass(frozen=True)
class SeedType:
    sport: str
    distance_km: Optional[float]
    days_back: int
    weekdays: frozenset       # int weekday() values
    duration_fn: Callable[[date], int]
    notes: str
    nome: str                 # rótulo do esporte, ex: "natação"
    icone: str                # emoji do botão
    descricao: str            # subtítulo / texto do alerta


# Lookup pra musculação: (weekday, semana_par/ímpar) -> minutos
_GYM_DUR = {(2, 0): 45, (2, 1): 50, (3, 0): 55, (3, 1): 60}

SEED_TYPES: dict[str, SeedType] = {
    "swim": SeedType(
        sport="natacao", distance_km=1.8,
        days_back=365, weekdays=frozenset({1, 3}),
        duration_fn=lambda _: 50,
        notes="Agenda fixa (terça/quinta)",
        nome="natação", icone="🏊",
        descricao="Terça e quinta · 50 min · 1800 m · 1 ano",
    ),
    "gym": SeedType(
        sport="musculacao", distance_km=None,
        days_back=120, weekdays=frozenset({2, 3}),
        duration_fn=lambda d: _GYM_DUR[(d.weekday(), d.isocalendar().week % 2)],
        notes="Agenda fixa (quarta/quinta)",
        nome="musculação", icone="🏋️",
        descricao="Quarta e quinta · 45–60 min · 4 meses",
    ),
}


def _seed_workouts(db: Session, athlete: Athlete, cfg: SeedType) -> int:
    """Idempotente por data. duration_fn é determinístico — re-rodar não duplica."""
    today = date.today()
    start = today - timedelta(days=cfg.days_back)

    dist_filter = (Workout.distance_km.is_(None) if cfg.distance_km is None
                   else Workout.distance_km == cfg.distance_km)
    existing = {
        d for (d,) in db.query(Workout.date).filter(
            Workout.athlete_id == athlete.id,
            Workout.sport == cfg.sport,
            dist_filter,
        ).all()
    }

    kcal_cache: dict[int, Optional[float]] = {}
    inserted = 0
    d = start
    while d <= today:
        if d.weekday() in cfg.weekdays and d not in existing:
            dur = cfg.duration_fn(d)
            if dur not in kcal_cache:
                kcal_cache[dur] = estimate_calories(
                    cfg.sport, athlete.weight_kg, cfg.distance_km, dur
                )
            db.add(Workout(
                athlete_id=athlete.id, date=d, sport=cfg.sport,
                distance_km=cfg.distance_km, duration_min=dur,
                calories=kcal_cache[dur], notes=cfg.notes,
            ))
            inserted += 1
        d += timedelta(days=1)
    db.commit()
    return inserted


@app.post("/admin/seed/{kind}")
def admin_seed(kind: str, request: Request, db: Session = Depends(get_db)):
    cfg = SEED_TYPES.get(kind)
    if not cfg:
        raise HTTPException(status_code=404, detail="Tipo de seed desconhecido")
    athlete = get_active_athlete(request, db)
    n = _seed_workouts(db, athlete, cfg)
    return RedirectResponse(url=f"/atletas?seeded={n}&kind={kind}", status_code=303)


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
