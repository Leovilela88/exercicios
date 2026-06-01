"""Conquistas (badges) derivadas dos treinos — calculadas on-the-fly."""
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

import stats
from models import Workout


@dataclass(frozen=True)
class Badge:
    id: str
    icon: str         # nome do ícone SVG (ver templates/_macros.html)
    color: str        # cor do ícone/acento
    title: str
    desc: str
    metric: str       # chave em `agg` para medir progresso
    target: float


# metric: chave do dicionário de agregados calculado em evaluate()
BADGES = [
    # Corrida acumulada
    Badge("run10", "run", "#60a5fa", "Aquecendo", "10 km de corrida acumulados", "run_km", 10),
    Badge("run100", "trophy", "#60a5fa", "Centurião", "100 km de corrida acumulados", "run_km", 100),
    Badge("run500", "trophy", "#fbbf24", "Maratonista de elite", "500 km de corrida acumulados", "run_km", 500),
    # Natação acumulada
    Badge("swim10", "waves", "#22d3ee", "Peixe", "10 km nadados", "swim_km", 10),
    Badge("swim50", "waves", "#06b6d4", "Golfinho", "50 km nadados", "swim_km", 50),
    # Maior corrida única (distância de um treino)
    Badge("long5", "target", "#34d399", "Primeiros 5K", "Uma corrida de 5 km", "max_run", 5),
    Badge("long10", "target", "#60a5fa", "Dez de uma vez", "Uma corrida de 10 km", "max_run", 10),
    Badge("half", "medal", "#cbd5e1", "Meia-maratona", "Uma corrida de 21 km", "max_run", 21.0975),
    Badge("full", "medal", "#fbbf24", "Maratona", "Uma corrida de 42 km", "max_run", 42.195),
    # Sequência
    Badge("streak7", "flame", "#fb923c", "Uma semana firme", "7 dias seguidos treinando", "max_streak", 7),
    Badge("streak30", "flame", "#f87171", "Imparável", "30 dias seguidos treinando", "max_streak", 30),
    # Volume de treinos
    Badge("count50", "dumbbell", "#c084fc", "Constante", "50 treinos registrados", "count", 50),
    Badge("count200", "star", "#a855f7", "Veterano", "200 treinos registrados", "count", 200),
    # Energia
    Badge("cal50k", "zap", "#fb923c", "Usina", "50.000 kcal queimadas", "cal", 50000),
]

BADGES_BY_ID = {b.id: b for b in BADGES}


def evaluate(db: Session, athlete_id: int) -> dict:
    """Retorna {unlocked: [...], locked: [...], count, total} com progresso."""
    base = db.query(Workout).filter(Workout.athlete_id == athlete_id)

    def _sum_km(sport):
        return float(base.filter(Workout.sport == sport).with_entities(
            func.coalesce(func.sum(Workout.distance_km), 0.0)).scalar() or 0)

    agg = {
        "run_km": _sum_km("corrida"),
        "swim_km": _sum_km("natacao"),
        "max_run": float(base.filter(Workout.sport == "corrida").with_entities(
            func.coalesce(func.max(Workout.distance_km), 0.0)).scalar() or 0),
        "count": int(base.with_entities(func.count(Workout.id)).scalar() or 0),
        "cal": float(base.with_entities(
            func.coalesce(func.sum(Workout.calories), 0.0)).scalar() or 0),
        "max_streak": stats._max_streak(db, athlete_id),
    }

    unlocked, locked = [], []
    for b in BADGES:
        value = agg.get(b.metric, 0)
        pct = min(100, round(value / b.target * 100)) if b.target else 0
        item = {"badge": b, "value": value, "pct": pct, "done": value >= b.target}
        (unlocked if item["done"] else locked).append(item)

    # locked ordenado por quem está mais perto
    locked.sort(key=lambda x: -x["pct"])
    return {
        "unlocked": unlocked,
        "locked": locked,
        "count": len(unlocked),
        "total": len(BADGES),
    }
