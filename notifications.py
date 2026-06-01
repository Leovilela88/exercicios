"""Central de notificações: pedidos de amizade, troféus, metas, desafios e
treinos de amigos. Gera notificações de progresso comparando o estado antes/
depois de registrar um treino (sem inundar com o que já estava conquistado)."""
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

import achievements
import challenges
import stats
from models import ChallengeJoin, Friendship, Goal, Notification


def notify(db: Session, athlete_id: int, ntype: str, title: str,
           body: str = "", link: str = "", ref: Optional[str] = None,
           action_id: Optional[int] = None, commit: bool = True):
    """Cria uma notificação (dedupe por `ref` quando informado)."""
    if ref:
        exists = db.query(Notification).filter(
            Notification.athlete_id == athlete_id,
            Notification.type == ntype, Notification.ref == ref,
        ).first()
        if exists:
            return None
    n = Notification(athlete_id=athlete_id, type=ntype, title=title,
                     body=body or None, link=link or None, ref=ref,
                     action_id=action_id, read=0)
    db.add(n)
    if commit:
        db.commit()
    return n


def unread_count(db: Session, athlete_id: int) -> int:
    return db.query(Notification).filter(
        Notification.athlete_id == athlete_id, Notification.read == 0
    ).count()


# ----------------------------------------------------------- progresso

def snapshot(db: Session, athlete_id: int, today: date) -> dict:
    """Conjuntos do que já está conquistado (troféus, desafios, metas)."""
    badges = {it["badge"].id for it in achievements.evaluate(db, athlete_id)["unlocked"]}

    joins = db.query(ChallengeJoin).filter(ChallengeJoin.athlete_id == athlete_id).all()
    joined = {(j.code, j.period_key) for j in joins}
    chdata = challenges.build(db, athlete_id, today, joined)
    chal = set()
    for it in chdata["weekly"] + chdata["monthly"]:
        if it["joined"] and it["done"]:
            ch = it["ch"]
            chal.add((ch.code, challenges.period_key(ch.period, today)))

    goals = db.query(Goal).filter(Goal.athlete_id == athlete_id).all()
    goal_map = {g.id: g for g in goals}
    gdone = set()
    for g in goals:
        p = stats.goal_progress(db, athlete_id, g, today)
        if p and p.get("done"):
            gdone.add(g.id)
    return {"badges": badges, "chal": chal, "goals": gdone, "goal_map": goal_map}


def emit_progress(db: Session, athlete_id: int, before: dict, after: dict,
                  today: date) -> None:
    """Notifica o que foi conquistado entre os dois snapshots."""
    for bid in after["badges"] - before["badges"]:
        b = achievements.BADGES_BY_ID.get(bid)
        if b:
            notify(db, athlete_id, "trophy", f"Troféu desbloqueado: {b.title}",
                   b.desc, "/conquistas", ref=f"trophy:{bid}", commit=False)
    for (code, pk) in after["chal"] - before["chal"]:
        ch = challenges.CHALLENGES_BY_CODE.get(code)
        if ch:
            notify(db, athlete_id, "challenge", f"Desafio concluído: {ch.title}",
                   "Mandou bem!", "/desafios", ref=f"chal:{code}:{pk}", commit=False)
    for gid in after["goals"] - before["goals"]:
        g = after["goal_map"].get(gid)
        if g:
            pk = challenges.period_key(g.period, today)
            notify(db, athlete_id, "goal", "Meta alcançada!",
                   "Você bateu uma das suas metas.", "/metas",
                   ref=f"goal:{gid}:{pk}", commit=False)
    db.commit()


# ----------------------------------------------------------- treino de amigo

def notify_friends_of_workout(db: Session, athlete, sport_label: str) -> None:
    """Avisa os amigos (que aceitaram) que o atleta registrou um treino."""
    rows = db.query(Friendship.athlete_id).filter(
        Friendship.friend_id == athlete.id, Friendship.status == "accepted"
    ).all()
    for (fid,) in rows:
        notify(db, fid, "workout", f"{athlete.name} registrou um treino",
               f"{sport_label} — confira o ranking!", "/ranking", commit=False)
    db.commit()
