"""Estatísticas de engajamento: metas, recordes e comparativos de período."""
import calendar
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from metrics import pace, sport_label
from models import Workout

# Metas: rótulos e unidades por métrica
GOAL_METRICS = {
    "distance": ("Distância", "km"),
    "count": ("Treinos", ""),
    "duration": ("Duração", "min"),
    "calories": ("Calorias", "kcal"),
}
GOAL_PERIODS = {"week": "na semana", "month": "no mês"}

# Pace só faz sentido acima de uma distância mínima (evita outliers de aquecimento)
_MIN_PACE_KM = {"corrida": 1.0, "trilha": 1.0, "natacao": 0.2}


# ------------- limites de período -------------

def week_bounds(today: date) -> tuple[date, date]:
    return today - timedelta(days=today.weekday()), today


def prev_week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=1)
    return start, today - timedelta(weeks=1)


def month_bounds(today: date) -> tuple[date, date]:
    return today.replace(day=1), today


def prev_month_bounds(today: date) -> tuple[date, date]:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    start_prev = last_prev.replace(day=1)
    # mesmo dia-do-mês que hoje, clampado ao tamanho do mês anterior
    return start_prev, start_prev.replace(day=min(today.day, last_prev.day))


# ------------- agregação -------------

def _aggregate(db: Session, athlete_id: int, start: date, end: date,
               sport: Optional[str] = None) -> dict:
    q = db.query(
        func.coalesce(func.sum(Workout.distance_km), 0.0),
        func.count(Workout.id),
        func.coalesce(func.sum(Workout.calories), 0.0),
        func.coalesce(func.sum(Workout.duration_min), 0.0),
    ).filter(
        Workout.athlete_id == athlete_id,
        Workout.date >= start, Workout.date <= end,
    )
    if sport:
        q = q.filter(Workout.sport == sport)
    row = q.one()
    return {"km": float(row[0] or 0), "count": int(row[1] or 0),
            "cal": float(row[2] or 0), "min": float(row[3] or 0)}


def _pct(cur: float, prev: float) -> Optional[float]:
    if prev <= 0:
        return None  # sem base de comparação
    return round((cur - prev) / prev * 100.0)


def period_comparison(db: Session, athlete_id: int, today: date) -> dict:
    """Semana e mês corrente (até hoje) vs mesmo ponto do período anterior."""
    out = {}
    for label, cur_fn, prev_fn in (
        ("week", week_bounds, prev_week_bounds),
        ("month", month_bounds, prev_month_bounds),
    ):
        cur = _aggregate(db, athlete_id, *cur_fn(today))
        prev = _aggregate(db, athlete_id, *prev_fn(today))
        out[label] = {
            "cur": cur, "prev": prev,
            "pct_km": _pct(cur["km"], prev["km"]),
            "pct_count": _pct(cur["count"], prev["count"]),
            "pct_cal": _pct(cur["cal"], prev["cal"]),
        }
    return out


# ------------- metas -------------

def goal_value(db: Session, athlete_id: int, goal, today: date) -> float:
    start, end = (week_bounds(today) if goal.period == "week"
                  else month_bounds(today))
    agg = _aggregate(db, athlete_id, start, end, sport=goal.sport)
    return {
        "distance": agg["km"],
        "count": float(agg["count"]),
        "duration": agg["min"],
        "calories": agg["cal"],
    }.get(goal.metric, 0.0)


def goal_progress(db: Session, athlete_id: int, goal, today: date) -> dict:
    value = goal_value(db, athlete_id, goal, today)
    target = goal.target or 0
    pct = min(100, round(value / target * 100)) if target > 0 else 0
    metric_label, unit = GOAL_METRICS.get(goal.metric, ("?", ""))
    scope = sport_label(goal.sport) if goal.sport else "Todos os esportes"
    done = value >= target > 0

    # Projeção: extrapola o ritmo atual até o fim do período
    if goal.period == "week":
        elapsed, total_days = today.weekday() + 1, 7
    else:
        elapsed = today.day
        total_days = calendar.monthrange(today.year, today.month)[1]
    projected = (value / elapsed * total_days) if elapsed > 0 else value
    on_track = projected >= target > 0

    return {
        "goal": goal,
        "value": value,
        "target": target,
        "pct": pct,
        "done": done,
        "projected": round(projected, 1),
        "on_track": on_track,
        "unit": unit,
        "metric_label": metric_label,
        "scope": scope,
        "period_label": GOAL_PERIODS.get(goal.period, goal.period),
    }


# ------------- recordes -------------

def personal_records(db: Session, athlete_id: int) -> dict:
    base = db.query(Workout).filter(Workout.athlete_id == athlete_id)

    longest_dist = (
        base.filter(Workout.distance_km.isnot(None))
        .order_by(Workout.distance_km.desc()).first()
    )
    longest_dur = (
        base.filter(Workout.duration_min.isnot(None))
        .order_by(Workout.duration_min.desc()).first()
    )

    best_pace = {}
    for sport, min_km in _MIN_PACE_KM.items():
        rows = (
            base.filter(
                Workout.sport == sport,
                Workout.distance_km >= min_km,
                Workout.duration_min.isnot(None),
                Workout.duration_min > 0,
            ).all()
        )
        best = None  # menor segundos/unidade = mais rápido
        for w in rows:
            secs = w.duration_min * 60.0 / (
                w.distance_km if sport in ("corrida", "trilha") else w.distance_km * 10.0
            )
            if best is None or secs < best[0]:
                best = (secs, w)
        if best:
            best_pace[sport] = {
                "pace": pace(sport, best[1].distance_km, best[1].duration_min),
                "date": best[1].date,
            }

    return {
        "longest_dist": longest_dist,
        "longest_dur": longest_dur,
        "best_pace": best_pace,
        "max_streak": _max_streak(db, athlete_id),
    }


