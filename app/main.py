import os
from pathlib import Path
from fastapi import FastAPI, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .models import Confirmation, Reply, TransferEvent
from .service import JourneyService
from .store import Store


def create_app(db_path=None, agent=None, transport=None, clock=None):
    application = FastAPI(title="KENA synthetic demo", version="0.1.0")
    kwargs = {"agent": agent, "transport": transport}
    if clock:
        kwargs["clock"] = clock
    service = JourneyService(Store(db_path or os.getenv("KENA_DB", "data/kena.db")), **kwargs)
    service.recover_queued()
    application.state.service = service
    static = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=static), name="static")

    @application.get("/")
    def home():
        return FileResponse(static / "index.html")

    @application.get("/healthz")
    def health():
        return {"status": "ok", "mode": "synthetic-local-demo"}

    @application.get("/api/dashboard")
    def dashboard():
        return service.snapshot()

    @application.post("/api/events", status_code=201)
    def event(event: TransferEvent):
        return service.create(event)

    @application.get("/api/journeys/{journey_id}")
    def journey(journey_id: str):
        return service.read(journey_id)

    @application.get("/api/customer/{journey_id}")
    def customer(journey_id: str, x_demo_customer: str = Header()):
        return service.customer(journey_id, x_demo_customer)

    @application.post("/api/customer/{journey_id}/replies")
    def reply(journey_id: str, reply: Reply, x_demo_customer: str = Header()):
        return service.reply(journey_id, x_demo_customer, reply)

    @application.post("/api/customer/{journey_id}/confirm")
    def confirm(journey_id: str, confirmation: Confirmation, x_demo_customer: str = Header()):
        return service.confirm(journey_id, x_demo_customer, confirmation)

    return application


app = create_app()
