"""Métricas derivadas: pace (ritmo), IMC e rótulos de esporte."""
from typing import Optional

# Rótulo + classe CSS + emoji por esporte (fonte única para os templates)
SPORT_LABELS = {
    "corrida": ("Corrida", "tag-run", "🏃"),
    "natacao": ("Natação", "tag-swim", "🏊"),
    "musculacao": ("Musculação", "tag-gym", "🏋️"),
    "trilha": ("Trilha", "tag-trail", "🥾"),
    "bike": ("Bike", "tag-bike", "🚴"),
    "outro": ("Outro", "tag-other", "💪"),
}

# Cor de acento por esporte (espelha as classes .tag-* do style.css)
SPORT_COLORS = {
    "corrida": "#60a5fa",
    "natacao": "#22d3ee",
    "musculacao": "#f87171",
    "trilha": "#a3e635",
    "bike": "#facc15",
    "outro": "#c084fc",
}


def sport_label(sport: str) -> str:
    return SPORT_LABELS.get(sport, SPORT_LABELS["outro"])[0]


def pace(sport: str, distance_km: Optional[float], duration_min: Optional[float]) -> Optional[str]:
    """Ritmo formatado. Corrida/trilha em min/km; natação em min/100m.
    Retorna None quando não se aplica ou faltam dados."""
    if not distance_km or not duration_min or distance_km <= 0 or duration_min <= 0:
        return None

    if sport in ("corrida", "trilha"):
        secs_per_km = (duration_min * 60.0) / distance_km
        m, s = divmod(int(round(secs_per_km)), 60)
        return f"{m}:{s:02d}/km"

    if sport == "natacao":
        hundreds = distance_km * 10.0  # km -> unidades de 100 m
        secs_per_100 = (duration_min * 60.0) / hundreds
        m, s = divmod(int(round(secs_per_100)), 60)
        return f"{m}:{s:02d}/100m"

    if sport == "bike":
        kmh = distance_km / (duration_min / 60.0)
        return f"{kmh:.1f} km/h"

    return None  # musculação / outro não têm pace


def bmi(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    if not weight_kg or not height_cm or height_cm <= 0:
        return None
    h = height_cm / 100.0
    return round(weight_kg / (h * h), 1)


def bmi_category(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value < 18.5:
        return "abaixo do peso"
    if value < 25:
        return "peso normal"
    if value < 30:
        return "sobrepeso"
    return "obesidade"


def _fmt_duration(duration_min: Optional[float]) -> Optional[str]:
    if not duration_min or duration_min <= 0:
        return None
    d = int(round(duration_min))
    return f"{d // 60}h{d % 60:02d}" if d >= 60 else f"{d} min"


def _fmt_distance(distance_km: Optional[float]) -> Optional[str]:
    if not distance_km or distance_km <= 0:
        return None
    s = f"{distance_km:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{s} km"


# métricas extras (vindas do Strava): chave -> (ícone, rótulo, formato)
EXTRA_DEFS = [
    ("hr_avg", "heart", "FC média", "{} bpm"),
    ("hr_max", "heart", "FC máx", "{} bpm"),
    ("elev", "mountain", "Elevação", "{} m"),
    ("speed_max", "speed", "Vel. máx", "{} km/h"),
    ("cadence", "pace", "Cadência", "{}"),
    ("watts", "bolt", "Potência", "{} W"),
]


def extra_metrics_list(extra: Optional[dict], exclude=()) -> list:
    """Transforma o dict de extras em lista de {icon, value, label} pra exibir.
    `exclude` = chaves a omitir (ex: cadência no card de compartilhar)."""
    if not extra:
        return []
    out = []
    for key, icon, label, fmt in EXTRA_DEFS:
        if key in exclude:
            continue
        v = extra.get(key)
        if v is not None:
            out.append({"icon": icon, "value": fmt.format(v), "label": label})
    return out


def workout_share(sport, distance_km, duration_min, calories,
                  date_label=None, volume=None, ex_count=None, polyline=None,
                  extra=None) -> dict:
    """Monta o payload do card de compartilhamento de um treino:
    rótulo do esporte, cor de acento e métricas (ícone + valor + rótulo)."""
    label = SPORT_LABELS.get(sport, SPORT_LABELS["outro"])[0]
    color = SPORT_COLORS.get(sport, SPORT_COLORS["outro"])
    p = pace(sport, distance_km, duration_min)
    dur, dist = _fmt_duration(duration_min), _fmt_distance(distance_km)
    cal = f"{calories:.0f} kcal" if calories else None

    if sport in ("corrida", "trilha"):
        raw = [("clock", dur, "Tempo"), ("distance", dist, "Distância"),
               ("pace", p, "Pace"), ("flame", cal, "Calorias")]
    elif sport == "natacao":
        raw = [("distance", dist, "Distância"), ("clock", dur, "Tempo"),
               ("pace", p, "Ritmo"), ("flame", cal, "Calorias")]
    elif sport == "bike":
        raw = [("distance", dist, "Distância"), ("clock", dur, "Tempo"),
               ("speed", p, "Velocidade"), ("flame", cal, "Calorias")]
    elif sport == "musculacao":
        vol = (f"{volume:,.0f}".replace(",", ".") + " kg") if volume else None
        raw = [("clock", dur, "Tempo"), ("volume", vol, "Volume"),
               ("count", str(ex_count) if ex_count else None, "Exercícios"),
               ("flame", cal, "Calorias")]
    else:
        raw = [("clock", dur, "Tempo"), ("distance", dist, "Distância"),
               ("flame", cal, "Calorias")]

    metrics = [{"icon": i, "value": v, "label": l} for (i, v, l) in raw if v]
    return {
        "type": "workout",
        "sportLabel": label,
        "dateLabel": date_label,
        "color": color,
        "metrics": metrics,
        "extras": extra_metrics_list(extra, exclude={"cadence"}),
        "route": polyline or None,
    }
