"""Vibry AI Core — FastAPI Application

Unified entry point. Mounts all routers on startup.
Run: python run.py
"""

import logging, sys
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import config

# Setup logging
DATA_DIR = Path(__file__).parent.parent / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "server.log"
logging.basicConfig(
    level=getattr(logging, config.server.log_level.upper(), logging.INFO),
    format="[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("vibry")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks"""
    import db
    db.init_db()
    log.info("SQLite DB initialized")

    # Load ASR config from DB
    config.doubao_asr.reload_from_db()
    try:
        asr_cfg = db.get_asr_config()
        if asr_cfg.get("asr_mode"):
            config.asr.mode = asr_cfg["asr_mode"]
    except Exception:
        pass

    # Load Chat / Embedding model config from DB
    config.chat.reload_from_db()
    config.embedding.reload_from_db()

    # Durable cognitive jobs process Sources into claims and project suggestions.
    from cognition.worker import CognitiveWorker
    cognitive_worker = CognitiveWorker()
    cognitive_worker.start()
    app.state.cognitive_worker = cognitive_worker
    cognitive_scheduler = None
    if config.cognition.scheduler_enabled:
        from cognition.scheduler import CognitiveScheduler
        cognitive_scheduler = CognitiveScheduler(config.cognition.nightly_insight_time)
        cognitive_scheduler.start()
        app.state.cognitive_scheduler = cognitive_scheduler

    log.info("=" * 55)
    log.info("Vibry AI Core - Cognitive gateway and AI backend")
    log.info(f"   Chat:    {config.chat.model} @ {config.chat.base_url}")
    log.info(f"   Embed:   {config.embedding.model} @ {config.embedding.base_url}")
    log.info(f"   ASR mode: {config.asr.mode}")
    log.info(f"   Voice ASR: {config.doubao_asr.voice_mode}")
    log.info("   Cognition: Source + Claims + Projects + Insights")
    log.info(f"   Listen: http://{config.server.host}:{config.server.port}")
    log.info("=" * 55)

    yield

    from services.proxy import _http_client
    if _http_client:
        await _http_client.aclose()
    cognitive_worker.stop()
    if cognitive_scheduler:
        cognitive_scheduler.stop()
    log.info("Vibry AI Core shut down")


# Create app
app = FastAPI(
    title="Vibry AI Core",
    description="Vibry.AI cognitive core, AI gateway, transcription, projects, and insights",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.server.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all routers
from routers import chat, admin, recordings, transcribe, voiceprint, health, cognition

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(recordings.router)
app.include_router(transcribe.router)
app.include_router(voiceprint.router)
app.include_router(health.router)
app.include_router(cognition.router)

# Mount static files (i18n JSON, etc.)
BASE_DIR = Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
