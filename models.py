from datetime import date as date_type
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, LargeBinary, func
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
