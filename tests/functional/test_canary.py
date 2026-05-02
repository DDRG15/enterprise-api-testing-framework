import pytest
import logging
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class CanaryDates(BaseModel):
    checkin: str
    checkout: str

class CanaryPayload(BaseModel):
    firstname: str
    lastname: str
    totalprice: int
    depositpaid: bool
    bookingdates: CanaryDates
    additionalneeds: str

def test_production_canary_probe(booking_client):
    """
    CANARY PROBE: Single-threaded, static payload.
    Used for safe verification against live production environments without triggering WAFs
    or polluting the database with high-entropy synthetic data.
    """
    logger.info("INITIATING CANARY PROBE - Static Payload Generation")
    
    static_payload = CanaryPayload(
        firstname="Canary",
        lastname="Probe",
        totalprice=1,
        depositpaid=True,
        bookingdates=CanaryDates(checkin="2026-05-10", checkout="2026-05-15"),
        additionalneeds="SRE Health Check"
    )

    # 1. Create
    create_response = booking_client.create_booking(static_payload)
    booking_id = create_response.bookingid
    logger.info(f"Canary deployed successfully. ID: {booking_id}")

    # 2. Read
    read_response = booking_client.get_booking(booking_id)
    assert read_response.firstname == "Canary"

    # 3. Delete
    booking_client.delete_booking(booking_id)
    logger.info(f"Canary {booking_id} cleanly neutralized.")
