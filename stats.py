"""Estatísticas de engajamento: metas, recordes e comparativos de período."""
import calendar
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from metrics import (SPORT_COLORS, SPORT_LABELS, _fmt_distance, _fmt_duration,
                     pace, sport_label)
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


# ícone do card por esporte (precisa existir em static/share-card.js)
_SPORT_ICON = {
    "corrida": "run", "natacao": "waves", "musculacao": "dumbbell",
    "trilha": "mountain", "bike": "bike", "outro": "star",
}
# ordem de exibição no card
_SPORT_ORDER = ("corrida", "trilha", "bike", "natacao", "musculacao", "outro")


def period_share(db: Session, athlete_id: int, start: date, end: date,
                 period_label: str) -> dict:
    """Payload do card de compartilhamento de um período: totais gerais +
    desdobramento por esporte (distância e calorias), cada um na sua cor."""
    overall = _aggregate(db, athlete_id, start, end)

    sports = []
    for sport in _SPORT_ORDER:
        agg = _aggregate(db, athlete_id, start, end, sport=sport)
        if agg["count"] == 0:
            continue
        # métrica principal do esporte: distância (ou tempo, p/ musculação)
        if sport == "musculacao":
            main = _fmt_duration(agg["min"]) or "—"
        else:
            main = _fmt_distance(agg["km"]) or _fmt_duration(agg["min"]) or "—"
        sports.append({
            "icon": _SPORT_ICON.get(sport, "star"),
            "color": SPORT_COLORS.get(sport, SPORT_COLORS["outro"]),
            "label": SPORT_LABELS.get(sport, SPORT_LABELS["outro"])[0],
            "main": main,
            "cal": f"{agg['cal']:.0f} kcal" if agg["cal"] else "—",
            "count": agg["count"],
        })

    totals = []
    totals.append({"icon": "count", "value": str(overall["count"]), "label": "Treinos"})
    if overall["km"]:
        totals.append({"icon": "distance", "value": _fmt_distance(overall["km"]), "label": "Distância"})
    if overall["min"]:
        totals.append({"icon": "clock", "value": _fmt_duration(overall["min"]), "label": "Tempo"})
    if overall["cal"]:
        totals.append({"icon": "flame", "value": f"{overall['cal']:.0f} kcal", "label": "Calorias"})

    return {
        "type": "period",
        "periodLabel": period_label,
        "color": "#60a5fa",
        "totals": totals,
        "sports": sports,
    }


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

_MONTH_PT = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
             "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]


def _level(n: int) -> int:
    if n <= 0:
        return 0
    return min(n, 4)


def monthly_calendars(db: Session, athlete_id: int, today: date, months: int = 3) -> dict:
    """Mini-calendários mensais tradicionais (domingo→sábado) dos últimos N meses,
    com os dias coloridos por nº de treinos. Formato familiar para qualquer um."""
    # lista de (ano, mês), do mais antigo ao atual
    ym = []
    y, m = today.year, today.month
    for _ in range(months):
        ym.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    ym.reverse()

    start = date(ym[0][0], ym[0][1], 1)
    # Treinos do período, para contagem e para o detalhe ao clicar no dia
    wk_rows = (
        db.query(Workout.date, Workout.sport, Workout.distance_km,
                 Workout.duration_min, Workout.calories)
        .filter(Workout.athlete_id == athlete_id,
                Workout.date >= start, Workout.date <= today)
        .order_by(Workout.date, Workout.id).all()
    )
    counts: dict[date, int] = {}
    day_workouts: dict[str, list] = {}
    for d, sport, dist, dur, cal_ in wk_rows:
        dd = _as_date(d)
        counts[dd] = counts.get(dd, 0) + 1
        label, tag, _icon = SPORT_LABELS.get(sport, SPORT_LABELS["outro"])
        day_workouts.setdefault(dd.isoformat(), []).append({
            "sport": label,
            "tag": tag,
            "dist": round(dist, 2) if dist else None,
            "dur": round(dur) if dur else None,
            "cal": round(cal_) if cal_ else None,
        })

    cal = calendar.Calendar(firstweekday=6)  # 6 = domingo como 1ª coluna
    out_months = []
    for yy, mm in ym:
        weeks = []
        for week in cal.monthdatescalendar(yy, mm):
            cells = []
            for d in week:
                if d.month != mm:
                    cells.append(None)  # dia de outro mês (espaço vazio)
                    continue
                n = counts.get(d, 0)
                cells.append({
                    "day": d.day, "count": n, "level": _level(n),
                    "iso": d.isoformat(),
                    "is_today": d == today, "is_future": d > today,
                })
            weeks.append(cells)
        out_months.append({
            "name": f"{_MONTH_PT[mm]} {yy}",
            "weeks": weeks,
        })

    total = sum(counts.values())
    active_days = sum(1 for n in counts.values() if n > 0)
    return {
        "months": out_months,
        "weekdays": ["D", "S", "T", "Q", "Q", "S", "S"],
        "total": total,
        "active_days": active_days,
        "n_months": months,
        "day_workouts": day_workouts,
    }


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


