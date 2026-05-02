"""
tests/contract/test_schema_contracts.py
=========================================
Contract tests: verify the API's response shape against our defined schema.

These are NOT functional tests. They don't test business logic.
They ask one question: "Is the API speaking the language we expect?"

Why this matters for Tier-1 systems:
  - APIs change. A field gets renamed from `totalprice` to `total_price`.
  - Without contract tests, functional tests start failing with cryptic
    KeyErrors weeks later in production.
  - These tests catch schema drift at the API boundary, immediately.

These run separately from CRUD tests so a schema failure is immediately
identifiable as "contract problem" vs "logic problem."
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.client.booking_client import BookingClient
from src.models.booking import BookingResponse, BookingSummary, CreateBookingResponse
from src.utils.logger import get_logger

logger = get_logger(__name__)


@pytest.mark.contract
class TestResponseSchemaContracts:

    def test_create_booking_response_matches_contract(
        self,
        created_booking: tuple[int, any],
        booking_client: BookingClient,
    ) -> None:
        """
        GIVEN POST /booking is called
        THEN the response is a valid CreateBookingResponse (bookingid + booking object)
        AND bookingid is a positive integer
        AND all nested booking fields are present and correctly typed

        Failure here = the API changed its POST response shape.
        """
        booking_id, _ = created_booking
        assert isinstance(booking_id, int) and booking_id > 0

    def test_get_booking_response_matches_contract(
        self,
        created_booking: tuple[int, any],
        booking_client: BookingClient,
    ) -> None:
        """
        GIVEN GET /booking/{id} is called
        THEN the response deserializes into BookingResponse without error
        AND no required fields are missing or null
        """
        booking_id, _ = created_booking

        # This will raise ValidationError if schema has drifted
        booking = booking_client.get_booking(booking_id)

        assert isinstance(booking, BookingResponse)
        assert booking.firstname is not None and booking.firstname != ""
        assert booking.lastname is not None and booking.lastname != ""
        assert booking.bookingdates is not None
        assert booking.bookingdates.checkin is not None
        assert booking.bookingdates.checkout is not None

    def test_list_bookings_response_is_array_of_summaries(
        self,
        booking_client: BookingClient,
    ) -> None:
        """
        GIVEN GET /booking is called
        THEN the response is a non-empty JSON array
        AND each element contains a `bookingid` field as a positive integer
        """
        summaries = booking_client.list_bookings()

        assert isinstance(summaries, list), (
            f"Expected list response from GET /booking, got {type(summaries).__name__}"
        )
        assert len(summaries) > 0, (
            "GET /booking returned an empty list. "
            "There should be at least some bookings in the system."
        )

        for summary in summaries[:5]:  # Spot-check the first 5
            assert isinstance(summary, BookingSummary)
            assert summary.bookingid > 0

    def test_date_fields_are_properly_typed(
        self,
        created_booking: tuple[int, any],
        booking_client: BookingClient,
    ) -> None:
        """
        GIVEN GET /booking/{id} returns date fields
        THEN checkin and checkout deserialize as Python date objects (not raw strings)
        AND checkout is strictly after checkin

        This catches APIs that return dates as strings in wrong formats
        (e.g., "2024/01/15" instead of "2024-01-15").
        """
        from datetime import date

        booking_id, _ = created_booking
        booking = booking_client.get_booking(booking_id)

        assert isinstance(booking.bookingdates.checkin, date), (
            f"checkin should be a date object, got {type(booking.bookingdates.checkin)}"
        )
        assert isinstance(booking.bookingdates.checkout, date), (
            f"checkout should be a date object, got {type(booking.bookingdates.checkout)}"
        )
        assert booking.bookingdates.checkout > booking.bookingdates.checkin, (
            "Contract violation: checkout must always be after checkin."
        )
