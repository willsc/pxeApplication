from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.bootstrap import init_database
from app.database import check_database
from app.routers import assets, auth, boot, hosts, images, post_install, profiles, ui
from app.settings import settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if settings.enable_openapi else None,
        redoc_url="/redoc" if settings.enable_openapi else None,
        openapi_url="/openapi.json" if settings.enable_openapi else None,
        lifespan=lifespan,
    )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        check_database()
        return {"ok": True}

    app.include_router(boot.router)
    app.include_router(auth.router)
    app.include_router(hosts.router)
    app.include_router(assets.router)
    app.include_router(images.router)
    app.include_router(post_install.router)
    app.include_router(profiles.router)
    app.include_router(ui.router)
    return app


def run() -> None:
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.listen_host,
        port=settings.listen_port,
        proxy_headers=True,
    )


app = create_app()


if __name__ == "__main__":
    run()
