from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from .core.sentry_setup import init_sentry

load_dotenv()
init_sentry()

app = FastAPI(title="VitalTwin DE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://vitaltwin.de",
        "https://www.vitaltwin.de",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Central error-event logging (Founder OS internal foundation #7).

    Starlette dispatches by most-specific registered handler, so any
    `HTTPException` a route raises intentionally still goes to FastAPI's own
    built-in handler, never here — this only ever sees genuinely unhandled
    exceptions (real bugs), which is exactly the honest, narrow scope of
    this internal error log (see docs/FOUNDER_OS_MISSING_INTEGRATIONS.md for
    what a real external tool like Sentry would add on top)."""
    from .core.error_events import log_error_event
    from .core.system_events import log_system_event

    log_error_event(source=str(request.url.path), error_type=type(exc).__name__, message=str(exc))
    log_system_event(
        event_type="unhandled_exception", severity="error", source=str(request.url.path),
        message=type(exc).__name__,
    )

    import sentry_sdk

    sentry_sdk.capture_exception(exc)  # no-op if SENTRY_DSN is unset

    return JSONResponse(status_code=500, content={"detail": "Interner Serverfehler."})


@app.on_event("startup")
async def _log_startup_event() -> None:
    from .core.system_events import log_system_event

    log_system_event(event_type="server_start", severity="info", source="app.main", message="Backend gestartet.")


from .routers import twin, users, payments, beta, profile, chat, recommendations, twin_memory, daily_planning, privacy, admin, contact, notifications, affiliate, affiliate_admin, founder, founder_briefing, founder_tasks, founder_approval, founder_business_coach, founder_affiliate_intelligence, founder_automation, founder_ceo_intelligence, founder_documentation, founder_autopilot, health, google_health, health_connect, content, family

app.include_router(users.router, prefix="/api/users")
app.include_router(twin.router, prefix="/api/twin")
app.include_router(payments.router, prefix="/api/payments")
app.include_router(beta.router, prefix="/api/beta")
app.include_router(profile.router, prefix="/api/profile")
app.include_router(chat.router, prefix="/api/chat")
app.include_router(recommendations.router, prefix="/api/recommendations")
app.include_router(twin_memory.router, prefix="/api/memory")
app.include_router(daily_planning.router, prefix="/api/planning")
app.include_router(privacy.router, prefix="/api/privacy")
app.include_router(admin.router, prefix="/api/admin")
app.include_router(contact.router, prefix="/api/contact")
app.include_router(notifications.router, prefix="/api/notifications")
app.include_router(affiliate.router, prefix="/api/affiliate")
app.include_router(affiliate_admin.router, prefix="/api/admin/affiliate")
app.include_router(health.router, prefix="/api/health", tags=["Health Data"])
app.include_router(google_health.router, prefix="/api/health", tags=["Google Health"])
app.include_router(health_connect.router, prefix="/api/health", tags=["Health Connect"])
app.include_router(content.router, prefix="/api/content", tags=["Public Content"])
app.include_router(family.router, prefix="/api/family", tags=["Family"])
app.include_router(founder.router, prefix="/api/admin/founder")
app.include_router(founder_briefing.router, prefix="/api/admin/founder")
app.include_router(founder_tasks.router, prefix="/api/admin/founder")
app.include_router(founder_approval.router, prefix="/api/admin/founder")
app.include_router(founder_business_coach.router, prefix="/api/admin/founder")
app.include_router(founder_affiliate_intelligence.router, prefix="/api/admin/founder")
app.include_router(founder_automation.router, prefix="/api/admin/founder")
app.include_router(founder_ceo_intelligence.router, prefix="/api/admin/founder")
app.include_router(founder_documentation.router, prefix="/api/admin/founder")
app.include_router(founder_autopilot.router, prefix="/api/admin/founder")

@app.get("/")
def root():
    return {"message": "VitalTwin Backend läuft"}