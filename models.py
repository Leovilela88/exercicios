from datetime import date as date_type
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, func
from db import Base


class Workout(Base):
    __tablename__ = "workouts"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, default=date_type.today, index=True)
    sport = Column(String(20), nullable=False)  # "corrida" | "natacao" | "outro"
    distance_km = Column(Float, nullable=True)
    duration_min = Column(Float, nullable=True)
    calories = Column(Float, nullable=True)
    notes = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    weight_kg = Column(Float, nullable=False, default=82.0)
