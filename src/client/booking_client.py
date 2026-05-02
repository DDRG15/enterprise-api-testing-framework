"""
src/client/booking_client.py
==============================
Domain-specific client for the Restful-Booker API.

Wraps ApiClient with typed methods that return validated Pydantic models.
Tests never call requests directly — they call this client and receive
typed, validated objects. Schema drift is caught at deserialization.

This layer also isolates tests from HTTP mechanics: a test that calls
`create_booking()` doesn't know or care about status codes — it receives
a `CreateBookingResponse` or a clear exception.
"""
from __future__ import annotations

import structlog
from pydantic import ValidationError

from src.client.base_client import ApiClient
from src.models.booking import (
    AuthTokenResponse,
    BookingPayload,
    BookingResponse,
    BookingSummary,
    CreateBookingResponse,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


class BookingClient:
    """
    All interactions with /booking and /auth endpoints.
    Returns typed Pydantic models. Raises on non-2xx or schema violations.
    """

    def __init__(self, api_client: ApiClient) -> None:
        self._client = api_client

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> str:
        """
        Obtain an auth token from POST /auth.
        Sets the token on the underlying session for subsequent calls.
        Returns the raw token string.
        """
        response = self._client.post(
            "/auth",
            json={"username": username, "password": password},
        )
        response.raise_for_status()

        try:
            auth = AuthTokenResponse.model_validate(response.json())
        except ValidationError as exc:
            logger.error("auth_response_schema_violation", error=str(exc))
            raise

        self._client.set_auth_token(auth.token)
        logger.info("authentication_success")
        return auth.token

    # ------------------------------------------------------------------
    # CRUD Operations
    # ------------------------------------------------------------------

    def create_booking(self, payload: BookingPayload) -> CreateBookingResponse:
        """POST /booking — returns CreateBookingResponse with server-assigned ID."""
        response = self._client.post(
            "/booking",
            json=payload.model_dump(mode="json"),
        )
        response.raise_for_status()
        return self._parse(response.json(), CreateBookingResponse, "create_booking")

    def get_booking(self, booking_id: int) -> BookingResponse:
        """GET /booking/{id} — returns the full booking object."""
        response = self._client.get(f"/booking/{booking_id}")
        response.raise_for_status()
        return self._parse(response.json(), BookingResponse, "get_booking")

    def list_bookings(
        self,
        firstname: str | None = None,
        lastname: str | None = None,
    ) -> list[BookingSummary]:
        """GET /booking — returns a list of booking ID summaries."""
        params: dict[str, str] = {}
        if firstname:
            params["firstname"] = firstname
        if lastname:
            params["lastname"] = lastname

        response = self._client.get("/booking", params=params or None)
        response.raise_for_status()

        try:
            return [BookingSummary.model_validate(item) for item in response.json()]
        except ValidationError as exc:
            logger.error("list_bookings_schema_violation", error=str(exc))
            raise

    def update_booking(
        self, booking_id: int, payload: BookingPayload
    ) -> BookingResponse:
        """PUT /booking/{id} — full replacement. Returns updated booking."""
        response = self._client.put(
            f"/booking/{booking_id}",
            json=payload.model_dump(mode="json"),
        )
        response.raise_for_status()
        return self._parse(response.json(), BookingResponse, "update_booking")

    def partial_update_booking(
        self, booking_id: int, partial_payload: dict
    ) -> BookingResponse:
        """PATCH /booking/{id} — partial update. Returns updated booking."""
        response = self._client.patch(
            f"/booking/{booking_id}",
            json=partial_payload,
        )
        response.raise_for_status()
        return self._parse(response.json(), BookingResponse, "partial_update_booking")

    def delete_booking(self, booking_id: int) -> None:
        """
        DELETE /booking/{id}.
        Returns None on success. Raises on non-2xx.
        The caller MUST handle teardown separately — see conftest.py.
        """
        response = self._client.delete(f"/booking/{booking_id}")
        response.raise_for_status()
        logger.info("booking_deleted", booking_id=booking_id)

    def booking_exists(self, booking_id: int) -> bool:
        """
        Non-raising existence check. Returns False on 404, True on 200.
        Used in teardown to confirm deletion without raising.
        """
        response = self._client.get(f"/booking/{booking_id}")
        return response.status_code == 200

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(data: dict, model: type, operation: str):
        """Deserialize a dict into a Pydantic model. Log schema violations."""
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            logger.error(
                "response_schema_violation",
                operation=operation,
                model=model.__name__,
                raw_data=data,
                error=str(exc),
            )
            raise
