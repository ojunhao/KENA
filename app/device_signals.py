"""Minimal call-state boundary. No phone numbers, call history or audio are accepted."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol


@dataclass(frozen=True)
class CallStateObservation:
    active_call: bool | None
    observed_at: datetime
    source: Literal["simulator", "android_telephony"]

    def fresh_value(self, at: datetime, max_age_seconds: int = 30) -> bool | None:
        if self.observed_at.tzinfo is None or at.tzinfo is None:
            raise ValueError("Call-state timestamps must include a timezone")
        age = (at - self.observed_at).total_seconds()
        return self.active_call if 0 <= age <= max_age_seconds else None


class CallStateProvider(Protocol):
    def observe(self) -> CallStateObservation: ...


def normalize_telephony_state(state: int | None, *, permission_granted: bool) -> bool | None:
    """Android constants: IDLE=0, RINGING=1, OFFHOOK=2. OFFHOOK includes dialing/held."""
    if not permission_granted or type(state) is not int:
        return None
    if state == 0:
        return False
    if state == 2:
        return True
    return None


class SimulatedCallStateProvider:
    def __init__(self, active_call: bool | None):
        self.active_call = active_call

    def observe(self) -> CallStateObservation:
        return CallStateObservation(self.active_call, datetime.now(timezone.utc), "simulator")


def call_context(active_call: bool | None) -> dict:
    observation = SimulatedCallStateProvider(active_call).observe()
    value = observation.fresh_value(observation.observed_at)
    return {"state": "active" if value is True else "inactive" if value is False else "unknown",
            "source": observation.source, "observed_at": observation.observed_at.isoformat(),
            "scope": "simulated telephony state; no call content"}
