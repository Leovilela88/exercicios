from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import io
import json
import os
import secrets
import time
import uuid
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:  # Pillow é opcional — sem ele, fotos são salvas sem resize
    Image = ImageOps = None
    HAS_PIL = False

from calories import estimate_calories
from db import Base, SessionLocal, engine, get_db
from metrics import bmi, bmi_category, pace, sport_label, workout_share
from models import (Athlete, ChallengeJoin, ExerciseEntry, Friendship, Goal,
                    Notification, Race, Routine, RoutineItem, Settings,
                    WeightLog, Workout)
from strava_import import parse_strava_csv
import achievements
import auth
import challenges
import notifications
import stats
import strava_api

SPORTS = ("corrida", "natacao", "musculacao", "trilha", "bike", "outro")

# Fuso do Brasil — o servidor roda em UTC, então datas/horas seguem São Paulo.
BR_TZ = ZoneInfo("America/Sao_Paulo")


def now_br() -> datetime:
    """Horário atual no fuso do Brasil (naive, para gravar/comparar no banco)."""
    return datetime.now(BR_TZ).replace(tzinfo=None)


def today_br() -> date:
    """Data de hoje no fuso do Brasil."""
    return datetime.now(BR_TZ).date()


def _init_db() -> None:
    """Cria tabelas e migra dados legados de Settings -> Athlete."""
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)

    # Coluna athlete_id pode não existir em bases pré-migração.
    if "workouts" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("workouts")}
        if "athlete_id" not in cols:
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    conn.execute(text(
                        "ALTER TABLE workouts ADD COLUMN athlete_id INTEGER "
                        "REFERENCES athletes(id)"
                    ))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_workouts_athlete_id "
                        "ON workouts(athlete_id)"
                    ))
                else:
                    conn.execute(text("ALTER TABLE workouts ADD COLUMN athlete_id INTEGER"))
        if "route_polyline" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE workouts ADD COLUMN route_polyline TEXT"))
        if "extra_json" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE workouts ADD COLUMN extra_json TEXT"))

    # Colunas de foto + conta em athletes podem não existir em bases pré-migração.
    if "athletes" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("athletes")}
        photo_type = "BYTEA" if engine.dialect.name == "postgresql" else "BLOB"
        with engine.begin() as conn:
            if "photo" not in cols:
                conn.execute(text(f"ALTER TABLE athletes ADD COLUMN photo {photo_type}"))
            if "photo_mime" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN photo_mime VARCHAR(50)"))
            if "username" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN username VARCHAR(30)"))
            if "password_hash" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN password_hash VARCHAR(255)"))
            if "friend_code" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN friend_code VARCHAR(12)"))
            if "is_admin" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN is_admin INTEGER DEFAULT 0"))
            if "last_seen_at" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN last_seen_at TIMESTAMP"))
            if "strava_athlete_id" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN strava_athlete_id VARCHAR(32)"))
            if "strava_access_token" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN strava_access_token VARCHAR(255)"))
            if "strava_refresh_token" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN strava_refresh_token VARCHAR(255)"))
            if "strava_expires_at" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN strava_expires_at INTEGER"))
            if "strava_last_sync_at" not in cols:
                conn.execute(text("ALTER TABLE athletes ADD COLUMN strava_last_sync_at TIMESTAMP"))

    # Coluna dispute_id em races (disputa entre amigos).
    if "races" in insp.get_table_names():
        rcols = {c["name"] for c in insp.get_columns("races")}
        if "dispute_id" not in rcols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE races ADD COLUMN dispute_id VARCHAR(40)"))

    # Coluna status em friendships (amizades antigas já contam como aceitas).
    if "friendships" in insp.get_table_names():
        fcols = {c["name"] for c in insp.get_columns("friendships")}
        if "status" not in fcols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE friendships ADD COLUMN status VARCHAR(10) DEFAULT 'accepted'"))
                conn.execute(text("UPDATE friendships SET status = 'accepted'"))

    db = SessionLocal()
    try:
        # Gera friend_code para atletas que ainda não têm.
        for a in db.query(Athlete).filter(Athlete.friend_code.is_(None)).all():
            code = auth.gen_friend_code()
            while db.query(Athlete).filter(Athlete.friend_code == code).first():
                code = auth.gen_friend_code()
            a.friend_code = code
        db.commit()

        if not db.query(Athlete).first():
            weight = 82.0
            try:
                legacy = db.query(Settings).filter(Settings.id == 1).first()
                if legacy and legacy.weight_kg:
                    weight = float(legacy.weight_kg)
            except Exception:
                pass
            leo = Athlete(name="Leonardo", weight_kg=weight,
                          friend_code=auth.gen_friend_code())
            db.add(leo)
            db.commit()
            db.refresh(leo)
            db.query(Workout).filter(Workout.athlete_id.is_(None)).update(
                {"athlete_id": leo.id}, synchronize_session=False
            )
            db.commit()
    finally:
        db.close()


_init_db()


def _ensure_pwa_icons() -> None:
    """Gera ícones PNG do app (192/512px) se não existirem.
    iOS Safari precisa de PNG para apple-touch-icon. No-op sem Pillow —
    os PNGs já vão commitados no repo, então isso é só um fallback."""
    if not HAS_PIL:
        return
    import os
    from PIL import ImageDraw

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    for size in (192, 512):
        path = os.path.join(static_dir, f"icon-{size}.png")
        if os.path.exists(path):
            continue
        img = Image.new("RGB", (size, size), (10, 16, 32))
        draw = ImageDraw.Draw(img)
        s = size / 512
        stroke = max(int(36 * s), 6)

        def line(x1, y1, x2, y2):
            draw.line([(int(x1 * s), int(y1 * s)), (int(x2 * s), int(y2 * s))],
                      fill=(96, 165, 250), width=stroke)

        line(138, 138, 138, 374)
        line(374, 138, 374, 374)
        line(104, 200, 104, 312)
        line(408, 200, 408, 312)
        line(138, 256, 374, 256)
        try:
            img.save(path, format="PNG", optimize=True)
        except Exception:
            pass


_ensure_pwa_icons()


app = FastAPI(title="Exercícios")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Caminhos que não exigem login.
PUBLIC_PREFIXES = ("/static", "/sw.js", "/offline", "/manifest.webmanifest",
                   "/entrar", "/registrar", "/sair", "/primeiro-acesso", "/healthz",
                   "/strava/status")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in PUBLIC_PREFIXES):
        resp = await call_next(request)
        # cache curto nos estáticos: evita o Cloudflare prender versão antiga por horas
        if path.startswith("/static"):
            resp.headers["Cache-Control"] = "public, max-age=300"
        return resp
    aid = request.session.get("athlete_id")
    if not aid:
        return RedirectResponse(url="/entrar", status_code=303)
    return await call_next(request)


# SECRET_KEY assina o cookie de sessão. Defina no Railway; sem ela, gera uma
# efêmera (as sessões caem a cada deploy, mas o app não quebra).
_SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET,
    https_only=False,
    same_site="lax",
    max_age=60 * 60 * 24 * 30,  # 30 dias
)

# Helpers disponíveis nos templates Jinja
templates.env.globals["pace"] = pace
templates.env.globals["sport_label"] = sport_label
templates.env.globals["workout_share"] = workout_share
templates.env.globals["bmi"] = bmi
templates.env.globals["bmi_category"] = bmi_category


def _from_json(s):
    try:
        return json.loads(s) if s else None
    except (ValueError, TypeError):
        return None


from metrics import extra_metrics_list as _extra_metrics_list  # noqa: E402
templates.env.filters["fromjson"] = _from_json
templates.env.globals["extra_metrics_list"] = _extra_metrics_list


# ------------- helpers -------------

def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", ".")))
    except ValueError:
        return None


def get_active_athlete(request: Request, db: Session = Depends(get_db)) -> Athlete:
    """Atleta logado (vem da sessão). O middleware require_login garante que
    há sessão nas rotas protegidas."""
    aid = request.session.get("athlete_id")
    athlete = db.query(Athlete).filter(Athlete.id == aid).first() if aid else None
    if not athlete:
        raise HTTPException(status_code=401)
    return athlete


def get_all_athletes(db: Session) -> list[Athlete]:
    return db.query(Athlete).order_by(Athlete.id).all()


def friend_ids(db: Session, athlete_id: int) -> list[int]:
    """IDs do atleta + amigos aceitos (para o ranking)."""
    rows = db.query(Friendship.friend_id).filter(
        Friendship.athlete_id == athlete_id, Friendship.status == "accepted"
    ).all()
    ids = {athlete_id} | {fid for (fid,) in rows}
    return list(ids)


def _touch_last_seen(db: Session, athlete: Athlete) -> None:
    athlete.last_seen_at = now_br()
    db.commit()


# ------------- autenticação / contas -------------

def _has_any_account(db: Session) -> bool:
    return db.query(Athlete).filter(Athlete.password_hash.isnot(None)).first() is not None


def _unique_friend_code(db: Session) -> str:
    code = auth.gen_friend_code()
    while db.query(Athlete).filter(Athlete.friend_code == code).first():
        code = auth.gen_friend_code()
    return code


@app.get("/entrar", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db), erro: Optional[str] = None):
    if request.session.get("athlete_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "first_access": not _has_any_account(db), "erro": erro},
    )


@app.post("/entrar")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    uname = auth.normalize_username(username)
    a = db.query(Athlete).filter(func.lower(Athlete.username) == uname).first()
    if not a or not a.password_hash or not auth.verify_password(password, a.password_hash):
        return RedirectResponse(url="/entrar?erro=1", status_code=303)
    request.session["athlete_id"] = a.id
    _touch_last_seen(db, a)
    return RedirectResponse(url="/", status_code=303)


