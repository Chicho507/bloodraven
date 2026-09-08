"""Portal authentication, CSRF validation and role-checked management endpoints."""
import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.control import ControlError, InventoryInput, PROFILES, VERSION


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=128)


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=120)
    name: str = Field(min_length=2, max_length=80)
    role: str
    active: bool = True
    password: str = Field(default="", max_length=128)


class PasswordInput(BaseModel):
    current: str = Field(min_length=1, max_length=128)
    new: str = Field(min_length=16, max_length=128)


class RevisionInput(BaseModel):
    revision: int = Field(ge=1)


def install_security(application):
    static = Path(__file__).parent / "static"
    public = {"/healthz", "/login", "/api/auth/context", "/api/auth/login",
              "/static/auth.css", "/static/auth.js", "/static/sertracen.png"}

    def cookie_names(cfg):
        prefix = "__Host-" if cfg.secure_cookies else "dev-"
        return prefix + "bloodraven", prefix + "br-preauth"

    def headers(response, cfg):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"})
        if cfg.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @application.exception_handler(ControlError)
    async def control_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response includes submitted passwords in `input`.
        return JSONResponse({"detail": "Revisa los campos del formulario.", "fields": [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]}, status_code=422)

    @application.middleware("http")
    async def protect(request: Request, call_next):
        cfg, control = request.app.state.settings, request.app.state.control
        name, pre_name = cookie_names(cfg)
        token = request.cookies.get(name, "")
        session = control.session(token, touch=False)
        request.state.user = session["user"] if session else None
        request.state.session = session
        request.state.session_token = token
        path = request.url.path
        if path not in public and not session:
            response = JSONResponse({"detail": "Tu sesión ha terminado. Inicia sesión."}, status_code=401) if path.startswith("/api/") else RedirectResponse("/login", status_code=303)
            return headers(response, cfg)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "").rstrip("/")
            expected = request.cookies.get(pre_name, "") if path == "/api/auth/login" else session["csrf"] if session else ""
            provided = request.headers.get("x-csrf-token", "")
            if origin != cfg.public_origin or len(expected) < 32 or not hmac.compare_digest(expected.encode(), provided.encode()):
                return headers(JSONResponse({"detail": "Solicitud inválida. Recarga la página."}, status_code=403), cfg)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return headers(JSONResponse({"detail": "Se requiere JSON."}, status_code=415), cfg)
            payload = bytearray()
            async for chunk in request.stream():
                payload.extend(chunk)
                if len(payload) > 32768:
                    return headers(JSONResponse({"detail": "Solicitud demasiado grande."}, status_code=413), cfg)
            request._body = bytes(payload)
            if session:
                control.session(token, touch=True)
        response = await call_next(request)
        return headers(response, cfg)

    def admin(request):
        user = request.state.user
        if not user or user["role"] != "admin":
            raise HTTPException(403, "Esta acción requiere un administrador.")
        return user["username"]

    def synchronize(request, device_id=None):
        store = request.app.state.store
        with store.lock:
            if device_id:
                device = next((d for d in request.app.state.settings.devices if d["id"] == device_id), None)
                if device:
                    store.pending(device, "Configuración guardada; esperando el siguiente ciclo" if device["enabled"] else "Monitoreo pausado")

    @application.get("/login")
    def login_page(request: Request):
        return RedirectResponse("/", status_code=303) if request.state.user else FileResponse(static / "login.html")

    @application.get("/api/auth/context")
    def context(request: Request):
        cfg = request.app.state.settings
        name, pre_name = cookie_names(cfg)
        session = request.app.state.control.session(request.cookies.get(name, ""))
        csrf = session["csrf"] if session else secrets.token_urlsafe(32)
        response = JSONResponse({"user": session["user"] if session else None, "csrf": csrf, "version": VERSION,
                                 "idle_minutes": 30, "maximum_hours": 8})
        if not session:
            response.set_cookie(pre_name, csrf, httponly=True, secure=cfg.secure_cookies, samesite="strict", max_age=600, path="/")
        return response

    @application.post("/api/auth/login")
    def login(body: Login, request: Request):
        control, cfg = request.app.state.control, request.app.state.settings
        token, csrf, user = control.authenticate(body.username, body.password, request.client.host if request.client else "unknown")
        name, pre_name = cookie_names(cfg)
        response = JSONResponse({"user": user, "csrf": csrf, "version": VERSION})
        response.set_cookie(name, token, httponly=True, secure=cfg.secure_cookies, samesite="strict", path="/", max_age=28800)
        response.delete_cookie(pre_name, path="/", secure=cfg.secure_cookies, httponly=True, samesite="strict")
        return response

    @application.post("/api/auth/logout")
    def logout(request: Request):
        cfg = request.app.state.settings
        request.app.state.control.logout(request.state.session_token, request.state.user["username"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_names(cfg)[0], path="/", secure=cfg.secure_cookies, httponly=True, samesite="strict")
        return response

    @application.post("/api/auth/keepalive")
    def keepalive():
        return {"ok": True}

    @application.post("/api/auth/password")
    def password(body: PasswordInput, request: Request):
        request.app.state.control.change_password(request.state.user, body.current, body.new)
        return {"ok": True, "reauthenticate": True}

    @application.get("/manage")
    def management(request: Request):
        admin(request)
        return FileResponse(static / "manage.html")

    @application.get("/account")
    def account_page():
        return FileResponse(static / "account.html")

    @application.get("/api/inventory")
    def inventory(request: Request):
        admin(request)
        return {"devices": request.app.state.control.inventory(), "profiles": PROFILES, "mode": request.app.state.settings.mode}

    @application.post("/api/inventory", status_code=201)
    def add_device(body: InventoryInput, request: Request):
        actor = admin(request)
        request.app.state.control.save_device(body, actor)
        synchronize(request, body.id)
        return {"id": body.id, "ok": True}

    @application.put("/api/inventory/{device_id}")
    def edit_device(device_id: str, body: InventoryInput, request: Request):
        actor = admin(request)
        if device_id != body.id:
            raise HTTPException(400, "El identificador del equipo no puede cambiar.")
        request.app.state.control.save_device(body, actor, editing=True)
        synchronize(request, body.id)
        return {"id": body.id, "ok": True}

    @application.post("/api/inventory/{device_id}/archive")
    def archive(device_id: str, body: RevisionInput, request: Request):
        actor = admin(request)
        request.app.state.control.archive_device(device_id, body.revision, actor)
        return {"ok": True}

    @application.get("/api/admin/users")
    def users(request: Request):
        admin(request)
        return {"users": request.app.state.control.users()}

    @application.post("/api/admin/users", status_code=201)
    def add_user(body: UserInput, request: Request):
        actor = admin(request)
        try:
            request.app.state.control.save_user(**body.model_dump(), actor=actor)
        except ValueError as exc:
            raise ControlError(str(exc))
        return {"ok": True}

    @application.put("/api/admin/users/{user_id}")
    def update_user(user_id: str, body: UserInput, request: Request):
        actor = admin(request)
        try:
            request.app.state.control.save_user(**body.model_dump(), actor=actor, user_id=user_id)
        except ValueError as exc:
            raise ControlError(str(exc))
        return {"ok": True}

    @application.get("/api/admin/audit")
    def audit(request: Request):
        admin(request)
        return {"events": request.app.state.control.audit()}
