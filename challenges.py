"""Desafios (estilo Strava): metas recorrentes semanais e mensais por esporte.

A pessoa aceita um desafio e o progresso é calculado a partir dos treinos do
período corrente (semana ISO ou mês).
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Workout
from metrics import SPORT_COLORS


@dataclass
class Challenge:
    code: str
    title: str
    sport: Optional[str]   # None = qualquer esporte
    metric: str            # 'distance' | 'duration' | 'count' | 'calories'
    target: float
    period: str            # 'week' | 'month'
    icon: str
    color: str
    unit: str              # 'km' | 'min' | 'treinos' | 'kcal'


def _c(code, title, sport, metric, target, period, icon, unit):
    color = SPORT_COLORS.get(sport, "#60a5fa")
    return Challenge(code, title, sport, metric, target, period, icon, color, unit)


# Catálogo de desafios. Semanais e mensais, cobrindo todos os esportes.
CHALLENGES = [
    # ---- semanais ----
    _c("run_w_25", "25 km de corrida", "corrida", "distance", 25, "week", "run", "km"),
    _c("swim_w_3", "3 km de natação", "natacao", "distance", 3, "week", "waves", "km"),
    _c("bike_w_80", "80 km de bike", "bike", "distance", 80, "week", "route", "km"),
    _c("trail_w_15", "15 km de trilha", "trilha", "distance", 15, "week", "trending-up", "km"),
    _c("gym_w_3", "3 treinos de musculação", "musculacao", "count", 3, "week", "dumbbell", "treinos"),
    _c("all_w_5", "5 treinos na semana", None, "count", 5, "week", "target", "treinos"),
    # ---- mensais ----
    _c("run_m_100", "100 km de corrida", "corrida", "distance", 100, "month", "run", "km"),
    _c("swim_m_10", "10 km de natação", "natacao", "distance", 10, "month", "waves", "km"),
    _c("bike_m_300", "300 km de bike", "bike", "distance", 300, "month", "route", "km"),
    _c("trail_m_50", "50 km de trilha", "trilha", "distance", 50, "month", "trending-up", "km"),
    _c("gym_m_12", "12 treinos de musculação", "musculacao", "count", 12, "month", "dumbbell", "treinos"),
    _c("all_m_20", "20 treinos no mês", None, "count", 20, "month", "target", "treinos"),
    _c("cal_m_15000", "15.000 kcal queimadas", None, "calories", 15000, "month", "flame", "kcal"),
]

CHALLENGES_BY_CODE = {c.code: c for c in CHALLENGES}


def period_key(period: str, today: date) -> str:
    if period == "month":
        return today.strftime("%Y-%m")
    iso = today.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def period_window(period: str, today: date) -> tuple[date, date]:
    if period == "month":
        return today.replace(day=1), today
    monday = today - timedelta(days=today.weekday())
    return monday, today


def period_label(period: str) -> str:
    return "mês" if period == "month" else "semana"


def progress(db: Session, athlete_id: int, ch: Challenge, today: date) -> float:
    start, end = period_window(ch.period, today)
    q = db.query(Workout).filter(
        Workout.athlete_id == athlete_id,
        Workout.date >= start, Workout.date <= end,
    )
    if ch.sport:
        q = q.filter(Workout.sport == ch.sport)
    if ch.metric == "distance":
        val = q.with_entities(func.coalesce(func.sum(Workout.distance_km), 0.0)).scalar()
    elif ch.metric == "duration":
        val = q.with_entities(func.coalesce(func.sum(Workout.duration_min), 0.0)).scalar()
    elif ch.metric == "calories":
        val = q.with_entities(func.coalesce(func.sum(Workout.calories), 0.0)).scalar()
    else:  # count
        val = q.count()
    return float(val or 0)


def _fmt(value: float, unit: str) -> str:
    if unit in ("treinos",):
        return f"{int(round(value))}"
    if unit == "kcal":
        return f"{int(round(value)):,}".replace(",", ".")
    # km / min com 1 casa quando faz sentido
    s = f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return s


def build(db: Session, athlete_id: int, today: date, joined: set) -> dict:
    """Monta as listas de desafios (semanais/mensais) com status e progresso.
    `joined` = set de (code, period_key) que o atleta aceitou."""
    weekly, monthly = [], []
    for ch in CHALLENGES:
        pk = period_key(ch.period, today)
        is_joined = (ch.code, pk) in joined
        prog = progress(db, athlete_id, ch, today) if is_joined else 0.0
        pct = min(100, round(prog / ch.target * 100)) if ch.target else 0
        item = {
            "ch": ch,
            "joined": is_joined,
            "progress": prog,
            "progress_fmt": _fmt(prog, ch.unit),
            "target_fmt": _fmt(ch.target, ch.unit),
            "pct": pct,
            "done": prog >= ch.target,
        }
        (monthly if ch.period == "month" else weekly).append(item)
    return {"weekly": weekly, "monthly": monthly}
