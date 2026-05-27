from datetime import date as date_type
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, func
from db import Base


class Athlete(Base):
    __tablename__ = "athletes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False)
    weight_kg = Column(Float, nullable=False, default=70.0)
    height_cm = Column(Float, nullable=True)
    age = Column(Integer, nullable=True)
    sex = Column(String(1), nullable=True)  # 'M' | 'F' | None
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


# Mantida só por compatibilidade com bases antigas (não usada após migração).
class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    weight_kg = Column(Float, nullable=False, default=82.0)
