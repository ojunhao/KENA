"""Runtime settings from environment variables. With nothing set, KENA is fully local and simulated."""
import os
import re
from dataclasses import dataclass
from pathlib import Path


def _env(name, default=None):
    value = os.getenv(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    live: bool = False
    live_channels: frozenset | None = None
    region: str = "ap-southeast-1"
    agent: str = "local"
    model: str = "anthropic.claude-opus-5-5"
    effort: str | None = None
    customer_name: str = "Alex"
    customer_phone: str | None = None
    customer_email: str | None = None
    wa_phone_number_id: str | None = None
    wa_template: str | None = None
    wa_template_language: str = "en"
    meta_api_version: str = "v20.0"
    sms_identity: str | None = None
    rcs_identity: str | None = None
    ses_sender: str | None = None
    sns_topic_arn: str | None = None
    alert_email: bool = False
    no_response_seconds: int = 90
    worker_interval_seconds: float = 5.0
    basic_auth: str | None = None

    @classmethod
    def from_env(cls):
        return cls(
            live=_env("KENA_LIVE") == "1",
            # Optional subset of channels sent through AWS, e.g. "SES"; the others go to the device emulator.
            live_channels=frozenset(c.strip().upper() for c in _env("KENA_LIVE_CHANNELS", "").split(",") if c.strip()) or None,
            region=_env("AWS_REGION", _env("AWS_DEFAULT_REGION", "ap-southeast-1")),
            agent=_env("KENA_AGENT", "local"),
            model=_env("KENA_MODEL", cls.model),
            effort=_env("KENA_EFFORT"),
            customer_name=_env("KENA_CUSTOMER_NAME", cls.customer_name),
            customer_phone=_env("KENA_CUSTOMER_PHONE"),
            customer_email=_env("KENA_CUSTOMER_EMAIL"),
            wa_phone_number_id=_env("KENA_WA_PHONE_NUMBER_ID"),
            wa_template=_env("KENA_WA_TEMPLATE"),
            wa_template_language=_env("KENA_WA_TEMPLATE_LANGUAGE", cls.wa_template_language),
            meta_api_version=_env("KENA_META_API_VERSION", cls.meta_api_version),
            sms_identity=_env("KENA_SMS_IDENTITY"),
            rcs_identity=_env("KENA_RCS_IDENTITY"),
            ses_sender=_env("KENA_SES_SENDER"),
            sns_topic_arn=_env("KENA_SNS_TOPIC_ARN"),
            alert_email=_env("KENA_ALERT_EMAIL", "1") == "1",
            no_response_seconds=int(_env("KENA_NO_RESPONSE_SECONDS", cls.no_response_seconds)),
            worker_interval_seconds=float(_env("KENA_WORKER_INTERVAL_SECONDS", cls.worker_interval_seconds)),
            basic_auth=_env("KENA_BASIC_AUTH"),
        )

    def channel_modes(self, channels):
        """'live' for channels sent through AWS, 'emulated' for channels shown on the device emulator."""
        live = set(channels) if self.live and self.live_channels is None else set(self.live_channels or ()) if self.live else set()
        return {c: "live" if c in live else "emulated" for c in sorted(channels)}

    @staticmethod
    def host_is_public(host):
        return host not in {"127.0.0.1", "localhost", "::1"}


def digits(phone):
    """WhatsApp reports senders as bare digits; compare numbers without '+' or spacing."""
    return "".join(c for c in phone or "" if c.isdigit())


def load_env_file(path=".env"):
    """Minimal .env support: KEY=value lines and # comments. Variables already set in the shell win."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)
