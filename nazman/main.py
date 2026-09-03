from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

from .config import get_settings, ensure_directories
from .database import init_db
from .utils.exceptions import NAZManError
from .api import (
    disks_router, pools_router, datasets_router,
    nfs_router, smb_router, snapshots_router, backup_router, zfs_backup_router,
    system_router, metrics_router, auth_router,
)

# Get application settings
settings = get_settings()

# Create FastAPI app
app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    description="Web-based ZFS NAS management for Ubuntu Server"
)

# Ensure required directories exist
ensure_directories()


@app.exception_handler(NAZManError)
async def nazman_error_handler(request: Request, exc: NAZManError):
    """Convert domain errors into a 400 with a meaningful detail message."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})

# Initialize database
@app.on_event("startup")
async def startup_event():
    init_db()

    # Start scheduler
    from .managers import scheduler_manager
    await scheduler_manager.start()

    # Start metrics recorder
    from .managers import metrics_manager
    await metrics_manager.start()

    # Initialise the persistent metrics store and load per-pool logging flags.
    from .managers.metrics_store import metrics_store
    metrics_store.connect()

    # Initialise + prune the persistent command log store.
    from .utils.command_log_store import command_log_store
    command_log_store.connect()
    command_log_store.prune()

@app.on_event("shutdown")
async def shutdown_event():
    from .managers import scheduler_manager
    await scheduler_manager.stop()

    from .managers import metrics_manager
    await metrics_manager.stop()

    from .managers.metrics_store import metrics_store
    metrics_store.close()

    from .utils.command_log_store import command_log_store
    command_log_store.close()

    # Ephemeral kernel-name knowledge does not survive restarts.
    from .managers.disk_manager import clear_device_map
    clear_device_map()

# Mount static files
static_path = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# Setup templates
templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

# Include API routers
app.include_router(system_router)
app.include_router(disks_router)
app.include_router(pools_router)
app.include_router(datasets_router)
app.include_router(nfs_router)
app.include_router(smb_router)
app.include_router(snapshots_router)
app.include_router(backup_router)
app.include_router(zfs_backup_router)
app.include_router(metrics_router)
app.include_router(auth_router)


# Web UI routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Redirect to dashboard."""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard page."""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/disks", response_class=HTMLResponse)
async def disks_page(request: Request):
    """Disks management page."""
    return templates.TemplateResponse(request, "disks.html")


@app.get("/pools", response_class=HTMLResponse)
async def pools_page(request: Request):
    """Pools management page."""
    return templates.TemplateResponse(request, "pools.html")


@app.get("/datasets", response_class=HTMLResponse)
async def datasets_page(request: Request):
    """Datasets management page."""
    return templates.TemplateResponse(request, "datasets.html")


@app.get("/nfs", response_class=HTMLResponse)
async def nfs_page(request: Request):
    """NFS management page."""
    return templates.TemplateResponse(request, "nfs.html")


@app.get("/smb", response_class=HTMLResponse)
async def smb_page(request: Request):
    """SMB management page."""
    return templates.TemplateResponse(request, "smb.html")


@app.get("/snapshots", response_class=HTMLResponse)
async def snapshots_page(request: Request):
    """Snapshots management page."""
    return templates.TemplateResponse(request, "snapshots.html")


@app.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    """Backup management page."""
    return templates.TemplateResponse(request, "backup.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    return templates.TemplateResponse(request, "settings.html")


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request):
    """Command log / events page."""
    return templates.TemplateResponse(request, "events.html")


@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    """Performance monitoring page."""
    return templates.TemplateResponse(request, "monitoring.html")
