"""Parser do CSV de export do Strava (activities.csv).

Aceita exports em pt-BR e en. Identifica colunas por nome normalizado
(sem acentos, lowercase, sem espaços/underline/ponto/hífen).
"""
import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from dateutil import parser as dtparser

# Strava em pt-BR usa abreviações com ponto. Traduzir para inglês deixa o
# dateutil parsear corretamente.
PT_MONTHS = {
    "jan": "Jan", "fev": "Feb", "mar": "Mar", "abr": "Apr",
    "mai": "May", "jun": "Jun", "jul": "Jul", "ago": "Aug",
    "set": "Sep", "out": "Oct", "nov": "Nov", "dez": "Dec",
}

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    for ch in " _.-/()":
        s = s.replace(ch, "")
    return s


# Possíveis nomes de coluna (já normalizados)
COL_ALIASES = {
    "date": [
        "activitydate", "datadaatividade", "datadaatividadeoriginal", "data",
        "datedaatividade",
    ],
    "type": ["activitytype", "tipodeatividade", "tipodaatividade", "tipo"],
    "duration_s": [
        "movingtime", "tempoemmovimento", "tempomovel", "duracaomovel",
        "tempodemovimento",
    ],
    "elapsed_s": ["elapsedtime", "tempodecorrido", "tempototal"],
    "distance": ["distance", "distancia"],
    "calories": ["calories", "calorias"],
    "name": ["activityname", "nomedaatividade", "nomedoatividade", "nome"],
}

# Strava activity type -> nosso sport
SPORT_MAP = {
    # corrida
    "run": "corrida",
    "trailrun": "corrida",
    "virtualrun": "corrida",
    "corrida": "corrida",
    "corridadetrilha": "corrida",
    "corridavirtual": "corrida",
    # natação
    "swim": "natacao",
    "natacao": "natacao",
    # musculação / treinamento de força
    "weighttraining": "musculacao",
    "workout": "musculacao",
    "crossfit": "musculacao",
    "musculacao": "musculacao",
    "treinamentodeforca": "musculacao",
    "treinodeforca": "musculacao",
    "treinodepeso": "musculacao",
    # trilha / caminhada (Hike, Walk, Trekking)
    "hike": "trilha",
    "walk": "trilha",
    "trilha": "trilha",
    "caminhada": "trilha",
    "trekking": "trilha",
    "caminhadaaoarlivre": "trilha",
    # bike / ciclismo (Ride, Mountain Bike, E-Bike, Virtual)
    "ride": "bike",
    "mountainbikeride": "bike",
    "gravelride": "bike",
    "ebikeride": "bike",
    "virtualride": "bike",
    "bike": "bike",
    "pedal": "bike",
    "pedalada": "bike",
    "ciclismo": "bike",
}


def map_sport(strava_type: str) -> str:
    return SPORT_MAP.get(_norm(strava_type), "outro")


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    s = value.strip()

    # 1) ISO: deixar dateutil parsear sem dayfirst (yearfirst implícito).
    if _ISO_RE.match(s):
        try:
            return dtparser.parse(s).date()
        except (ValueError, OverflowError):
            return None

    # 2) Normaliza pt-BR: "04 de ago. de 2024, 07:23:42" -> "04 Aug 2024 07:23:42"
    s_norm = s
    s_norm = re.sub(r"\s+de\s+", " ", s_norm, flags=re.IGNORECASE)
    s_norm = s_norm.replace(",", " ").replace(".", " ")
    s_norm = re.sub(r"\s+", " ", s_norm).strip()
    # Tradução de mês pt-BR -> en (case-insensitive, palavra inteira)
    def _repl(m):
        return PT_MONTHS.get(m.group(0).lower(), m.group(0))
    s_norm = re.sub(r"\b[A-Za-zÀ-ÿ]{3,}\b", _repl, s_norm)

    for kwargs in ({"dayfirst": True}, {"dayfirst": False}):
        try:
            return dtparser.parse(s_norm, fuzzy=True, **kwargs).date()
        except (ValueError, OverflowError, dtparser.ParserError):
            continue
    return None


def _to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("--", "n/a", "nan"):
        return None
    # Strava usa ponto como decimal mesmo em pt-BR, mas alguns clones usam vírgula
    s = s.replace(",", ".") if s.count(",") == 1 and s.count(".") == 0 else s
    try:
        return float(s)
    except ValueError:
        return None


def _km_from_distance(raw: Optional[float]) -> Optional[float]:
    """Strava exporta distância sempre em metros. Sempre dividimos por 1000."""
    if raw is None:
        return None
    return round(raw / 1000.0, 3)


@dataclass
class ParsedWorkout:
    date: date
    sport: str
    distance_km: Optional[float]
    duration_min: Optional[float]
    calories: Optional[float]
    notes: Optional[str]
    polyline: Optional[str] = None  # traçado GPS (encoded polyline), só no Strava API
    extra: Optional[dict] = None    # métricas extras (FC, elevação, etc.), só no Strava API


@dataclass
class ImportResult:
    parsed: list[ParsedWorkout]
    skipped_bad_row: int
    total_rows: int
    detected_columns: dict[str, str]  # logical name -> header real do CSV


def parse_strava_csv(content: bytes) -> ImportResult:
    # Decode tolerante a BOM
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))

    try:
        headers = next(reader)
    except StopIteration:
        return ImportResult([], 0, 0, {})

    headers_norm = {_norm(h): i for i, h in enumerate(headers)}

    col_idx: dict[str, int] = {}
    detected: dict[str, str] = {}
    for logical, aliases in COL_ALIASES.items():
        for alias in aliases:
            if alias in headers_norm:
                idx = headers_norm[alias]
                col_idx[logical] = idx
                detected[logical] = headers[idx]
                break

    # Mínimo necessário: date + type
    if "date" not in col_idx or "type" not in col_idx:
        return ImportResult([], 0, 0, detected)

    parsed: list[ParsedWorkout] = []
    bad = 0
    total = 0

    for row in reader:
        total += 1
        if not row or all(not c.strip() for c in row):
            continue

        def cell(key: str) -> str:
            i = col_idx.get(key)
            if i is None or i >= len(row):
                return ""
            return (row[i] or "").strip()

        d = _parse_date(cell("date"))
        if not d:
            bad += 1
            continue

        sport = map_sport(cell("type"))

        dist_raw = _to_float(cell("distance"))
        dist_km = _km_from_distance(dist_raw)

        # Duração em minutos: prefer moving time, senão elapsed.
        dur_s = _to_float(cell("duration_s")) if "duration_s" in col_idx else None
        if dur_s is None and "elapsed_s" in col_idx:
            dur_s = _to_float(cell("elapsed_s"))
        dur_min = round(dur_s / 60.0, 1) if dur_s and dur_s > 0 else None

        cal = _to_float(cell("calories")) if "calories" in col_idx else None
        if cal is not None and cal <= 0:
            cal = None

        name = cell("name") if "name" in col_idx else ""

        parsed.append(ParsedWorkout(
            date=d, sport=sport, distance_km=dist_km,
            duration_min=dur_min, calories=cal,
            notes=(name or None),
        ))

    return ImportResult(parsed=parsed, skipped_bad_row=bad,
                        total_rows=total, detected_columns=detected)
