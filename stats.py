"""Estatísticas de engajamento: metas, recordes e comparativos de período."""
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
    return {
        "goal": goal,
        "value": value,
        "target": target,
        "pct": pct,
        "done": value >= target > 0,
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
