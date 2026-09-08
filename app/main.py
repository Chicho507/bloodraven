import asyncio
import base64
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import load_settings
from app.snmp import poll_device
from app.store import Store, stamp
from app.telegram_bot import format_command, run_bot


async def collector(settings, store, stop):
    gate = asyncio.Semaphore(4)

    async def one(device):
        async with gate:
            if device["profile"] == "cisco_sg350" and (not device.get("firmware_reviewed") or not device.get("firmware")):
                store.pending(device, "Revisión de firmware SG350 pendiente; no se ha consultado el equipo")
                return
            references = device["snmp"]
            if any(not os.getenv(references[k], "") for k in ("username_env", "auth_password_env", "privacy_password_env")):
                store.pending(device, "Faltan credenciales SNMP en el servidor; no se ha consultado el equipo")
                return
            try:
                result = await poll_device(device)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {"ok": False, "error": "Error de recolección; revisar configuración"}
            store.record(device, result)

    while not stop.is_set():
        started = time.monotonic()
        if settings.mode == "demo":
            store.demo_tick()
        else:
            await asyncio.gather(*(one(d) for d in settings.devices if d["enabled"]))
            store.set_meta("last_poll_at", stamp())
        store.prune()
        delay = max(1, settings.interval - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


def create_app(settings=None, start_workers=True):
    @asynccontextmanager
    async def lifespan(application):
        cfg = settings or load_settings()
        store = Store(cfg)
        store.seed_demo()
        application.state.settings = cfg
        application.state.store = store
        stop = asyncio.Event()
        tasks = []
        if start_workers:
            tasks.append(asyncio.create_task(collector(cfg, store, stop)))
            if cfg.telegram_enabled:
                tasks.append(asyncio.create_task(run_bot(store, stop, cfg.telegram_token, cfg.telegram_allowed_ids)))
        try:
            yield
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.close()

    application = FastAPI(title="BloodRaven", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.middleware("http")
    async def auth_and_headers(request: Request, call_next):
        if request.url.path != "/healthz":
            cfg = request.app.state.settings
            try:
                method, encoded = request.headers.get("authorization", "").split(" ", 1)
                if method.lower() != "basic":
                    raise ValueError()
                username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
                valid = hmac.compare_digest(username.encode(), cfg.username.encode()) & hmac.compare_digest(password.encode(), cfg.password.encode())
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                return JSONResponse({"detail": "Autenticación requerida"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="BloodRaven", charset="UTF-8"', "Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @application.get("/healthz")
    def health():
        return {"status": "ok"}

    @application.get("/api/overview")
    def overview(request: Request):
        return request.app.state.store.snapshot()

    @application.get("/api/devices/{device_id}/history")
    def history(device_id: str, request: Request, hours: int = Query(1, ge=1, le=24)):
        if device_id not in {d["id"] for d in request.app.state.settings.devices}:
            raise HTTPException(404, "Equipo desconocido")
        return {"mode": request.app.state.settings.mode, "samples": request.app.state.store.history(device_id, hours)}

    @application.get("/api/events")
    def events(request: Request, hours: int = Query(24, ge=1, le=2160)):
        return {"mode": request.app.state.settings.mode, "events": request.app.state.store.events(hours)}

    class Command(BaseModel):
        model_config = ConfigDict(extra="forbid")
        command: str = Field(min_length=1, max_length=160)

    @application.post("/api/demo/telegram")
    def simulated_command(body: Command, request: Request):
        store = request.app.state.store
        if request.app.state.settings.mode != "demo":
            raise HTTPException(404, "Simulador disponible solo en demo")
        return {"reply": format_command(body.command, store.snapshot(), store.events()), "simulated": True}

    static = Path(__file__).parent / "static"

    @application.get("/")
    def index():
        return FileResponse(static / "index.html")

    @application.get("/static/{filename}")
    def static_asset(filename: str):
        if filename not in {"styles.css", "app.js"}:
            raise HTTPException(404)
        return FileResponse(static / filename)

    return application


app = create_app()
