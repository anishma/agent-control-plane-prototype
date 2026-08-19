"""Fictional downstream travel-provider API for the clean-room E4 demo."""

import os
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Fictional Travel Provider")
PROVIDER_TOKEN = os.environ["SYNTHETIC_PROVIDER_TOKEN"]


class FlightBooking(BaseModel):
    principal: str
    flight_id: str
    travel_date: str
    total_amount: int


@app.post("/v1/flights/book")
def book_flight(booking: FlightBooking, authorization: str | None = Header(default=None)) -> dict:
    """Accept only the synthetic credential held by the BFF."""
    if authorization != f"Bearer {PROVIDER_TOKEN}":
        raise HTTPException(status_code=401, detail="provider credential required")
    return {
        "status": "booked",
        "provider_booking_id": f"PB-{uuid4().hex[:10].upper()}",
        "principal": booking.principal,
        "flight_id": booking.flight_id,
        "travel_date": booking.travel_date,
        "total_amount": booking.total_amount,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
