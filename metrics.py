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
