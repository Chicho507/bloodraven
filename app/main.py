import asyncio
from contextlib import asynccontextmanager
import ipaddress
import os
from pathlib import Path
import socket
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.branding import logo_path, page
from app.config import load_settings
from app.control import Control, ControlError
from app.security import install_security
from app.snmp import poll_device
from app.store import Store, stamp
from app.telegram_bot import format_command, run_bot


async def collector(settings, store, control, stop):
    gate = asyncio.Semaphore(4)

    async def one(device):
        async with gate:
            if device["profile"] == "cisco_sg350" and (not device.get("firmware_reviewed") or not device.get("firmware")):
                store.pending(device, "Revisión de firmware SG350 pendiente; no se ha consultado el equipo")
                return
            try:
                configured = control.poll_config(device)
            except ControlError:
                return
            references = configured.get("snmp", {})
            if not configured.get("_credentials") and any(not os.getenv(references.get(k, ""), "") for k in ("username_env", "auth_password_env", "privacy_password_env")):
                store.pending(device, "Faltan credenciales SNMP en el servidor; no se ha consultado el equipo")
                return
            try:
                # Resolve once and pin that exact address for this poll. Only configured networks may be queried.
                networks = [ipaddress.ip_network(value.strip()) for value in os.getenv("BR_ALLOWED_NETWORKS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16").split(",")]
                addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(configured["host"], configured.get("port", 161), family=socket.AF_INET, type=socket.SOCK_DGRAM), timeout=3)
                target = ipaddress.ip_address(addresses[0][4][0])
                if target.is_loopback or target.is_link_local or target.is_multicast or not any(target in network for network in networks):
                    store.pending(device, "La IP resuelta está fuera de las redes de gestión autorizadas")
                    return
                configured["host"] = str(target)
                result = await poll_device(configured)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {"ok": False, "error": "Error de recolección; revisar configuración"}
            current = next((d for d in settings.devices if d["id"] == device["id"]), None)
            if current and current.get("revision") == device.get("revision"):
                store.record(current, result)

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
        control = Control(cfg)
        store = Store(cfg)
        store.seed_demo()
        application.state.settings = cfg
        application.state.store = store
        application.state.control = control
        stop = asyncio.Event()
        tasks = []
        if start_workers:
            tasks.append(asyncio.create_task(collector(cfg, store, control, stop)))
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
            control.close()

    application = FastAPI(title="BloodRaven", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    install_security(application)

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
        return page(static / "index.html")

    @application.get("/static/{filename}")
    def static_asset(filename: str):
        if filename not in {"styles.css", "app.js", "brand.css", "auth.css", "auth.js", "session.js", "manage.css", "manage.js", "account.js", "brand-logo.png"}:
            raise HTTPException(404)
        if filename == "brand-logo.png":
            if not logo_path().is_file():
                raise HTTPException(404)
            return FileResponse(logo_path(), media_type="image/png")
        return FileResponse(static / filename)

    return application


app = create_app()
