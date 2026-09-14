"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import build_api_router, build_device_router
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.permissions import module_group_tree
from app.db.indexes import ensure_indexes
from app.db.mongo import close_mongo_connection, connect_to_mongo, ping

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)
# The Mongo driver logs every heartbeat at DEBUG; useful only when chasing
# a connection problem, deafening otherwise.
for noisy in ("pymongo", "pymongo.topology", "pymongo.connection", "pymongo.serverSelection",
              "pymongo.command", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("scholarly")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    # Index sync talks to Atlas 70+ times and can take 20s. It is idempotent and
    # the app serves correctly (just less efficiently) while it runs, so it must
    # not hold up the health check a platform like Render is waiting on.
    index_task = asyncio.create_task(ensure_indexes())
    log.info(
        "%s API ready — mode=%s env=%s",
        settings.app_name, settings.deployment_mode, settings.environment,
    )
    yield
    index_task.cancel()
    await close_mongo_connection()


def create_app() -> FastAPI:
    description = (
        "Multi-tenant ERP for schools, colleges and universities.\n\n"
        f"**Deployment mode:** `{settings.deployment_mode}` — "
        + (
            "institutions share this deployment; every request is scoped to one tenant."
            if settings.is_saas
            else "a single institution owns this deployment. No plan limits, every module on."
        )
    )
    app = FastAPI(
        title=f"{settings.app_name} ERP API",
        description=description,
        version="0.1.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=(
            rf"^https?://([a-z0-9-]+\.)?{settings.tenant_base_domain.replace('.', chr(92) + '.')}$"
            if settings.tenant_base_domain else None
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Duration"],
    )

    @app.middleware("http")
    async def timing(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Duration"] = f"{(time.perf_counter() - started) * 1000:.1f}ms"
        return response

    # ── Error shape: always {detail, code, meta} ──────────────────────────
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return ORJSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "meta": exc.meta},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return ORJSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": "http_error", "meta": {}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        fields = [
            {
                "field": ".".join(str(p) for p in err["loc"] if p not in ("body", "query")),
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        return ORJSONResponse(
            status_code=422,
            content={
                "detail": "Please check the highlighted fields",
                "code": "validation_error",
                "meta": {"fields": fields},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return ORJSONResponse(
            status_code=500,
            content={
                "detail": str(exc) if settings.debug else "Something went wrong on our side",
                "code": "internal_error",
                "meta": {},
            },
        )

    # ── Meta endpoints ────────────────────────────────────────────────────
    @app.get("/", tags=["Meta"], summary="Service banner")
    async def root():
        return {
            "name": f"{settings.app_name} ERP API",
            "version": "0.1.0",
            "deployment_mode": settings.deployment_mode,
            "environment": settings.environment,
            "docs": "/docs" if not settings.is_production else None,
        }

    @app.get("/health", tags=["Meta"], summary="Liveness and database check")
    async def health():
        db_ok = await ping()
        return {
            "status": "ok" if db_ok else "degraded",
            "database": "up" if db_ok else "down",
            "deployment_mode": settings.deployment_mode,
        }

    @app.get(f"{settings.api_v1_prefix}/meta/modules", tags=["Meta"],
             summary="Module & permission catalogue")
    async def modules():
        return {
            "deployment_mode": settings.deployment_mode,
            "groups": module_group_tree(),
        }

    app.include_router(build_api_router(), prefix=settings.api_v1_prefix)
    app.include_router(build_device_router())
    return app


app = create_app()
