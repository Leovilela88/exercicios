"""Parser do export de dados da Garmin (summarizedActivities.json).

O "Exportar seus dados" da Garmin gera um zip com um arquivo
`*_summarizedActivities.json` contendo um array de atividades. As unidades
costumam ser: distância em centímetros, duração em milissegundos, timestamps
em milissegundos. Como o formato varia entre contas, o parser é tolerante.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from strava_import import ParsedWorkout

# activityType (Garmin) -> nosso esporte. Casamos por substring (lowercase).
_SPORT_RULES = [
    ("swim", "natacao"),
    ("run", "corrida"),
    ("treadmill", "corrida"),
    ("cycl", "bike"),
    ("bik", "bike"),
    ("ride", "bike"),
    ("hik", "trilha"),
    ("walk", "trilha"),
    ("strength", "musculacao"),
    ("weight", "musculacao"),
    ("gym", "musculacao"),
]


def map_sport(activity_type: str) -> str:
    t = (activity_type or "").lower()
    for needle, sport in _SPORT_RULES:
        if needle in t:
            return sport
    return "outro"


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _km(distance) -> Optional[float]:
    """Garmin guarda distância em centímetros."""
    d = _num(distance)
    if d is None:
        return None
    return round(d / 100000.0, 3)  # cm -> km


def _minutes(duration) -> Optional[float]:
    """Garmin guarda duração em milissegundos (heurística para segundos)."""
    d = _num(duration)
    if d is None:
        return None
    # > 50000 quase certamente é ms; senão tratamos como segundos
    return round(d / 60000.0, 1) if d > 50000 else round(d / 60.0, 1)


def _date(ts) -> Optional[date]:
    """Timestamp em ms (ou s) -> date."""
    n = _num(ts)
    if n is None:
        return None
    if n > 1e12:       # ms
        n = n / 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        return None


def _extract_activities(data) -> list:
    """Lida com as variações de estrutura do export."""
    if isinstance(data, dict):
        if "summarizedActivitiesExport" in data:
            return data["summarizedActivitiesExport"] or []
        # às vezes vem aninhado
        for v in data.values():
            if isinstance(v, list):
                return v
        return []
    if isinstance(data, list):
        acts = []
        for item in data:
            if isinstance(item, dict) and "summarizedActivitiesExport" in item:
                acts.extend(item["summarizedActivitiesExport"] or [])
            elif isinstance(item, dict) and (
                "activityType" in item or "activityId" in item):
                acts.append(item)
        return acts
    return []


@dataclass
class GarminResult:
    parsed: list
    total_rows: int
    skipped_bad_row: int
    ok: bool


def parse_garmin_json(content: bytes) -> GarminResult:
    try:
        data = json.loads(content.decode("utf-8-sig", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return GarminResult([], 0, 0, ok=False)

    acts = _extract_activities(data)
    if not isinstance(acts, list):
        return GarminResult([], 0, 0, ok=False)

    parsed: list = []
    bad = 0
    for a in acts:
        if not isinstance(a, dict):
            continue
        atype = a.get("activityType") or a.get("activityTypeDTO", {}).get("typeKey", "")
        if isinstance(atype, dict):
            atype = atype.get("typeKey", "")
        d = (_date(a.get("startTimeGmt")) or _date(a.get("startTimeLocal"))
             or _date(a.get("beginTimestamp")))
        if not d:
            bad += 1
            continue
        sport = map_sport(atype)
        dist = _km(a.get("distance"))
        # musculação/treinos sem deslocamento: ignora distância espúria
        if sport in ("musculacao", "outro"):
            dist = None
        dur = _minutes(a.get("duration") or a.get("elapsedDuration")
                       or a.get("movingDuration"))
        cal = _num(a.get("calories"))
        name = a.get("name") or ""
        parsed.append(ParsedWorkout(
            date=d, sport=sport, distance_km=dist,
            duration_min=dur, calories=cal, notes=(name or None),
        ))

    return GarminResult(parsed=parsed, total_rows=len(acts),
                        skipped_bad_row=bad, ok=True)
