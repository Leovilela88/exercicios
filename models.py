from datetime import date as date_type
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, LargeBinary, Text, func
from db import Base


class Athlete(Base):
    __tablename__ = "athletes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False)
    weight_kg = Column(Float, nullable=False, default=70.0)
    height_cm = Column(Float, nullable=True)
    age = Column(Integer, nullable=True)
    sex = Column(String(1), nullable=True)  # 'M' | 'F' | None
    photo = Column(LargeBinary, nullable=True)
    photo_mime = Column(String(50), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    # --- conta / login ---
    username = Column(String(30), unique=True, index=True, nullable=True)
    password_hash = Column(String(255), nullable=True)
    friend_code = Column(String(12), unique=True, index=True, nullable=True)
    is_admin = Column(Integer, nullable=False, default=0)  # 0/1
    last_seen_at = Column(DateTime, nullable=True)
    # --- integração Strava ---
    strava_athlete_id = Column(String(32), nullable=True)
    strava_access_token = Column(String(255), nullable=True)
    strava_refresh_token = Column(String(255), nullable=True)
    strava_expires_at = Column(Integer, nullable=True)  # epoch (segundos)
    strava_last_sync_at = Column(DateTime, nullable=True)


class Friendship(Base):
    """Vínculo de amizade. `athlete_id` -> `friend_id`.
    status: 'pending' (pedido enviado, aguardando) | 'accepted'."""
    __tablename__ = "friendships"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    friend_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    status = Column(String(10), nullable=False, default="accepted")
    created_at = Column(DateTime, server_default=func.now())


class Notification(Base):
    """Notificação para um atleta (pedido de amizade, troféu, meta, etc.)."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    type = Column(String(20), nullable=False)  # friend_request|friend_accepted|workout|trophy|goal|challenge
    title = Column(String(160), nullable=False)
    body = Column(String(300), nullable=True)
    link = Column(String(120), nullable=True)
    ref = Column(String(60), nullable=True)        # dedupe (ex: trophy:run10)
    action_id = Column(Integer, nullable=True)     # ex: id da friendship p/ aceitar
    read = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now())


class ChallengeJoin(Base):
    """Desafio aceito por um atleta num período (semana/mês)."""
    __tablename__ = "challenge_joins"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    code = Column(String(40), nullable=False)
    period_key = Column(String(12), nullable=False)  # ex: 2026-06 ou 2026-W23
    created_at = Column(DateTime, server_default=func.now())


class Workout(Base):
    __tablename__ = "workouts"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=True, index=True)
    date = Column(Date, nullable=False, default=date_type.today, index=True)
    sport = Column(String(20), nullable=False)  # corrida | natacao | musculacao | outro
    distance_km = Column(Float, nullable=True)
    duration_min = Column(Float, nullable=True)
    calories = Column(Float, nullable=True)
    notes = Column(String(500), nullable=True)
    route_polyline = Column(Text, nullable=True)  # traçado GPS (encoded polyline do Strava)
    extra_json = Column(Text, nullable=True)       # métricas extras (FC, elevação, etc.) em JSON
    created_at = Column(DateTime, server_default=func.now())


class ExerciseEntry(Base):
    __tablename__ = "exercise_entries"

    id = Column(Integer, primary_key=True, index=True)
    workout_id = Column(Integer, ForeignKey("workouts.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    sets = Column(Integer, nullable=True)
    reps = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now())


class Routine(Base):
    __tablename__ = "routines"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class RoutineItem(Base):
    __tablename__ = "routine_items"

    id = Column(Integer, primary_key=True, index=True)
    routine_id = Column(Integer, ForeignKey("routines.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    sets = Column(Integer, nullable=True)
    reps = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)
    position = Column(Integer, nullable=False, default=0)


class WeightLog(Base):
    __tablename__ = "weight_logs"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, default=date_type.today, index=True)
    weight_kg = Column(Float, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class Race(Base):
    __tablename__ = "races"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    date = Column(Date, nullable=False, index=True)
    sport = Column(String(20), nullable=False, default="corrida")
    distance_km = Column(Float, nullable=True)
    location = Column(String(120), nullable=True)
    link = Column(String(300), nullable=True)
    result_min = Column(Float, nullable=True)   # tempo real (min) após concluir
    done = Column(Integer, nullable=False, default=0)
    dispute_id = Column(String(40), nullable=True, index=True)  # disputa entre amigos
    created_at = Column(DateTime, server_default=func.now())


class Goal(Base):
    __tablename__ = "goals"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athletes.id"), nullable=False, index=True)
    sport = Column(String(20), nullable=True)        # None = todos os esportes
    metric = Column(String(20), nullable=False)      # distance | count | duration | calories
    period = Column(String(10), nullable=False)      # week | month
    target = Column(Float, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


# Mantida só por compatibilidade com bases antigas (não usada após migração).
class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    weight_kg = Column(Float, nullable=False, default=82.0)