def _fmt_time(minutes: float) -> str:
    """Minutos decimais -> H:MM:SS ou MM:SS."""
    total = int(round(minutes * 60))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def race_predictions(db: Session, athlete_id: int, today: date) -> Optional[dict]:
    """Previsão de tempos de prova (Riegel) a partir da melhor corrida recente.
    T2 = T1 * (D2/D1) ** 1.06"""
    cutoff = today - timedelta(days=120)
    rows = (
        db.query(Workout)
        .filter(Workout.athlete_id == athlete_id, Workout.sport == "corrida",
                Workout.date >= cutoff,
                Workout.distance_km >= 3, Workout.duration_min.isnot(None),
                Workout.duration_min > 0)
        .all()
    )
    if not rows:
        return None
    # melhor desempenho = menor pace (seg/km)
    best = min(rows, key=lambda w: w.duration_min * 60 / w.distance_km)
    d1, t1 = best.distance_km, best.duration_min  # km, minutos

    preds = []
    for label, dist in [("5K", 5.0), ("10K", 10.0),
                        ("Meia (21K)", 21.0975), ("Maratona (42K)", 42.195)]:
        t2 = t1 * (dist / d1) ** 1.06
        secs_per_km = t2 * 60 / dist
        m, s = divmod(int(round(secs_per_km)), 60)
        preds.append({
            "label": label,
            "time": _fmt_time(t2),
            "pace": f"{m}:{s:02d}/km",
            "is_base": abs(dist - d1) < 0.4,
        })
    return {
        "base_km": round(d1, 2),
        "base_time": _fmt_time(t1),
        "base_date": best.date,
        "preds": preds,
    }


_RACE_MIN_KM = {"corrida": 3.0, "trilha": 3.0, "natacao": 0.3, "bike": 5.0}


def predict_race_time(db: Session, athlete_id: int, today: date,
                      distance_km: float, sport: str) -> Optional[dict]:
    """Previsão (Riegel) do tempo para uma prova de distância `distance_km`,
    a partir da melhor performance recente do atleta naquele esporte."""
    if not distance_km or distance_km <= 0:
        return None
    min_km = _RACE_MIN_KM.get(sport)
    if min_km is None:
        return None  # esporte sem previsão de tempo (musculação etc.)
    cutoff = today - timedelta(days=120)
    rows = (
        db.query(Workout).filter(
            Workout.athlete_id == athlete_id, Workout.sport == sport,
            Workout.date >= cutoff, Workout.distance_km >= min_km,
            Workout.duration_min.isnot(None), Workout.duration_min > 0,
        ).all()
    )
    if not rows:
        return None
    unit = (lambda km: km if sport in ("corrida", "trilha") else km * 10.0)
    best = min(rows, key=lambda w: w.duration_min / unit(w.distance_km))
    d1, t1 = best.distance_km, best.duration_min
    t2 = t1 * (distance_km / d1) ** 1.06  # minutos
    secs_per = t2 * 60 / unit(distance_km)
    m, s = divmod(int(round(secs_per)), 60)
    suffix = "/km" if sport in ("corrida", "trilha") else ("/100m" if sport == "natacao" else "/km")
    return {"time": _fmt_time(t2), "pace": f"{m}:{s:02d}{suffix}"}


_WD_PT = ["segundas", "terças", "quartas", "quintas", "sextas", "sábados", "domingos"]


def insights(db: Session, athlete_id: int, today: date) -> list[dict]:
    """Frases automáticas geradas dos dados — um 'coach' simples."""
    out = []

    # 1) sequência atual
    cs = current_streak(db, athlete_id, today)
    if cs >= 3:
        out.append({"icon": "flame", "color": "#fb923c",
                    "text": f"Você está há {cs} dias seguidos treinando. Mantenha o ritmo!"})

    # 2) dia da semana mais frequente
    wd_rows = db.query(Workout.date).filter(Workout.athlete_id == athlete_id).all()
    if len(wd_rows) >= 8:
        counts = [0] * 7
        for (d,) in wd_rows:
            counts[_as_date(d).weekday()] += 1
        top = max(range(7), key=lambda i: counts[i])
        if counts[top] > 0:
            out.append({"icon": "calendar", "color": "#60a5fa",
                        "text": f"Seu dia mais ativo são as {_WD_PT[top]} ({counts[top]} treinos)."})

    # 3) tendência de volume (últimos 30d vs 30 anteriores)
    cur = _aggregate(db, athlete_id, today - timedelta(days=29), today)
    prev = _aggregate(db, athlete_id, today - timedelta(days=59), today - timedelta(days=30))
    pc = _pct(cur["km"], prev["km"])
    if pc is not None and abs(pc) >= 10 and cur["km"] > 0:
        if pc > 0:
            out.append({"icon": "trending-up", "color": "#34d399",
                        "text": f"Seu volume subiu {pc}% vs os 30 dias anteriores ({cur['km']:.0f} km)."})
        else:
            out.append({"icon": "trending-down", "color": "#f87171",
                        "text": f"Seu volume caiu {abs(pc)}% vs os 30 dias anteriores. Bora retomar!"})

    # 4) tendência de pace na corrida
    pt = pace_trend(db, athlete_id, today, "corrida")
    if pt and pt["faster"] and abs(pt["pct"]) >= 3:
        out.append({"icon": "zap", "color": "#fbbf24",
                    "text": f"Você está {abs(pt['pct'])}% mais rápido na corrida que no mês passado."})

    return out[:4]


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
