import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from . import inbound
from .agent import build_agent
from .config import Settings
from .messaging import build_transport
from .models import Confirmation, Reply, TransferEvent
from .service import JourneyService
from .store import Store

log = logging.getLogger("kena")


def create_app(db_path=None, agent=None, transport=None, clock=None, settings=None, sns_verifier=None, sns_confirmer=None):
    settings = settings or Settings()
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_):
        # The no-response worker runs only while the app is served, not when tests construct it.
        worker = threading.Thread(target=run_worker, daemon=True, name="kena-worker")
        worker.start()
        yield
        stop.set()

    application = FastAPI(title="KENA intervention platform", version="0.3.0", lifespan=lifespan)
    kwargs = {"agent": agent or build_agent(settings), "transport": transport or build_transport(settings), "settings": settings}
    if clock:
        kwargs["clock"] = clock
    if settings.dynamo_table and not db_path:
        from .dynamo_store import DynamoStore
        store = DynamoStore(settings.dynamo_table)
    else:
        store = Store(db_path or os.getenv("KENA_DB", "data/kena.db"))
    service = JourneyService(store, **kwargs)
    service.recover_queued()
    application.state.service = service
    static = Path(__file__).parent / "static"
    verify = sns_verifier or inbound.verify
    confirm_subscription = sns_confirmer or inbound.confirm_subscription

    def process_events(events):
        for item in events:
            try:
                log.info("Inbound %s event: %s", item["kind"], service.handle_inbound(item))
            except Exception:
                log.exception("Inbound event failed")

    def run_worker():
        while not stop.wait(settings.worker_interval_seconds):
            try:
                service.tick()
            except Exception:
                log.exception("No-response worker tick failed")

    last_tick = [0.0]
    tick_lock = threading.Lock()

    # Signed-in browsers carry a session cookie derived from the configured credentials; API clients may send HTTP Basic.
    session_token = hmac.new(settings.basic_auth.encode(), b"kena-session", hashlib.sha256).hexdigest() if settings.basic_auth else ""

    if settings.basic_auth:
        @application.middleware("http")
        async def basic_auth(request: Request, call_next):
            # Provider webhooks authenticate by signature; the health check stays open for load balancers.
            if request.url.path in {"/healthz", "/webhooks/sns", "/login"}:
                return await call_next(request)
            expected = "Basic " + base64.b64encode(settings.basic_auth.encode()).decode()
            if (secrets.compare_digest(request.headers.get("authorization", ""), expected)
                    or secrets.compare_digest(request.cookies.get("kena_session", ""), session_token)):
                return await call_next(request)
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login?next=" + urllib.parse.quote(request.url.path), 303)
            return JSONResponse({"detail": "Authentication required"}, 401)

        @application.get("/login")
        def login_page():
            return FileResponse(static / "login.html")

        @application.post("/login")
        async def login(request: Request):
            form = urllib.parse.parse_qs((await request.body()).decode(errors="replace"))
            field = lambda name: (form.get(name) or [""])[0]
            target = field("next") if field("next").startswith("/") and not field("next").startswith("//") else "/"
            if not secrets.compare_digest(f"{field('username')}:{field('password')}", settings.basic_auth):
                return RedirectResponse("/login?failed=1&next=" + urllib.parse.quote(target), 303)
            response = RedirectResponse(target, 303)
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie("kena_session", session_token, max_age=14 * 24 * 3600, httponly=True, secure=secure, samesite="lax")
            return response

    if settings.origin_secret:
        @application.middleware("http")
        async def origin_only(request: Request, call_next):
            # Behind CloudFront: requests that bypass it (and its WAF) lack the secret origin header.
            if not secrets.compare_digest(request.headers.get("x-kena-origin", ""), settings.origin_secret):
                return JSONResponse({"detail": "Forbidden"}, 403)
            return await call_next(request)

    application.mount("/static", StaticFiles(directory=static), name="static")

    @application.get("/")
    def home():
        return FileResponse(static / "index.html")

    @application.get("/device")
    def device():
        """Customer phone emulator: WhatsApp, RCS or SMS styling for channels that are not sent live."""
        return FileResponse(static / "device.html")

    @application.get("/healthz")
    def health():
        return {"status": "ok", "mode": "live-aws-cds" if settings.live else "synthetic-local-demo"}

    @application.get("/api/dashboard")
    def dashboard():
        # Without a long-running worker (e.g. on Lambda), dashboard polling drives the no-response follow-ups.
        if settings.tick_on_request and time.time() - last_tick[0] >= settings.worker_interval_seconds and tick_lock.acquire(blocking=False):
            try:
                last_tick[0] = time.time()
                service.tick()
            except Exception:
                log.exception("No-response check failed")
            finally:
                tick_lock.release()
        return service.snapshot()

    @application.post("/api/events", status_code=201)
    def event(event: TransferEvent):
        return service.create(event)

    @application.get("/api/journeys/{journey_id}")
    def journey(journey_id: str):
        return service.read(journey_id)

    @application.delete("/api/journeys/{journey_id}")
    def delete_journey(journey_id: str):
        return service.delete(journey_id)

    @application.get("/api/customer/{journey_id}")
    def customer(journey_id: str, x_demo_customer: str = Header()):
        return service.customer(journey_id, x_demo_customer)

    @application.post("/api/customer/{journey_id}/replies")
    def reply(journey_id: str, reply: Reply, x_demo_customer: str = Header()):
        return service.reply(journey_id, x_demo_customer, reply)

    @application.post("/api/customer/{journey_id}/confirm")
    def confirm(journey_id: str, confirmation: Confirmation, x_demo_customer: str = Header()):
        return service.confirm(journey_id, x_demo_customer, confirmation)

    @application.post("/webhooks/sns")
    async def sns(request: Request, background: BackgroundTasks):
        """WhatsApp replies, button taps, delivery receipts and two-way SMS, published to SNS by AWS End User Messaging."""
        if not settings.sns_topic_arn:
            raise HTTPException(403, "Inbound messaging is not configured")
        try:
            envelope = json.loads(await request.body())
        except ValueError:
            raise HTTPException(400, "Invalid SNS payload")
        if not isinstance(envelope, dict) or envelope.get("TopicArn") != settings.sns_topic_arn:
            raise HTTPException(403, "Unexpected topic")
        try:
            verify(envelope)
        except inbound.InvalidSignature:
            raise HTTPException(403, "Invalid SNS signature")
        if envelope["Type"] == "SubscriptionConfirmation":
            confirm_subscription(envelope)
            return {"subscribed": True}
        if envelope["Type"] != "Notification":
            return {"ignored": envelope["Type"]}
        events = inbound.parse_notification(envelope.get("Message"))
        # Acknowledge SNS quickly; the agent may take several seconds. Duplicate deliveries are idempotent.
        background.add_task(process_events, events)
        return {"accepted": len(events)}

    return application


app = create_app(settings=Settings.from_env())
