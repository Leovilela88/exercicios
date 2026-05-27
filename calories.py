"""Estimativa de calorias via METs (Compendium of Physical Activities).

kcal = MET * peso_kg * horas

Os valores de MET aqui são aproximações; o resultado tem ~10-15% de erro
em relação a métodos baseados em VO2/HR.
"""
from typing import Optional


def _running_met(speed_kmh: float) -> float:
    if speed_kmh < 6.4:
        return 6.0
    if speed_kmh < 8.0:
        return 8.3
    if speed_kmh < 9.7:
        return 9.8
    if speed_kmh < 11.3:
        return 11.0
    if speed_kmh < 12.9:
        return 11.8
    if speed_kmh < 14.5:
        return 12.8
    return 14.5


def _swim_met(speed_kmh: float) -> float:
    # crawl: leve ~MET 5.8, moderado ~8.3, vigoroso ~10
    if speed_kmh < 2.0:
        return 5.8
    if speed_kmh < 3.0:
        return 8.3
    return 10.0


def estimate_calories(
    sport: str,
    weight_kg: float,
    distance_km: Optional[float] = None,
    duration_min: Optional[float] = None,
) -> Optional[float]:
    """Retorna kcal estimadas, ou None se não há dados suficientes."""
    if not weight_kg or weight_kg <= 0:
        return None
    if not distance_km and not duration_min:
        return None

    # Inferir duração quando só temos distância (estima ritmo padrão por esporte)
    default_speed = {"corrida": 10.0, "natacao": 2.5, "outro": 5.0}.get(sport, 5.0)

    if duration_min and duration_min > 0:
        hours = duration_min / 60.0
        speed_kmh = (distance_km / hours) if distance_km else default_speed
    else:
        speed_kmh = default_speed
        hours = (distance_km / speed_kmh) if distance_km else 0.0

    if hours <= 0:
        return None

    if sport == "corrida":
        met = _running_met(speed_kmh)
    elif sport == "natacao":
        met = _swim_met(speed_kmh)
    else:
        met = 5.0  # exercício geral moderado

    return round(met * weight_kg * hours, 0)