@app.post("/primeiro-acesso")
def first_access_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    # Só funciona enquanto nenhuma conta tiver senha (reivindica a conta existente).
    if _has_any_account(db):
        return RedirectResponse(url="/entrar", status_code=303)
    uname = auth.normalize_username(username)
    if len(uname) < 3 or len(password) < 4:
        return RedirectResponse(url="/entrar?erro=2", status_code=303)
    # Reivindica o atleta principal (o mais antigo) — preserva todos os dados.
    a = db.query(Athlete).order_by(Athlete.id).first()
    if not a:
        a = Athlete(name="Atleta", weight_kg=70.0, friend_code=_unique_friend_code(db))
        db.add(a)
    a.username = uname
    a.password_hash = auth.hash_password(password)
    a.is_admin = 1
    if not a.friend_code:
        a.friend_code = _unique_friend_code(db)
    db.commit()
    request.session["athlete_id"] = a.id
    _touch_last_seen(db, a)
    return RedirectResponse(url="/", status_code=303)


@app.get("/registrar", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db), erro: Optional[str] = None):
    if request.session.get("athlete_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request, "erro": erro})


@app.post("/registrar")
def register_submit(
    request: Request,
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    uname = auth.normalize_username(username)
    nm = name.strip()[:80]
    if len(uname) < 3 or len(password) < 4 or not nm:
        return RedirectResponse(url="/registrar?erro=2", status_code=303)
    if not uname.replace("_", "").replace(".", "").isalnum():
        return RedirectResponse(url="/registrar?erro=3", status_code=303)
    if db.query(Athlete).filter(func.lower(Athlete.username) == uname).first():
        return RedirectResponse(url="/registrar?erro=4", status_code=303)
    a = Athlete(
        name=nm, weight_kg=70.0,
        username=uname,
        password_hash=auth.hash_password(password),
        friend_code=_unique_friend_code(db),
        is_admin=0,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    request.session["athlete_id"] = a.id
    _touch_last_seen(db, a)
    return RedirectResponse(url="/", status_code=303)


@app.post("/sair")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/entrar", status_code=303)


# ------------- amigos -------------

def _friends_ctx(request, me, db, **extra):
    # amigos aceitos
    arows = (
        db.query(Friendship, Athlete)
        .join(Athlete, Friendship.friend_id == Athlete.id)
        .filter(Friendship.athlete_id == me.id, Friendship.status == "accepted")
        .order_by(Athlete.name).all()
    )
    friends = [{"fid": f.id, "athlete": a} for (f, a) in arows]
    # pedidos recebidos (alguém me adicionou e aguarda)
    recv = (
        db.query(Friendship, Athlete)
        .join(Athlete, Friendship.athlete_id == Athlete.id)
        .filter(Friendship.friend_id == me.id, Friendship.status == "pending")
        .order_by(Friendship.created_at.desc()).all()
    )
    received = [{"fid": f.id, "athlete": a} for (f, a) in recv]
    # pedidos enviados (aguardando o outro aceitar)
    sent_rows = (
        db.query(Friendship, Athlete)
        .join(Athlete, Friendship.friend_id == Athlete.id)
        .filter(Friendship.athlete_id == me.id, Friendship.status == "pending")
        .order_by(Friendship.created_at.desc()).all()
    )
    sent = [{"fid": f.id, "athlete": a} for (f, a) in sent_rows]
    ctx = {"request": request, "athlete": me, "friends": friends,
           "received": received, "sent": sent,
           "erro": None, "ok": None, "pending": None}
    ctx.update(extra)
    return ctx


@app.get("/amigos", response_class=HTMLResponse)
def friends_page(request: Request, db: Session = Depends(get_db),
                 erro: Optional[str] = None, ok: Optional[str] = None):
    me = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "friends.html", _friends_ctx(request, me, db, erro=erro, ok=ok))


@app.post("/amigos/buscar", response_class=HTMLResponse)
def friends_lookup(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    """Procura pelo código e mostra o NOME pra confirmar antes de mandar o pedido."""
    me = get_active_athlete(request, db)
    target_code = auth.normalize_code(code)
    target = db.query(Athlete).filter(Athlete.friend_code == target_code).first()
    if not target:
        return RedirectResponse(url="/amigos?erro=naoachou", status_code=303)
    if target.id == me.id:
        return RedirectResponse(url="/amigos?erro=voce", status_code=303)
    exists = db.query(Friendship).filter(
        Friendship.athlete_id == me.id, Friendship.friend_id == target.id
    ).first()
    if exists:
        return RedirectResponse(url="/amigos?erro=jaadd", status_code=303)
    pending = {"name": target.name, "code": target.friend_code,
               "athlete_id": target.id}
    return templates.TemplateResponse(
        "friends.html", _friends_ctx(request, me, db, pending=pending))


@app.post("/amigos/adicionar")
def friends_add(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    """Envia um pedido de amizade (pendente até o outro aceitar)."""
    me = get_active_athlete(request, db)
    target_code = auth.normalize_code(code)
    target = db.query(Athlete).filter(Athlete.friend_code == target_code).first()
    if not target:
        return RedirectResponse(url="/amigos?erro=naoachou", status_code=303)
    if target.id == me.id:
        return RedirectResponse(url="/amigos?erro=voce", status_code=303)
    exists = db.query(Friendship).filter(
        Friendship.athlete_id == me.id, Friendship.friend_id == target.id
    ).first()
    if not exists:
        f = Friendship(athlete_id=me.id, friend_id=target.id, status="pending")
        db.add(f)
        db.commit()
        db.refresh(f)
        notifications.notify(
            db, target.id, "friend_request",
            f"{me.name} quer ser seu amigo", "Toque para aceitar ou recusar.",
            "/amigos", ref=f"req:{me.id}", action_id=f.id)
    return RedirectResponse(url=f"/amigos?ok={target.name}", status_code=303)


def _accept_friendship(db, me, f):
    """Aceita o pedido `f` (alguém -> me) e cria o vínculo recíproco."""
    f.status = "accepted"
    reverse = db.query(Friendship).filter(
        Friendship.athlete_id == me.id, Friendship.friend_id == f.athlete_id
    ).first()
    if reverse:
        reverse.status = "accepted"
    else:
        db.add(Friendship(athlete_id=me.id, friend_id=f.athlete_id, status="accepted"))
    db.commit()
    notifications.notify(
        db, f.athlete_id, "friend_accepted",
        f"{me.name} aceitou seu pedido de amizade", "Agora vocês competem no ranking!",
        "/ranking", ref=f"acc:{me.id}")


@app.post("/amigos/{fid}/aceitar")
def friends_accept(request: Request, fid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    f = db.query(Friendship).filter(
        Friendship.id == fid, Friendship.friend_id == me.id,
        Friendship.status == "pending",
    ).first()
    if f:
        _accept_friendship(db, me, f)
    return RedirectResponse(url=request.headers.get("referer", "/amigos"), status_code=303)


@app.post("/amigos/{fid}/recusar")
def friends_decline(request: Request, fid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    f = db.query(Friendship).filter(
        Friendship.id == fid, Friendship.friend_id == me.id,
        Friendship.status == "pending",
    ).first()
    if f:
        db.delete(f)
        db.commit()
    return RedirectResponse(url=request.headers.get("referer", "/amigos"), status_code=303)


@app.post("/amigos/{fid}/remover")
def friends_remove(request: Request, fid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    f = db.query(Friendship).filter(
        Friendship.id == fid, Friendship.athlete_id == me.id
    ).first()
    if f:
        # remove os dois lados do vínculo
        db.query(Friendship).filter(
            ((Friendship.athlete_id == me.id) & (Friendship.friend_id == f.friend_id)) |
            ((Friendship.athlete_id == f.friend_id) & (Friendship.friend_id == me.id))
        ).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse(url="/amigos", status_code=303)


# ------------- notificações -------------

@app.get("/api/notif-count")
def notif_count(request: Request, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    return JSONResponse({"unread": notifications.unread_count(db, me.id)})


@app.get("/notificacoes", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    items = (
        db.query(Notification).filter(Notification.athlete_id == me.id)
        .order_by(Notification.read, Notification.created_at.desc())
        .limit(60).all()
    )
    # pedidos de amizade ainda pendentes (para mostrar aceitar/recusar)
    pending_req_ids = {
        fid for (fid,) in db.query(Friendship.id).filter(
            Friendship.friend_id == me.id, Friendship.status == "pending")
    }
    rendered = templates.TemplateResponse(
        "notifications.html",
        {"request": request, "athlete": me, "items": items,
         "pending_req_ids": pending_req_ids},
    )
    # marca todas como lidas (depois de montar a página)
    db.query(Notification).filter(
        Notification.athlete_id == me.id, Notification.read == 0
    ).update({"read": 1}, synchronize_session=False)
    db.commit()
    return rendered


# ------------- perfil do amigo (resumido) -------------

@app.get("/amigo/{aid}", response_class=HTMLResponse)
def friend_profile(request: Request, aid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    # só dá pra ver o perfil de amigos aceitos
    if aid == me.id or aid not in friend_ids(db, me.id):
        raise HTTPException(status_code=403)
    friend = db.query(Athlete).filter(Athlete.id == aid).first()
    if not friend:
        raise HTTPException(status_code=404)
    today = today_br()
    earliest = db.query(func.min(Workout.date)).filter(
        Workout.athlete_id == aid).scalar() or today
    allt = stats._aggregate(db, aid, earliest, today)
    month_start = today.replace(day=1)
    month = stats._aggregate(db, aid, month_start, today)
    per_sport = []
    for sp in ("corrida", "natacao", "bike", "trilha"):
        km = stats._aggregate(db, aid, earliest, today, sport=sp)["km"]
        if km > 0:
            per_sport.append({"sport": sp, "label": sport_label(sp), "km": km})
    gym = stats._aggregate(db, aid, earliest, today, sport="musculacao")["count"]
    ev = achievements.evaluate(db, aid)
    summary = {
        "friend": friend,
        "total_workouts": allt["count"],
        "total_km": allt["km"],
        "total_cal": allt["cal"],
        "streak": stats.current_streak(db, aid, today),
        "month_km": month["km"], "month_count": month["count"],
        "per_sport": per_sport,
        "gym_count": gym,
        "trophies": ev["count"],
    }
    return templates.TemplateResponse(
        "friend_profile.html",
        {"request": request, "athlete": me, "p": summary,
         "records": stats.personal_records(db, aid),
         "badges_unlocked": ev["unlocked"]},
    )


# ------------- admin -------------

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db),
               reset_name: Optional[str] = None, reset_pwd: Optional[str] = None):
    me = get_active_athlete(request, db)
    if not me.is_admin:
        raise HTTPException(status_code=403)
    accounts = (
        db.query(Athlete).filter(Athlete.password_hash.isnot(None))
        .order_by(Athlete.created_at.desc().nullslast(), Athlete.id.desc()).all()
    )
    cutoff = now_br() - timedelta(minutes=30)
    today_start = now_br() - timedelta(hours=24)
    online = sum(1 for a in accounts if a.last_seen_at and a.last_seen_at >= cutoff)
    active_24h = sum(1 for a in accounts if a.last_seen_at and a.last_seen_at >= today_start)
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request, "athlete": me, "accounts": accounts,
            "total": len(accounts), "online": online, "active_24h": active_24h,
            "reset_name": reset_name, "reset_pwd": reset_pwd,
        },
    )


@app.post("/admin/{aid}/redefinir-senha")
def admin_reset_password(request: Request, aid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    if not me.is_admin:
        raise HTTPException(status_code=403)
    target = db.query(Athlete).filter(Athlete.id == aid).first()
    if not target or not target.password_hash:
        return RedirectResponse(url="/admin", status_code=303)
    temp = auth.gen_temp_password()
    target.password_hash = auth.hash_password(temp)
    db.commit()
    return RedirectResponse(
        url=f"/admin?reset_name={target.name}&reset_pwd={temp}", status_code=303)


def _current_streak(active_dates: set[date], today: date) -> int:
    streak = 0
    d = today
    if d not in active_dates and (d - timedelta(days=1)) in active_dates:
        d = d - timedelta(days=1)
    while d in active_dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


RANGE_OPTIONS = [
    ("1w", "1 semana"),
    ("1m", "1 mês"),
    ("3m", "3 meses"),
    ("6m", "6 meses"),
    ("1y", "1 ano"),
    ("ytd", "No ano"),
    ("all", "Total"),
]
# Rótulos curtos para a barra de períodos no celular (cabem sem rolagem)
RANGE_SHORT = {
    "1w": "1sem", "1m": "1mês", "3m": "3m", "6m": "6m",
    "1y": "1ano", "ytd": "ano", "all": "tudo",
}


def _resolve_range(
    range_key: str, today: date, earliest: Optional[date] = None
) -> tuple[date, str]:
    key = range_key if range_key in dict(RANGE_OPTIONS) else "1m"
    if key == "1w":
        start = today - timedelta(days=6)
    elif key == "1m":
        # "1 mês" = últimos 31 dias, pra um mês de 31 dias não deixar nenhum de fora.
        start = today - timedelta(days=31)
    elif key == "3m":
        start = today - timedelta(days=89)
    elif key == "6m":
        start = today - timedelta(days=179)
    elif key == "1y":
        start = today - timedelta(days=364)
    elif key == "ytd":
        start = date(today.year, 1, 1)
    elif key == "all":
        start = earliest if earliest else today - timedelta(days=29)
    else:
        start = today - timedelta(days=29)
    return start, key


def _month_range(start: date, end: date):
    """Yield (primeiro_do_mes, ultimo_do_mes) inclusive de start até end."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = date(y, m, 1)
        if m == 12:
            next_first = date(y + 1, 1, 1)
        else:
            next_first = date(y, m + 1, 1)
        yield first, next_first - timedelta(days=1)
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


# ------------- dashboard -------------

@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    range_: str = Query("1m", alias="range"),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    _maybe_strava_autosync(athlete, db)  # puxa treinos novos do Strava ao abrir
    today = today_br()
    earliest = (
        db.query(func.min(Workout.date))
        .filter(Workout.athlete_id == athlete.id)
        .scalar()
    )
    start, range_key = _resolve_range(range_, today, earliest=earliest)
    span_days = (today - start).days + 1
    if span_days <= 90:
        bucket = "day"
    elif span_days <= 365 * 2:  # até ~2 anos: semanal
        bucket = "week"
    else:
        bucket = "month"

    aq = lambda q: q.filter(Workout.athlete_id == athlete.id)

    # Filtros do período (mesmo recorte dos gráficos)
    period_filter = lambda q: q.filter(
        Workout.athlete_id == athlete.id,
        Workout.date >= start,
        Workout.date <= today,
    )

    totals = {
        "corrida_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "corrida").scalar() or 0.0,
        "natacao_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "natacao").scalar() or 0.0,
        "musculacao_min": period_filter(db.query(func.coalesce(func.sum(Workout.duration_min), 0.0)))
        .filter(Workout.sport == "musculacao").scalar() or 0.0,
        "musculacao_sessoes": period_filter(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "musculacao").scalar() or 0,
        "trilha_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "trilha").scalar() or 0.0,
        "trilha_sessoes": period_filter(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "trilha").scalar() or 0,
        "bike_km": period_filter(db.query(func.coalesce(func.sum(Workout.distance_km), 0.0)))
        .filter(Workout.sport == "bike").scalar() or 0.0,
        "bike_sessoes": period_filter(db.query(func.count(Workout.id)))
        .filter(Workout.sport == "bike").scalar() or 0,
        "calorias": period_filter(db.query(func.coalesce(func.sum(Workout.calories), 0.0))).scalar() or 0.0,
        "dias_ativos": period_filter(db.query(func.count(func.distinct(Workout.date)))).scalar() or 0,
        "total_treinos": period_filter(db.query(func.count(Workout.id))).scalar() or 0,
    }

    # Streak permanece all-time (é uma métrica de "agora", não de período)
    all_dates = {stats._as_date(r[0]) for r in aq(db.query(Workout.date).distinct()).all()}
    streak = _current_streak(all_dates, today)
    has_any_workout = bool(all_dates)

    range_rows = aq(
        db.query(
            Workout.date,
            func.coalesce(func.sum(Workout.calories), 0.0),
            func.count(Workout.id),
        )
    ).filter(Workout.date >= start, Workout.date <= today).group_by(Workout.date).all()
    by_day = {r[0]: {"cal": float(r[1] or 0), "count": int(r[2])} for r in range_rows}

    labels = []
    cal_series = []
    active_series = []
    if bucket == "day":
        days = (today - start).days + 1
        for i in range(days):
            d = start + timedelta(days=i)
            info = by_day.get(d, {"cal": 0.0, "count": 0})
            labels.append(d.strftime("%d/%m"))
            cal_series.append(info["cal"])
            active_series.append(1 if info["count"] > 0 else 0)
    elif bucket == "week":
        first_monday = start - timedelta(days=start.weekday())
        cur = first_monday
        while cur <= today:
            cal_sum = 0.0
            active_days = 0
            for j in range(7):
                d = cur + timedelta(days=j)
                if d < start or d > today:
                    continue
                info = by_day.get(d)
                if info:
                    cal_sum += info["cal"]
                    if info["count"] > 0:
                        active_days += 1
            labels.append(cur.strftime("%d/%m"))
            cal_series.append(cal_sum)
            active_series.append(active_days)
            cur += timedelta(days=7)
    else:  # month
        for first, last in _month_range(start, today):
            cal_sum = 0.0
            active_days = 0
            for d, info in by_day.items():
                if first <= d <= min(last, today):
                    cal_sum += info["cal"]
                    if info["count"] > 0:
                        active_days += 1
            labels.append(first.strftime("%b/%y"))
            cal_series.append(cal_sum)
            active_series.append(active_days)

    evo_rows = aq(
        db.query(Workout.date, Workout.sport, Workout.distance_km, Workout.duration_min)
    ).filter(
        Workout.date >= start, Workout.date <= today,
        Workout.sport.in_(["corrida", "natacao", "trilha", "bike", "musculacao"]),
    ).all()
    evo_labels = labels
    corrida_evo = [0.0] * len(labels)
    natacao_evo = [0.0] * len(labels)
    trilha_evo = [0.0] * len(labels)
    bike_evo = [0.0] * len(labels)
    musculacao_evo = [0.0] * len(labels)  # em minutos
    km_series = {"corrida": corrida_evo, "natacao": natacao_evo,
                 "trilha": trilha_evo, "bike": bike_evo}

    def _bucket_idx(d: date) -> int:
        if bucket == "day":
            return (d - start).days
        if bucket == "week":
            first_monday = start - timedelta(days=start.weekday())
            return (d - first_monday).days // 7
        # month
        return (d.year - start.year) * 12 + (d.month - start.month)

    for d, sport, km, dur in evo_rows:
        idx = _bucket_idx(d)
        if not (0 <= idx < len(labels)):
            continue
        if sport == "musculacao":
            if dur:
                musculacao_evo[idx] += float(dur)
        elif km and sport in km_series:
            km_series[sport][idx] += float(km)

    breakdown_rows = aq(
        db.query(Workout.sport, func.count(Workout.id))
    ).filter(Workout.date >= start, Workout.date <= today).group_by(Workout.sport).all()
    breakdown = {"corrida": 0, "natacao": 0, "musculacao": 0, "trilha": 0, "bike": 0, "outro": 0}
    for sport, count in breakdown_rows:
        if sport in breakdown:
            breakdown[sport] = int(count)

    recent = (
        aq(db.query(Workout)).order_by(Workout.date.desc(), Workout.id.desc()).limit(10).all()
    )

    # Engajamento: comparativo, recordes, metas, heatmap, badges, tendência de pace
    comparison = stats.period_comparison(db, athlete.id, today)
    records = stats.personal_records(db, athlete.id)
    goals = (
        db.query(Goal).filter(Goal.athlete_id == athlete.id)
        .order_by(Goal.id).all()
    )
    goals_progress = [stats.goal_progress(db, athlete.id, g, today) for g in goals]
    calendars = stats.monthly_calendars(db, athlete.id, today)
    badges = achievements.evaluate(db, athlete.id)
    # desafios aceitos (mostra as barras enchendo no dashboard)
    _ch_joins = db.query(ChallengeJoin).filter(ChallengeJoin.athlete_id == athlete.id).all()
    _ch_joined = {(j.code, j.period_key) for j in _ch_joins}
    _ch_data = challenges.build(db, athlete.id, today, _ch_joined)
    active_challenges = [it for it in (_ch_data["weekly"] + _ch_data["monthly"]) if it["joined"]]
    pace_run = stats.pace_trend(db, athlete.id, today, "corrida")
    pace_swim = stats.pace_trend(db, athlete.id, today, "natacao")
    insights = stats.insights(db, athlete.id, today)
    predictions = stats.race_predictions(db, athlete.id, today)
    next_race_row = (
        db.query(Race).filter(
            Race.athlete_id == athlete.id, Race.done == 0, Race.date >= today
        ).order_by(Race.date).first()
    )
    next_race = None
    if next_race_row:
        nr = next_race_row
        pred = (stats.predict_race_time(db, athlete.id, today, nr.distance_km, nr.sport)
                if nr.distance_km else None)
        next_race = {"r": nr, "days": (nr.date - today).days, "pred": pred}

    _PERIOD_LABELS = {
        "1w": "Última semana", "1m": "Último mês", "3m": "Últimos 3 meses",
        "6m": "Últimos 6 meses", "1y": "Último ano",
        "ytd": f"No ano de {today.year}", "all": "Desde o início",
    }
    share_period = stats.period_share(
        db, athlete.id, start, today, _PERIOD_LABELS.get(range_key, "Período")
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "totals": totals,
            "share_period": share_period,
            "streak": streak,
            "has_any_workout": has_any_workout,
            "active_challenges": active_challenges,
            "strava_configured": strava_api.is_configured(),
            "strava_connected": bool(athlete.strava_access_token),
            "comparison": comparison,
            "records": records,
            "goals_progress": goals_progress,
            "calendars": calendars,
            "badges": badges,
            "pace_run": pace_run,
            "pace_swim": pace_swim,
            "insights": insights,
            "predictions": predictions,
            "next_race": next_race,
            "labels": labels,
            "cal_series": cal_series,
            "active_series": active_series,
            "evo_labels": evo_labels,
            "corrida_evo": corrida_evo,
            "natacao_evo": natacao_evo,
            "trilha_evo": trilha_evo,
            "bike_evo": bike_evo,
            "musculacao_evo": musculacao_evo,
            "bucket": bucket,
            "breakdown": breakdown,
            "recent": recent,
            "today": today.isoformat(),
            "range_options": RANGE_OPTIONS,
            "range_short": RANGE_SHORT,
            "range_key": range_key,
        },
    )


# ------------- treinos -------------

def _parse_workout_form(
    sport: str, workout_date: str, distance_m: Optional[str],
    duration_min: Optional[str], calories: Optional[str], weight_kg: float,
) -> dict:
    """Normaliza os campos do form e calcula calorias quando vazio."""
    if sport not in SPORTS:
        sport = "outro"
    try:
        d = date.fromisoformat(workout_date)
    except ValueError:
        d = today_br()
    dist_m_val = _to_float(distance_m)
    dist = (dist_m_val / 1000.0) if dist_m_val else None
    dur = _to_float(duration_min)
    cal = _to_float(calories)
    if cal is None:
        cal = estimate_calories(sport, weight_kg, dist, dur)
    return {"date": d, "sport": sport, "distance_km": dist,
            "duration_min": dur, "calories": cal}


@app.get("/novo", response_class=HTMLResponse)
def new_form(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "today": today_br().isoformat(),
            "workout": None,
            "action": "/novo",
        },
    )


@app.post("/novo")
def create_workout(
    request: Request,
    sport: str = Form(...),
    workout_date: str = Form(...),
    distance_m: Optional[str] = Form(None),
    duration_min: Optional[str] = Form(None),
    calories: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    today = today_br()
    before = notifications.snapshot(db, athlete.id, today)
    fields = _parse_workout_form(sport, workout_date, distance_m,
                                 duration_min, calories, athlete.weight_kg)
    db.add(Workout(athlete_id=athlete.id, notes=(notes or None), **fields))
    db.commit()
    # notificações: conquistas/metas/desafios recém-batidos + avisa os amigos
    try:
        after = notifications.snapshot(db, athlete.id, today)
        notifications.emit_progress(db, athlete.id, before, after, today)
        notifications.notify_friends_of_workout(db, athlete, sport_label(sport))
    except Exception:
        pass
    return RedirectResponse(url="/", status_code=303)


@app.get("/treino/{workout_id}/editar", response_class=HTMLResponse)
def edit_form(request: Request, workout_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if not w:
        return RedirectResponse(url="/treinos", status_code=303)
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "today": today_br().isoformat(),
            "workout": w,
            "action": f"/treino/{w.id}/editar",
        },
    )


@app.post("/treino/{workout_id}/editar")
def edit_workout(
    request: Request,
    workout_id: int,
    sport: str = Form(...),
    workout_date: str = Form(...),
    distance_m: Optional[str] = Form(None),
    duration_min: Optional[str] = Form(None),
    calories: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w:
        fields = _parse_workout_form(sport, workout_date, distance_m,
                                     duration_min, calories, athlete.weight_kg)
        for k, v in fields.items():
            setattr(w, k, v)
        w.notes = notes or None
        db.commit()
    return RedirectResponse(url="/treinos", status_code=303)


@app.get("/treino/{workout_id}", response_class=HTMLResponse)
def workout_detail(request: Request, workout_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if not w:
        return RedirectResponse(url="/treinos", status_code=303)
    exercises = (
        db.query(ExerciseEntry).filter(ExerciseEntry.workout_id == w.id)
        .order_by(ExerciseEntry.position, ExerciseEntry.id).all()
    )
    # volume total (séries × reps × peso) das entradas completas
    volume = sum(
        (e.sets or 0) * (e.reps or 0) * (e.weight_kg or 0) for e in exercises
    )
    # nomes de exercícios já usados pelo atleta (autocomplete)
    used = (
        db.query(ExerciseEntry.name)
        .join(Workout, ExerciseEntry.workout_id == Workout.id)
        .filter(Workout.athlete_id == athlete.id)
        .distinct().all()
    )
    exercise_names = sorted({n for (n,) in used})
    routines = (
        db.query(Routine).filter(Routine.athlete_id == athlete.id)
        .order_by(Routine.name).all()
    ) if w.sport == "musculacao" else []
    return templates.TemplateResponse(
        "workout_detail.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "w": w,
            "exercises": exercises,
            "volume": volume,
            "exercise_names": exercise_names,
            "routines": routines,
            "pace": pace(w.sport, w.distance_km, w.duration_min),
            "extra": _from_json(w.extra_json),
            "share_data": workout_share(
                w.sport, w.distance_km, w.duration_min, w.calories,
                date_label=w.date.strftime("%d/%m/%Y"),
                volume=volume, ex_count=len(exercises), polyline=w.route_polyline,
                extra=_from_json(w.extra_json),
            ),
        },
    )


@app.post("/treino/{workout_id}/exercicio")
def add_exercise(
    request: Request,
    workout_id: int,
    name: str = Form(...),
    sets: Optional[str] = Form(None),
    reps: Optional[str] = Form(None),
    weight_kg: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w and name.strip():
        last_pos = (
            db.query(func.coalesce(func.max(ExerciseEntry.position), -1))
            .filter(ExerciseEntry.workout_id == w.id).scalar()
        )
        db.add(ExerciseEntry(
            workout_id=w.id,
            name=name.strip()[:100],
            sets=_to_int(sets),
            reps=_to_int(reps),
            weight_kg=_to_float(weight_kg),
            position=(last_pos or -1) + 1,
        ))
        db.commit()
    return RedirectResponse(url=f"/treino/{workout_id}", status_code=303)


@app.post("/exercicio/{entry_id}/delete")
def delete_exercise(request: Request, entry_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    e = (
        db.query(ExerciseEntry)
        .join(Workout, ExerciseEntry.workout_id == Workout.id)
        .filter(ExerciseEntry.id == entry_id, Workout.athlete_id == athlete.id)
        .first()
    )
    wid = e.workout_id if e else None
    if e:
        db.delete(e)
        db.commit()
    return RedirectResponse(url=f"/treino/{wid}" if wid else "/treinos", status_code=303)


@app.get("/api/exercicio-hist")
def api_exercise_history(request: Request, name: str, db: Session = Depends(get_db)):
    """Histórico de um exercício (para progressão de carga)."""
    athlete = get_active_athlete(request, db)
    rows = (
        db.query(ExerciseEntry, Workout.date)
        .join(Workout, ExerciseEntry.workout_id == Workout.id)
        .filter(Workout.athlete_id == athlete.id,
                func.lower(ExerciseEntry.name) == name.strip().lower())
        .order_by(Workout.date.desc(), ExerciseEntry.id.desc())
        .limit(6).all()
    )
    items = [{
        "date": stats._as_date(d).strftime("%d/%m/%y"),
        "sets": e.sets, "reps": e.reps, "weight": e.weight_kg,
    } for e, d in rows]
    return JSONResponse({"items": items})


# ------------- rotinas de treino -------------

@app.get("/musculacao", response_class=HTMLResponse)
def strength_hub(request: Request, db: Session = Depends(get_db)):
    """Hub da musculação: lista de rotinas (treinos) editáveis."""
    athlete = get_active_athlete(request, db)
    routines = (
        db.query(Routine).filter(Routine.athlete_id == athlete.id)
        .order_by(Routine.name).all()
    )
    rdata = []
    for r in routines:
        exs = (
            db.query(RoutineItem).filter(RoutineItem.routine_id == r.id)
            .order_by(RoutineItem.position, RoutineItem.id).all()
        )
        rdata.append({"r": r, "exs": exs})
    return templates.TemplateResponse(
        "strength.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "routines": rdata,
        },
    )


@app.get("/rotinas")
def routines_redirect():
    return RedirectResponse(url="/musculacao", status_code=307)


@app.post("/rotinas")
def routine_create(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    """Cria uma rotina (treino) vazia e abre para editar os exercícios."""
    athlete = get_active_athlete(request, db)
    if not name.strip():
        return RedirectResponse(url="/musculacao", status_code=303)
    r = Routine(athlete_id=athlete.id, name=name.strip()[:100])
    db.add(r)
    db.commit()
    db.refresh(r)
    return RedirectResponse(url=f"/rotina/{r.id}", status_code=303)


def _get_routine(db: Session, athlete_id: int, routine_id: int):
    return db.query(Routine).filter(
        Routine.id == routine_id, Routine.athlete_id == athlete_id
    ).first()


@app.get("/rotina/{routine_id}", response_class=HTMLResponse)
def routine_edit_page(
    request: Request, routine_id: int,
    trained: Optional[int] = None, db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    r = _get_routine(db, athlete.id, routine_id)
    if not r:
        return RedirectResponse(url="/musculacao", status_code=303)
    items = (
        db.query(RoutineItem).filter(RoutineItem.routine_id == r.id)
        .order_by(RoutineItem.position, RoutineItem.id).all()
    )
    volume = sum((it.sets or 0) * (it.reps or 0) * (it.weight_kg or 0) for it in items)
    used = (
        db.query(ExerciseEntry.name)
        .join(Workout, ExerciseEntry.workout_id == Workout.id)
        .filter(Workout.athlete_id == athlete.id).distinct().all()
    )
    exercise_names = sorted({n for (n,) in used})
    return templates.TemplateResponse(
        "routine_edit.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "r": r,
            "items": items,
            "volume": volume,
            "exercise_names": exercise_names,
            "trained": trained,
        },
    )


@app.post("/rotina/{routine_id}/renomear")
def routine_rename(request: Request, routine_id: int,
                   name: str = Form(...), db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    r = _get_routine(db, athlete.id, routine_id)
    if r and name.strip():
        r.name = name.strip()[:100]
        db.commit()
    return RedirectResponse(url=f"/rotina/{routine_id}", status_code=303)


@app.post("/rotina/{routine_id}/item")
def routine_add_item(
    request: Request, routine_id: int,
    name: str = Form(...), sets: Optional[str] = Form(None),
    reps: Optional[str] = Form(None), weight_kg: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    r = _get_routine(db, athlete.id, routine_id)
    if r and name.strip():
        last = (db.query(func.coalesce(func.max(RoutineItem.position), -1))
                .filter(RoutineItem.routine_id == r.id).scalar()) or -1
        db.add(RoutineItem(
            routine_id=r.id, name=name.strip()[:100],
            sets=_to_int(sets), reps=_to_int(reps), weight_kg=_to_float(weight_kg),
            position=last + 1,
        ))
        db.commit()
    return RedirectResponse(url=f"/rotina/{routine_id}", status_code=303)


@app.post("/rotina-item/{item_id}/editar")
def routine_item_edit(
    request: Request, item_id: int,
    sets: Optional[str] = Form(None), reps: Optional[str] = Form(None),
    weight_kg: Optional[str] = Form(None), db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    it = (db.query(RoutineItem).join(Routine, RoutineItem.routine_id == Routine.id)
          .filter(RoutineItem.id == item_id, Routine.athlete_id == athlete.id).first())
    if it:
        it.sets = _to_int(sets)
        it.reps = _to_int(reps)
        it.weight_kg = _to_float(weight_kg)
        db.commit()
    rid = it.routine_id if it else None
    return RedirectResponse(url=f"/rotina/{rid}" if rid else "/musculacao", status_code=303)


@app.post("/rotina-item/{item_id}/delete")
def routine_item_delete(request: Request, item_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    it = (db.query(RoutineItem).join(Routine, RoutineItem.routine_id == Routine.id)
          .filter(RoutineItem.id == item_id, Routine.athlete_id == athlete.id).first())
    rid = it.routine_id if it else None
    if it:
        db.delete(it)
        db.commit()
    return RedirectResponse(url=f"/rotina/{rid}" if rid else "/musculacao", status_code=303)


@app.post("/rotina/{routine_id}/treinei")
def routine_train(request: Request, routine_id: int, db: Session = Depends(get_db)):
    """Registra que fez este treino hoje: cria a sessão (conta no dashboard,
    calorias, calendário) sem sair da rotina."""
    athlete = get_active_athlete(request, db)
    r = _get_routine(db, athlete.id, routine_id)
    if not r:
        return RedirectResponse(url="/musculacao", status_code=303)
    items = (db.query(RoutineItem).filter(RoutineItem.routine_id == r.id)
             .order_by(RoutineItem.position, RoutineItem.id).all())
    today = today_br()
    # duração estimada: ~3 min por série (ou 45 min se não houver séries)
    total_sets = sum(it.sets or 0 for it in items)
    dur = total_sets * 3 if total_sets else 45
    cal = estimate_calories("musculacao", athlete.weight_kg, None, dur)
    w = Workout(athlete_id=athlete.id, date=today, sport="musculacao",
                duration_min=dur, calories=cal, notes=f"Rotina: {r.name}")
    db.add(w)
    db.flush()
    for it in items:
        db.add(ExerciseEntry(
            workout_id=w.id, name=it.name, sets=it.sets,
            reps=it.reps, weight_kg=it.weight_kg, position=it.position,
        ))
    db.commit()
    return RedirectResponse(url=f"/rotina/{routine_id}?trained=1", status_code=303)


@app.post("/rotinas/{routine_id}/delete")
def routine_delete(request: Request, routine_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    r = db.query(Routine).filter(
        Routine.id == routine_id, Routine.athlete_id == athlete.id
    ).first()
    if r:
        db.query(RoutineItem).filter(RoutineItem.routine_id == r.id).delete(
            synchronize_session=False)
        db.delete(r)
        db.commit()
    return RedirectResponse(url="/rotinas", status_code=303)


@app.post("/treino/{workout_id}/salvar-rotina")
def save_as_routine(
    request: Request, workout_id: int,
    name: str = Form(...), db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w and name.strip():
        exercises = (
            db.query(ExerciseEntry).filter(ExerciseEntry.workout_id == w.id)
            .order_by(ExerciseEntry.position, ExerciseEntry.id).all()
        )
        if exercises:
            r = Routine(athlete_id=athlete.id, name=name.strip()[:100])
            db.add(r)
            db.flush()
            for e in exercises:
                db.add(RoutineItem(
                    routine_id=r.id, name=e.name, sets=e.sets,
                    reps=e.reps, weight_kg=e.weight_kg, position=e.position,
                ))
            db.commit()
    return RedirectResponse(url=f"/treino/{workout_id}", status_code=303)


@app.post("/treino/{workout_id}/carregar-rotina")
def load_routine(
    request: Request, workout_id: int,
    routine_id: str = Form(...), db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    rid = _to_int(routine_id)
    r = db.query(Routine).filter(
        Routine.id == rid, Routine.athlete_id == athlete.id
    ).first() if rid else None
    if w and r:
        last_pos = (
            db.query(func.coalesce(func.max(ExerciseEntry.position), -1))
            .filter(ExerciseEntry.workout_id == w.id).scalar()
        ) or -1
        items = (
            db.query(RoutineItem).filter(RoutineItem.routine_id == r.id)
            .order_by(RoutineItem.position, RoutineItem.id).all()
        )
        for it in items:
            last_pos += 1
            db.add(ExerciseEntry(
                workout_id=w.id, name=it.name, sets=it.sets,
                reps=it.reps, weight_kg=it.weight_kg, position=last_pos,
            ))
        db.commit()
    return RedirectResponse(url=f"/treino/{workout_id}", status_code=303)


@app.post("/delete/{workout_id}")
def delete_workout(
    request: Request,
    workout_id: int,
    redirect: str = Form("/"),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = db.query(Workout).filter(
        Workout.id == workout_id, Workout.athlete_id == athlete.id
    ).first()
    if w:
        # Remove os exercícios vinculados antes (evita violação de FK no Postgres)
        db.query(ExerciseEntry).filter(
            ExerciseEntry.workout_id == w.id
        ).delete(synchronize_session=False)
        db.delete(w)
        db.commit()
    dest = redirect if redirect in ("/", "/treinos") else "/"
    return RedirectResponse(url=dest, status_code=303)


@app.post("/treinos/excluir")
def delete_workouts_bulk(
    request: Request,
    ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    if ids:
        # só treinos do próprio atleta
        own = [wid for (wid,) in db.query(Workout.id).filter(
            Workout.id.in_(ids), Workout.athlete_id == athlete.id
        ).all()]
        if own:
            db.query(ExerciseEntry).filter(
                ExerciseEntry.workout_id.in_(own)
            ).delete(synchronize_session=False)
            db.query(Workout).filter(
                Workout.id.in_(own)
            ).delete(synchronize_session=False)
            db.commit()
    return RedirectResponse(url="/treinos", status_code=303)


_BIKE_HINTS = ("ride", "bike", "pedal", "cicl", "bicicl")


@app.post("/admin/limpar-outro")
def admin_clean_outro(request: Request, db: Session = Depends(get_db)):
    """Reclassifica treinos 'outro' de ciclismo para 'bike' e remove o restante
    de 'outro' do atleta ativo. Operação pontual de limpeza pós-import."""
    athlete = get_active_athlete(request, db)
    outros = db.query(Workout).filter(
        Workout.athlete_id == athlete.id, Workout.sport == "outro"
    ).all()
    reclass, removed = 0, 0
    for w in outros:
        note = (w.notes or "").lower()
        if any(h in note for h in _BIKE_HINTS):
            w.sport = "bike"
            reclass += 1
        else:
            db.delete(w)
            removed += 1
    db.commit()
    return RedirectResponse(
        url=f"/treinos?cleaned_reclass={reclass}&cleaned_removed={removed}",
        status_code=303,
    )


PAGE_SIZE = 25


@app.get("/treinos", response_class=HTMLResponse)
def workouts_history(
    request: Request,
    sport: Optional[str] = None,
    page: int = 1,
    cleaned_reclass: Optional[int] = None,
    cleaned_removed: Optional[int] = None,
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    q = db.query(Workout).filter(Workout.athlete_id == athlete.id)
    if sport in SPORTS:
        q = q.filter(Workout.sport == sport)
    total = q.count()
    page = max(1, page)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = (
        q.order_by(Workout.date.desc(), Workout.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    )
    # nº de exercícios por treino (1 query para a página)
    ids = [r.id for r in rows]
    ex_counts = dict(
        db.query(ExerciseEntry.workout_id, func.count(ExerciseEntry.id))
        .filter(ExerciseEntry.workout_id.in_(ids))
        .group_by(ExerciseEntry.workout_id).all()
    ) if ids else {}
    return templates.TemplateResponse(
        "workouts.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "sport": sport if sport in SPORTS else None,
            "sports": SPORTS,
            "cleaned_reclass": cleaned_reclass,
            "cleaned_removed": cleaned_removed,
            "ex_counts": ex_counts,
        },
    )


# ------------- metas -------------

GOAL_METRICS = ("distance", "count", "duration", "calories")
GOAL_PERIODS = ("week", "month")


@app.get("/metas", response_class=HTMLResponse)
def goals_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    today = today_br()
    goals = (
        db.query(Goal).filter(Goal.athlete_id == athlete.id)
        .order_by(Goal.id).all()
    )
    progress = [stats.goal_progress(db, athlete.id, g, today) for g in goals]
    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "progress": progress,
            "sports": SPORTS,
            "metrics": stats.GOAL_METRICS,
            "periods": stats.GOAL_PERIODS,
        },
    )


@app.post("/metas")
def goals_create(
    request: Request,
    metric: str = Form(...),
    period: str = Form(...),
    target: str = Form(...),
    sport: str = Form(""),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    tgt = _to_float(target)
    sport_val = sport if sport in SPORTS else None
    # Distância não se aplica a esportes sem deslocamento (musculação/outro)
    if metric == "distance" and sport_val in ("musculacao", "outro"):
        metric = "duration"
    if metric in GOAL_METRICS and period in GOAL_PERIODS and tgt and tgt > 0:
        db.add(Goal(
            athlete_id=athlete.id,
            sport=sport_val,
            metric=metric, period=period, target=tgt,
        ))
        db.commit()
    return RedirectResponse(url="/metas", status_code=303)


@app.post("/metas/{goal_id}/delete")
def goals_delete(request: Request, goal_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    g = db.query(Goal).filter(
        Goal.id == goal_id, Goal.athlete_id == athlete.id
    ).first()
    if g:
        db.delete(g)
        db.commit()
    return RedirectResponse(url="/metas", status_code=303)


# ------------- próximas provas -------------

def _race_view(db: Session, athlete: Athlete, r: Race, today: date) -> dict:
    days = (r.date - today).days
    pred = None
    if not r.done and r.distance_km:
        pred = stats.predict_race_time(db, athlete.id, today, r.distance_km, r.sport)
    # disputa: outros participantes da mesma prova (mesmo dispute_id)
    rivals = []
    if r.dispute_id:
        rows = (
            db.query(Race, Athlete).join(Athlete, Race.athlete_id == Athlete.id)
            .filter(Race.dispute_id == r.dispute_id, Race.athlete_id != athlete.id).all()
        )
        for rr, a in rows:
            rivals.append({
                "name": a.name, "done": bool(rr.done),
                "result": _fmt_minutes(rr.result_min) if rr.result_min else None,
                "result_min": rr.result_min,
            })
    return {"r": r, "days": days, "pred": pred,
            "result": _fmt_minutes(r.result_min) if r.result_min else None,
            "rivals": rivals}


def _fmt_minutes(minutes: float) -> str:
    total = int(round(minutes * 60))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@app.get("/provas", response_class=HTMLResponse)
def races_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    today = today_br()
    races = (
        db.query(Race).filter(Race.athlete_id == athlete.id)
        .order_by(Race.date).all()
    )
    upcoming = [_race_view(db, athlete, r, today) for r in races
                if not r.done and r.date >= today]
    past = [_race_view(db, athlete, r, today) for r in races
            if r.done or r.date < today]
    past.reverse()
    # amigos aceitos (para convidar pra disputa)
    fids = [i for i in friend_ids(db, athlete.id) if i != athlete.id]
    friends = db.query(Athlete).filter(Athlete.id.in_(fids)).order_by(Athlete.name).all() if fids else []
    return templates.TemplateResponse(
        "races.html",
        {
            "request": request, "athlete": athlete,
            "athletes": get_all_athletes(db),
            "upcoming": upcoming, "past": past,
            "today": today.isoformat(), "sports": SPORTS,
            "friends": friends,
        },
    )


@app.post("/provas")
def race_create(
    request: Request,
    name: str = Form(...), race_date: str = Form(...),
    sport: str = Form("corrida"), distance_m: Optional[str] = Form(None),
    location: Optional[str] = Form(None), link: Optional[str] = Form(None),
    invite_friend_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    try:
        d = date.fromisoformat(race_date)
    except ValueError:
        return RedirectResponse(url="/provas", status_code=303)
    dist_m = _to_float(distance_m)
    # se convidou um amigo (aceito), cria uma disputa
    dispute_id = None
    invited = None
    fid = _to_int(invite_friend_id)
    if fid and fid in friend_ids(db, athlete.id) and fid != athlete.id:
        invited = db.query(Athlete).filter(Athlete.id == fid).first()
        if invited:
            dispute_id = uuid.uuid4().hex[:32]
    r = Race(
        athlete_id=athlete.id, name=name.strip()[:120], date=d,
        sport=sport if sport in SPORTS else "corrida",
        distance_km=(dist_m / 1000.0) if dist_m else None,
        location=(location.strip()[:120] or None) if location else None,
        link=(link.strip()[:300] or None) if link else None,
        dispute_id=dispute_id,
    )
    db.add(r)
    db.commit()
    if invited and dispute_id:
        db.refresh(r)
        notifications.notify(
            db, invited.id, "race_invite",
            f"{athlete.name} te desafiou para uma disputa",
            f"{r.name} · {d.strftime('%d/%m/%Y')} — topa ver quem vai melhor?",
            "/provas", ref=f"race:{r.id}", action_id=r.id)
    return RedirectResponse(url="/provas", status_code=303)


@app.post("/provas/disputa/{race_id}/aceitar")
def race_dispute_accept(request: Request, race_id: int, db: Session = Depends(get_db)):
    """Aceita a disputa: copia a prova do convidador para a conta do amigo."""
    me = get_active_athlete(request, db)
    src = db.query(Race).filter(Race.id == race_id).first()
    if not src or not src.dispute_id or src.athlete_id == me.id:
        return RedirectResponse(url="/provas", status_code=303)
    # já tenho essa disputa?
    exists = db.query(Race).filter(
        Race.athlete_id == me.id, Race.dispute_id == src.dispute_id
    ).first()
    if not exists:
        db.add(Race(
            athlete_id=me.id, name=src.name, date=src.date, sport=src.sport,
            distance_km=src.distance_km, location=src.location, link=src.link,
            dispute_id=src.dispute_id,
        ))
        db.commit()
        notifications.notify(
            db, src.athlete_id, "race_invite",
            f"{me.name} topou a disputa: {src.name}",
            "Que vença o melhor!", "/provas", ref=f"raceacc:{src.dispute_id}:{me.id}")
    return RedirectResponse(url="/provas", status_code=303)


@app.post("/provas/{race_id}/concluir")
def race_complete(
    request: Request, race_id: int,
    result_h: Optional[str] = Form(None), result_m: Optional[str] = Form(None),
    result_s: Optional[str] = Form(None), db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    r = db.query(Race).filter(
        Race.id == race_id, Race.athlete_id == athlete.id
    ).first()
    if r:
        h = _to_int(result_h) or 0
        m = _to_int(result_m) or 0
        s = _to_int(result_s) or 0
        total = h * 60 + m + s / 60.0
        r.result_min = round(total, 2) if total > 0 else None
        r.done = 1
        db.commit()
    return RedirectResponse(url="/provas", status_code=303)


@app.post("/provas/{race_id}/delete")
def race_delete(request: Request, race_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    r = db.query(Race).filter(
        Race.id == race_id, Race.athlete_id == athlete.id
    ).first()
    if r:
        db.delete(r)
        db.commit()
    return RedirectResponse(url="/provas", status_code=303)


# ------------- ranking & conquistas -------------

@app.get("/ranking", response_class=HTMLResponse)
def ranking_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    ids = friend_ids(db, athlete.id)
    athletes = db.query(Athlete).filter(Athlete.id.in_(ids)).all()
    today = today_br()
    rows = stats.ranking(db, athletes, today)
    return templates.TemplateResponse(
        "ranking.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": athletes,
            "rows": rows,
            "month_label": today.strftime("%B de %Y"),
            "solo": len(athletes) <= 1,
        },
    )


@app.get("/conquistas", response_class=HTMLResponse)
def achievements_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    badges = achievements.evaluate(db, athlete.id)
    return templates.TemplateResponse(
        "achievements.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "badges": badges,
        },
    )


# ------------- desafios -------------

@app.get("/desafios", response_class=HTMLResponse)
def challenges_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    today = today_br()
    joins = db.query(ChallengeJoin).filter(ChallengeJoin.athlete_id == athlete.id).all()
    joined = {(j.code, j.period_key) for j in joins}
    data = challenges.build(db, athlete.id, today, joined)
    # disputas de prova com amigos (acompanhar aqui também)
    dispute_races = (
        db.query(Race).filter(
            Race.athlete_id == athlete.id, Race.dispute_id.isnot(None)
        ).order_by(Race.date).all()
    )
    disputes = [_race_view(db, athlete, r, today) for r in dispute_races]
    return templates.TemplateResponse(
        "challenges.html",
        {
            "request": request,
            "athlete": athlete,
            "weekly": data["weekly"],
            "monthly": data["monthly"],
            "disputes": disputes,
        },
    )


@app.post("/desafios/aceitar")
def challenge_accept(request: Request, code: str = Form(...),
                     db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    ch = challenges.CHALLENGES_BY_CODE.get(code)
    if ch:
        pk = challenges.period_key(ch.period, today_br())
        exists = db.query(ChallengeJoin).filter(
            ChallengeJoin.athlete_id == athlete.id,
            ChallengeJoin.code == code, ChallengeJoin.period_key == pk,
        ).first()
        if not exists:
            db.add(ChallengeJoin(athlete_id=athlete.id, code=code, period_key=pk))
            db.commit()
    return RedirectResponse(url="/desafios", status_code=303)


@app.post("/desafios/{code}/sair")
def challenge_leave(request: Request, code: str, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    ch = challenges.CHALLENGES_BY_CODE.get(code)
    if ch:
        pk = challenges.period_key(ch.period, today_br())
        db.query(ChallengeJoin).filter(
            ChallengeJoin.athlete_id == athlete.id,
            ChallengeJoin.code == code, ChallengeJoin.period_key == pk,
        ).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse(url="/desafios", status_code=303)


# ------------- peso -------------

@app.get("/peso", response_class=HTMLResponse)
def weight_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    logs = (
        db.query(WeightLog).filter(WeightLog.athlete_id == athlete.id)
        .order_by(WeightLog.date.asc(), WeightLog.id.asc()).all()
    )
    series = [{"date": l.date.isoformat(), "label": l.date.strftime("%d/%m/%y"),
               "weight": l.weight_kg, "id": l.id} for l in logs]
    first = logs[0].weight_kg if logs else None
    last = logs[-1].weight_kg if logs else None
    delta = round(last - first, 1) if (first is not None and last is not None) else None
    return templates.TemplateResponse(
        "weight.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "logs": list(reversed(logs)),
            "series": series,
            "delta": delta,
            "today": today_br().isoformat(),
        },
    )


@app.post("/peso")
def weight_create(
    request: Request,
    weight_kg: str = Form(...),
    log_date: str = Form(...),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    w = _to_float(weight_kg)
    try:
        d = date.fromisoformat(log_date)
    except ValueError:
        d = today_br()
    if w and w > 0:
        # um registro por dia: atualiza se já existe
        existing = db.query(WeightLog).filter(
            WeightLog.athlete_id == athlete.id, WeightLog.date == d
        ).first()
        if existing:
            existing.weight_kg = w
        else:
            db.add(WeightLog(athlete_id=athlete.id, date=d, weight_kg=w))
        # mantém o peso "atual" do atleta = registro mais recente
        latest = db.query(func.max(WeightLog.date)).filter(
            WeightLog.athlete_id == athlete.id
        ).scalar()
        if latest is None or d >= latest:
            athlete.weight_kg = w
        db.commit()
    return RedirectResponse(url="/peso", status_code=303)


@app.post("/peso/{log_id}/delete")
def weight_delete(request: Request, log_id: int, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    l = db.query(WeightLog).filter(
        WeightLog.id == log_id, WeightLog.athlete_id == athlete.id
    ).first()
    if l:
        db.delete(l)
        db.commit()
    return RedirectResponse(url="/peso", status_code=303)


# ------------- atletas -------------

@app.get("/atletas", response_class=HTMLResponse)
def athletes_page(request: Request, db: Session = Depends(get_db),
                  pwd: Optional[str] = None):
    active = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "athletes.html",
        {
            "request": request,
            "athlete": active,
            "db_dialect": engine.dialect.name,
            "pwd": pwd,  # ok | erro
        },
    )


@app.post("/atletas/senha")
def change_password(request: Request, current: str = Form(...),
                    new: str = Form(...), db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    if (not me.password_hash or not auth.verify_password(current, me.password_hash)
            or len(new) < 4):
        return RedirectResponse(url="/atletas?pwd=erro", status_code=303)
    me.password_hash = auth.hash_password(new)
    db.commit()
    return RedirectResponse(url="/atletas?pwd=ok", status_code=303)


@app.post("/atletas/{aid}/edit")
def athletes_edit(
    request: Request,
    aid: int,
    name: str = Form(...),
    weight_kg: str = Form(...),
    height_cm: Optional[str] = Form(None),
    age: Optional[str] = Form(None),
    sex: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    me = get_active_athlete(request, db)
    if aid != me.id and not me.is_admin:
        raise HTTPException(status_code=403)
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if a:
        a.name = name.strip()[:80] or a.name
        w = _to_float(weight_kg)
        if w and w > 0:
            a.weight_kg = w
        a.height_cm = _to_float(height_cm)
        a.age = _to_int(age)
        a.sex = sex.upper() if sex in ("M", "F", "m", "f") else None
        db.commit()
    return RedirectResponse(url="/atletas", status_code=303)


@app.get("/atletas/{aid}/foto")
def athletes_photo(aid: int, db: Session = Depends(get_db)):
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if not a or not a.photo:
        raise HTTPException(status_code=404)
    return Response(content=a.photo, media_type=a.photo_mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/atletas/{aid}/foto")
async def athletes_photo_upload(
    request: Request, aid: int, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    me = get_active_athlete(request, db)
    if aid != me.id and not me.is_admin:
        raise HTTPException(status_code=403)
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if not a:
        return RedirectResponse(url="/atletas", status_code=303)
    raw = await file.read()
    if not raw or len(raw) > 8 * 1024 * 1024:  # 8 MB hard cap
        return RedirectResponse(url="/atletas", status_code=303)

    if HAS_PIL:
        # Caminho ideal: crop quadrado central + resize 512x512 JPEG (~20KB)
        try:
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)  # respeita orientação EXIF do celular
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((512, 512), Image.LANCZOS)
            if img.mode in ("RGBA", "P", "LA"):
                bg = Image.new("RGB", img.size, (10, 16, 32))
                bg.paste(img, mask=img.convert("RGBA").split()[-1] if img.mode != "P" else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            a.photo = out.getvalue()
            a.photo_mime = "image/jpeg"
            db.commit()
        except Exception:
            pass
    else:
        # Fallback sem Pillow: salva o original (limitado a 2 MB para não estourar
        # o banco) com o mime informado pelo navegador.
        if len(raw) <= 2 * 1024 * 1024 and (file.content_type or "").startswith("image/"):
            a.photo = raw
            a.photo_mime = file.content_type
            db.commit()
    return RedirectResponse(url="/atletas", status_code=303)


@app.post("/atletas/{aid}/foto/delete")
def athletes_photo_delete(request: Request, aid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    if aid != me.id and not me.is_admin:
        raise HTTPException(status_code=403)
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if a:
        a.photo = None
        a.photo_mime = None
        db.commit()
    return RedirectResponse(url="/atletas", status_code=303)


@app.post("/atletas/{aid}/delete")
def athletes_delete(request: Request, aid: int, db: Session = Depends(get_db)):
    me = get_active_athlete(request, db)
    if not me.is_admin or aid == me.id:
        # apenas admin pode excluir contas, e nunca a própria
        raise HTTPException(status_code=403)
    total = db.query(Athlete).count()
    if total <= 1:
        # nunca apaga o último atleta
        return RedirectResponse(url="/atletas", status_code=303)
    a = db.query(Athlete).filter(Athlete.id == aid).first()
    if a:
        # Limpa os vínculos antes (evita violação de FK no Postgres)
        w_ids = [wid for (wid,) in
                 db.query(Workout.id).filter(Workout.athlete_id == aid).all()]
        if w_ids:
            db.query(ExerciseEntry).filter(
                ExerciseEntry.workout_id.in_(w_ids)).delete(synchronize_session=False)
        r_ids = [rid for (rid,) in
                 db.query(Routine.id).filter(Routine.athlete_id == aid).all()]
        if r_ids:
            db.query(RoutineItem).filter(
                RoutineItem.routine_id.in_(r_ids)).delete(synchronize_session=False)
        db.query(Routine).filter(Routine.athlete_id == aid).delete(synchronize_session=False)
        db.query(Goal).filter(Goal.athlete_id == aid).delete(synchronize_session=False)
        db.query(WeightLog).filter(WeightLog.athlete_id == aid).delete(synchronize_session=False)
        db.query(Friendship).filter(
            (Friendship.athlete_id == aid) | (Friendship.friend_id == aid)
        ).delete(synchronize_session=False)
        db.query(ChallengeJoin).filter(ChallengeJoin.athlete_id == aid).delete(synchronize_session=False)
        db.query(Notification).filter(Notification.athlete_id == aid).delete(synchronize_session=False)
        db.query(Workout).filter(Workout.athlete_id == aid).delete(synchronize_session=False)
        db.delete(a)
        db.commit()
    return RedirectResponse(url="/atletas", status_code=303)


# ------------- importar -------------

def _insert_parsed_workouts(db: Session, athlete: Athlete, parsed_list):
    """Insere treinos parseados (Strava/Garmin) com dedup por
    (data, esporte, duração, distância). Retorna (inseridos, duplicados, by_sport)."""
    existing = db.query(Workout).filter(Workout.athlete_id == athlete.id).all()

    def key(d, sport, dur, dist):
        return (d, sport,
                round(dur, 0) if dur is not None else None,
                round(dist, 2) if dist is not None else None)

    existing_by_key = {key(w.date, w.sport, w.duration_min, w.distance_km): w
                       for w in existing}
    inserted, skipped_dup = 0, 0
    by_sport: dict[str, int] = {}

    for pw in parsed_list:
        k = key(pw.date, pw.sport, pw.duration_min, pw.distance_km)
        poly = getattr(pw, "polyline", None)
        extra = getattr(pw, "extra", None)
        extra_json = json.dumps(extra) if extra else None
        if k in existing_by_key:
            skipped_dup += 1
            # backfill: preenche rota / métricas extras se o treino já existia sem
            w = existing_by_key[k]
            if poly and not w.route_polyline:
                w.route_polyline = poly
            if extra_json and not w.extra_json:
                w.extra_json = extra_json
            continue
        cal = pw.calories
        if cal is None:
            cal = estimate_calories(pw.sport, athlete.weight_kg,
                                    pw.distance_km, pw.duration_min)
        w = Workout(
            athlete_id=athlete.id, date=pw.date, sport=pw.sport,
            distance_km=pw.distance_km, duration_min=pw.duration_min,
            calories=cal, notes=pw.notes, route_polyline=poly, extra_json=extra_json,
        )
        db.add(w)
        existing_by_key[k] = w
        inserted += 1
        by_sport[pw.sport] = by_sport.get(pw.sport, 0) + 1

    db.commit()
    return inserted, skipped_dup, by_sport


@app.get("/importar/garmin", response_class=HTMLResponse)
def garmin_import_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "import_garmin.html",
        {
            "request": request,
            "athlete": athlete,
            "athletes": get_all_athletes(db),
            "result": None,
        },
    )


@app.post("/importar/garmin", response_class=HTMLResponse)
async def garmin_import_upload(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    content = await file.read()
    ctx = {"request": request, "athlete": athlete, "athletes": get_all_athletes(db)}

    if len(content) > 25 * 1024 * 1024:  # 25 MB
        ctx["result"] = {"error": "Arquivo maior que 25 MB. Suba o summarizedActivities.json."}
        return templates.TemplateResponse("import_garmin.html", ctx)

    from garmin_import import parse_garmin_json
    parsed = parse_garmin_json(content)
    if not parsed.ok or parsed.total_rows == 0:
        ctx["result"] = {
            "error": ("Não consegui ler as atividades. Confirme que é o arquivo "
                      "summarizedActivities.json do export da Garmin (não o .zip inteiro)."),
        }
        return templates.TemplateResponse("import_garmin.html", ctx)

    inserted, skipped_dup, by_sport = _insert_parsed_workouts(db, athlete, parsed.parsed)
    ctx["result"] = {
        "ok": True, "filename": file.filename, "total_rows": parsed.total_rows,
        "inserted": inserted, "skipped_dup": skipped_dup,
        "skipped_bad": parsed.skipped_bad_row, "by_sport": by_sport,
    }
    return templates.TemplateResponse("import_garmin.html", ctx)


def _import_ctx(request, athlete, db, result=None, strava_result=None, strava_error=None):
    return {
        "request": request,
        "athlete": athlete,
        "athletes": get_all_athletes(db),
        "result": result,
        "strava_configured": strava_api.is_configured(),
        "strava_connected": bool(athlete.strava_access_token),
        "strava_result": strava_result,
        "strava_error": strava_error,
    }


@app.get("/importar", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db),
                strava: Optional[str] = None):
    athlete = get_active_athlete(request, db)
    err = {"erro": "Não consegui conectar ao Strava. Tente de novo."} if strava == "erro" else None
    return templates.TemplateResponse(
        "import.html",
        _import_ctx(request, athlete, db, strava_error=(err and err["erro"])),
    )


def _strava_redirect_uri(request: Request) -> str:
    # o domínio precisa bater com o "Authorization Callback Domain" no Strava
    return f"https://{request.url.hostname}/strava/callback"


@app.get("/strava/status")
def strava_status():
    """Diagnóstico (sem segredos): o app está enxergando as credenciais?"""
    cid = strava_api.client_id()
    return JSONResponse({
        "configured": strava_api.is_configured(),
        "client_id_set": bool(cid),
        "client_id_preview": (cid[:3] + "…") if cid else None,
        "client_secret_set": bool(strava_api.client_secret()),
    })


@app.get("/strava/conectar")
def strava_connect(request: Request, db: Session = Depends(get_db)):
    get_active_athlete(request, db)  # exige login
    if not strava_api.is_configured():
        return RedirectResponse(url="/importar?strava=erro", status_code=303)
    state = secrets.token_hex(16)
    request.session["strava_state"] = state
    url = strava_api.authorize_url(_strava_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@app.get("/strava/callback")
def strava_callback(request: Request, db: Session = Depends(get_db),
                    code: Optional[str] = None, state: Optional[str] = None,
                    error: Optional[str] = None):
    athlete = get_active_athlete(request, db)
    if error or not code or state != request.session.get("strava_state"):
        return RedirectResponse(url="/importar?strava=erro", status_code=303)
    request.session.pop("strava_state", None)
    try:
        tok = strava_api.exchange_code(code)
        athlete.strava_access_token = tok["access_token"]
        athlete.strava_refresh_token = tok["refresh_token"]
        athlete.strava_expires_at = int(tok["expires_at"])
        sa = tok.get("athlete") or {}
        if sa.get("id"):
            athlete.strava_athlete_id = str(sa["id"])
        db.commit()
    except Exception:
        return RedirectResponse(url="/importar?strava=erro", status_code=303)
    return RedirectResponse(url="/importar?strava=conectado", status_code=303)


def _strava_valid_token(athlete: Athlete, db: Session) -> Optional[str]:
    """Garante um access token válido, renovando pelo refresh token se expirou."""
    if not athlete.strava_refresh_token:
        return None
    if athlete.strava_expires_at and athlete.strava_expires_at - 60 > int(time.time()):
        return athlete.strava_access_token
    tok = strava_api.refresh_access_token(athlete.strava_refresh_token)
    athlete.strava_access_token = tok["access_token"]
    athlete.strava_refresh_token = tok["refresh_token"]
    athlete.strava_expires_at = int(tok["expires_at"])
    db.commit()
    return athlete.strava_access_token


_STRAVA_AUTOSYNC_INTERVAL = timedelta(hours=2)


def _maybe_strava_autosync(athlete: Athlete, db: Session) -> None:
    """Sincroniza treinos recentes do Strava ao abrir o app — no máximo 1x a
    cada 2h por pessoa, e sem nunca quebrar a página se o Strava falhar."""
    if not athlete.strava_access_token:
        return
    last = athlete.strava_last_sync_at
    if last and (now_br() - last) < _STRAVA_AUTOSYNC_INTERVAL:
        return
    try:
        token = _strava_valid_token(athlete, db)
        after = int(time.time()) - 45 * 24 * 3600  # últimos ~45 dias
        parsed = strava_api.fetch_activities(token, after_epoch=after,
                                             max_pages=3, timeout=8)
        _today = today_br()
        _before = notifications.snapshot(db, athlete.id, _today)
        _insert_parsed_workouts(db, athlete, parsed)
        notifications.emit_progress(
            db, athlete.id, _before, notifications.snapshot(db, athlete.id, _today), _today)
    except Exception:
        pass  # Strava lento/fora não pode travar o app
    athlete.strava_last_sync_at = now_br()
    db.commit()


@app.post("/strava/importar", response_class=HTMLResponse)
def strava_import(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    if not athlete.strava_access_token:
        return RedirectResponse(url="/importar", status_code=303)
    try:
        token = _strava_valid_token(athlete, db)
        parsed = strava_api.fetch_activities(token)
    except Exception:
        return templates.TemplateResponse(
            "import.html",
            _import_ctx(request, athlete, db,
                        strava_error="Falha ao buscar atividades no Strava. Tente de novo."),
        )
    _today = today_br()
    _before = notifications.snapshot(db, athlete.id, _today)
    inserted, skipped_dup, by_sport = _insert_parsed_workouts(db, athlete, parsed)
    athlete.strava_last_sync_at = now_br()
    db.commit()
    try:
        notifications.emit_progress(
            db, athlete.id, _before, notifications.snapshot(db, athlete.id, _today), _today)
    except Exception:
        pass
    return templates.TemplateResponse(
        "import.html",
        _import_ctx(request, athlete, db, strava_result={
            "total": len(parsed), "inserted": inserted,
            "skipped_dup": skipped_dup, "by_sport": by_sport,
        }),
    )


@app.post("/strava/desconectar")
def strava_disconnect(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    athlete.strava_access_token = None
    athlete.strava_refresh_token = None
    athlete.strava_expires_at = None
    athlete.strava_athlete_id = None
    db.commit()
    return RedirectResponse(url="/importar", status_code=303)


@app.post("/importar", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    content = await file.read()

    if len(content) > 10 * 1024 * 1024:  # 10 MB
        result = {"error": "Arquivo maior que 10 MB. Suba um CSV menor."}
        return templates.TemplateResponse("import.html",
            _import_ctx(request, athlete, db, result=result))

    parsed = parse_strava_csv(content)
    if not parsed.detected_columns.get("date") or not parsed.detected_columns.get("type"):
        result = {
            "error": ("Não consegui identificar as colunas 'Data' e 'Tipo de atividade' "
                      "no CSV. Confirma que é o activities.csv do export oficial do Strava?"),
            "headers_found": list(parsed.detected_columns.values()) or None,
        }
        return templates.TemplateResponse("import.html",
            _import_ctx(request, athlete, db, result=result))

    inserted, skipped_dup, by_sport = _insert_parsed_workouts(db, athlete, parsed.parsed)

    result = {
        "ok": True,
        "filename": file.filename,
        "total_rows": parsed.total_rows,
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "skipped_bad": parsed.skipped_bad_row,
        "by_sport": by_sport,
        "detected_columns": parsed.detected_columns,
    }
    return templates.TemplateResponse("import.html",
        _import_ctx(request, athlete, db, result=result))


# ------------- config (redirect legado) -------------

@app.get("/config")
def config_redirect():
    return RedirectResponse(url="/atletas", status_code=307)


# ------------- API -------------

@app.get("/sw.js")
def service_worker():
    """Serve o service worker da raiz para ter escopo '/' (controla o app todo)."""
    import os
    path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse("offline.html", {"request": request})


@app.get("/instalar", response_class=HTMLResponse)
def install_page(request: Request, db: Session = Depends(get_db)):
    athlete = get_active_athlete(request, db)
    return templates.TemplateResponse(
        "install.html",
        {"request": request, "athlete": athlete, "athletes": get_all_athletes(db)},
    )


@app.get("/api/db-info")
def api_db_info(db: Session = Depends(get_db)):
    """Diagnóstico: mostra qual banco está em uso e contagem de registros.
    Útil para confirmar se DATABASE_URL do Postgres está ativo no Railway."""
    dialect = engine.dialect.name
    # URL sem credenciais
    url = str(engine.url).split("@")[-1] if "@" in str(engine.url) else str(engine.url)
    return {
        "dialect": dialect,
        "persistente": dialect == "postgresql",
        "host": url,
        "athletes": db.query(func.count(Athlete.id)).scalar() or 0,
        "workouts": db.query(func.count(Workout.id)).scalar() or 0,
    }


@app.get("/api/estimate")
def api_estimate(
    request: Request,
    sport: str,
    distance_km: Optional[float] = None,
    distance_m: Optional[float] = None,
    duration_min: Optional[float] = None,
    db: Session = Depends(get_db),
):
    athlete = get_active_athlete(request, db)
    if distance_km is None and distance_m is not None:
        distance_km = distance_m / 1000.0
    kcal = estimate_calories(sport, athlete.weight_kg, distance_km, duration_min)
    return JSONResponse({
        "kcal": kcal,
        "weight_kg": athlete.weight_kg,
        "athlete": athlete.name,
    })