def _as_date(v) -> date:
    """SQLite pode devolver datas como string; Postgres devolve date nativo."""
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    return v


def current_streak(db: Session, athlete_id: int, today: date) -> int:
    dates = {
        _as_date(d) for (d,) in db.query(Workout.date)
        .filter(Workout.athlete_id == athlete_id).distinct().all()
    }
    if not dates:
        return 0
    d = today
    if d not in dates and (d - timedelta(days=1)) in dates:
        d -= timedelta(days=1)
    streak = 0
    while d in dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


# ------------- mapa de calor -------------

def activity_heatmap(db: Session, athlete_id: int, today: date, weeks: int = 27) -> dict:
    """Grade tipo GitHub: colunas = semanas, linhas = dias (seg→dom).
    Nível 0–4 por nº de treinos no dia."""
    # começa na segunda-feira, `weeks` semanas atrás
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    rows = (
        db.query(Workout.date, func.count(Workout.id))
        .filter(Workout.athlete_id == athlete_id,
                Workout.date >= start, Workout.date <= today)
        .group_by(Workout.date).all()
    )
    counts = {_as_date(d): int(c) for d, c in rows}

    def level(n: int) -> int:
        if n <= 0:
            return 0
        if n == 1:
            return 1
        if n == 2:
            return 2
        if n == 3:
            return 3
        return 4

    grid = []  # lista de semanas; cada semana = lista de 7 células
    cur = start
    months = []  # rótulos de mês por coluna
    last_month = None
    while cur <= today:
        week_cells = []
        col_month = None
        for i in range(7):
            d = cur + timedelta(days=i)
            if d > today:
                week_cells.append(None)
                continue
            n = counts.get(d, 0)
            week_cells.append({"date": d, "count": n, "level": level(n)})
            if col_month is None:
                col_month = d.month
        # rótulo do mês: mostra quando muda
        if col_month and col_month != last_month:
            months.append(date(today.year, col_month, 1).strftime("%b"))
            last_month = col_month
        else:
            months.append("")
        grid.append(week_cells)
        cur += timedelta(weeks=1)

    total = sum(counts.values())
    active_days = sum(1 for n in counts.values() if n > 0)
    return {"grid": grid, "months": months, "total": total,
            "active_days": active_days, "weeks": len(grid)}


# ------------- ranking entre atletas -------------

def ranking(db: Session, athletes: list, today: date) -> list[dict]:
    """Placar do mês corrente (até hoje) por atleta."""
    start, end = month_bounds(today)
    out = []
    for a in athletes:
        agg = _aggregate(db, a.id, start, end)
        out.append({
            "athlete": a,
            "km": agg["km"],
            "count": agg["count"],
            "cal": agg["cal"],
            "min": agg["min"],
            "streak": current_streak(db, a.id, today),
        })
    out.sort(key=lambda x: (-x["km"], -x["count"]))
    for i, row in enumerate(out):
        row["pos"] = i + 1
    return out


# ------------- tendência de pace -------------

def _avg_pace_secs(db: Session, athlete_id: int, sport: str,
                   start: date, end: date) -> Optional[float]:
    """Segundos por km (corrida/trilha) ou por 100m (natação) no intervalo."""
    row = db.query(
        func.coalesce(func.sum(Workout.distance_km), 0.0),
        func.coalesce(func.sum(Workout.duration_min), 0.0),
    ).filter(
        Workout.athlete_id == athlete_id, Workout.sport == sport,
        Workout.date >= start, Workout.date <= end,
        Workout.distance_km.isnot(None), Workout.duration_min.isnot(None),
        Workout.distance_km > 0,
    ).one()
    km, minutes = float(row[0] or 0), float(row[1] or 0)
    if km <= 0 or minutes <= 0:
        return None
    unit = km if sport in ("corrida", "trilha") else km * 10.0
    return minutes * 60.0 / unit


def pace_trend(db: Session, athlete_id: int, today: date, sport: str = "corrida") -> Optional[dict]:
    """Compara pace médio do mês atual vs mês anterior. pct<0 = mais rápido."""
    cur = _avg_pace_secs(db, athlete_id, sport, *month_bounds(today))
    prev = _avg_pace_secs(db, athlete_id, sport, *prev_month_bounds(today))
    if cur is None or prev is None:
        return None
    pct = round((cur - prev) / prev * 100)

    def fmt(secs):
        m, s = divmod(int(round(secs)), 60)
        suffix = "/km" if sport in ("corrida", "trilha") else "/100m"
        return f"{m}:{s:02d}{suffix}"

    return {"cur": fmt(cur), "prev": fmt(prev), "pct": pct, "faster": cur < prev}


def _max_streak(db: Session, athlete_id: int) -> int:
    dates = sorted(
        _as_date(d) for (d,) in db.query(Workout.date)
        .filter(Workout.athlete_id == athlete_id).distinct().all()
    )
    if not dates:
        return 0
    best = run = 1
    for prev, cur in zip(dates, dates[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        best = max(best, run)
    return best
