from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TransferEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    amount_cents: int = Field(default=800000, ge=1, le=100000000, strict=True)
    currency: Literal["SGD"] = "SGD"
    customer_id: Literal["demo-customer"] = "demo-customer"
    payee: Literal["New payee • 4821", "Known payee • 1930"] = "New payee • 4821"
    new_payee: bool = True
    unusual_device: bool = True
    recent_payee_count: int = Field(default=3, ge=0, le=20, strict=True)
    preferred_channel: Literal["WHATSAPP", "RCS", "SMS"] = "RCS"
    rcs_available: bool = True
    # None means unavailable/not observed; it must not be treated as proof of no call.
    active_call: bool | None = Field(default=None, strict=True)


class Reply(StrictModel):
    message_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    text: str = Field(min_length=1, max_length=1000)
    # Set when the customer tapped a reply button/suggestion; text is then the button title.
    button_id: Literal["reply:verify", "reply:dispute", "reply:coached", "reply:cancel"] | None = None


class Confirmation(StrictModel):
    command_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    token: str = Field(min_length=16, max_length=200)
    approved_actions: list[Literal["pause_transfer", "create_scam_case"]]
    # Set when consent came from a messaging-app button (device emulator) rather than the bank app.
    channel: Literal["WHATSAPP", "RCS", "SMS"] | None = None
