"""Integração com a API do Strava (OAuth2 + import de atividades).

Usa só a biblioteca padrão (urllib) — sem dependências novas.
Credenciais vêm de variáveis de ambiente:
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from dateutil import parser as dtparser

from strava_import import ParsedWorkout

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

SCOPE = "read,activity:read_all"


def client_id() -> Optional[str]:
    return os.environ.get("STRAVA_CLIENT_ID")


def client_secret() -> Optional[str]:
    return os.environ.get("STRAVA_CLIENT_SECRET")


def is_configured() -> bool:
    return bool(client_id() and client_secret())


# ----------------------------------------------------------------- HTTP utils

def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get(url: str, params: dict, token: str, timeout: int = 30) -> list:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ----------------------------------------------------------------- OAuth

def authorize_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    return _post(TOKEN_URL, {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code": code,
        "grant_type": "authorization_code",
    })


def refresh_access_token(refresh_token: str) -> dict:
    return _post(TOKEN_URL, {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })


# ----------------------------------------------------------------- atividades

# Strava sport_type / type -> nosso esporte
def _map_sport(act: dict) -> str:
    t = (act.get("sport_type") or act.get("type") or "").lower()
    if "trail" in t or t == "hike":
        return "trilha"
    if "walk" in t:
        return "trilha"
    if "run" in t:
        return "corrida"
    if "swim" in t:
        return "natacao"
    if "ride" in t or "bike" in t or "cycl" in t:
        return "bike"
    if any(k in t for k in ("weight", "workout", "crossfit", "strength")):
        return "musculacao"
    return "outro"


def _extra_metrics(act: dict) -> Optional[dict]:
    """Métricas extras disponíveis na atividade (FC, elevação, cadência, etc.)."""
    e = {}
    if act.get("average_heartrate"):
        e["hr_avg"] = round(act["average_heartrate"])
    if act.get("max_heartrate"):
        e["hr_max"] = round(act["max_heartrate"])
    if act.get("total_elevation_gain"):
        e["elev"] = round(act["total_elevation_gain"])
    if act.get("max_speed"):
        e["speed_max"] = round(act["max_speed"] * 3.6, 1)  # m/s -> km/h
    if act.get("average_cadence"):
        e["cadence"] = round(act["average_cadence"])
    if act.get("average_watts"):
        e["watts"] = round(act["average_watts"])
    return e or None


def _to_workout(act: dict) -> Optional[ParsedWorkout]:
    try:
        ds = act.get("start_date_local") or act.get("start_date")
        d = dtparser.parse(ds).date() if ds else None
        if not d:
            return None
        dist_m = act.get("distance") or 0
        dist_km = round(dist_m / 1000.0, 3) if dist_m else None
        secs = act.get("moving_time") or act.get("elapsed_time") or 0
        dur_min = round(secs / 60.0, 1) if secs else None
        # a lista de atividades não traz calorias; deixamos None p/ estimar depois
        cal = act.get("calories")
        name = act.get("name")
        poly = (act.get("map") or {}).get("summary_polyline") or None
        return ParsedWorkout(
            date=d, sport=_map_sport(act), distance_km=dist_km,
            duration_min=dur_min, calories=cal, notes=(name or None),
            polyline=poly, extra=_extra_metrics(act),
        )
    except Exception:
        return None


def fetch_activities(access_token: str, after_epoch: Optional[int] = None,
                     max_pages: int = 20, per_page: int = 100,
                     timeout: int = 30) -> list[ParsedWorkout]:
    """Busca atividades paginando até esvaziar (ou max_pages). after_epoch
    limita ao que vier depois de um timestamp (import incremental)."""
    out: list[ParsedWorkout] = []
    page = 1
    while page <= max_pages:
        params = {"per_page": per_page, "page": page}
        if after_epoch:
            params["after"] = after_epoch
        batch = _get(ACTIVITIES_URL, params, access_token, timeout=timeout)
        if not batch:
            break
        for act in batch:
            pw = _to_workout(act)
            if pw:
                out.append(pw)
        if len(batch) < per_page:
            break
        page += 1
    return out
