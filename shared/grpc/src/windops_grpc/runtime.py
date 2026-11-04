"""Run a service: gRPC, health and metrics endpoints, logs, traces and graceful shutdown."""

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import grpc
import uvicorn
from fastapi import FastAPI, Response
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from windops_observability import ServiceInfo, Telemetry, configure_logging, configure_telemetry

from windops_grpc.interceptor import ServerContextInterceptor
from windops_grpc.settings import Settings

logger = logging.getLogger("windops.service")

ReadinessCheck = Callable[[], Awaitable[bool]]
Register = Callable[[grpc.aio.Server], None]


class _HttpServer(uvicorn.Server):
    """Leave signal handling to the service runtime, so shutdown has one owner."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class ServiceRuntime:
    """Owns one service's servers and its startup and shutdown order."""

    def __init__(
        self,
        name: str,
        settings: Settings,
        register: Register,
        *,
        readiness_checks: Sequence[ReadinessCheck] = (),
        install_global_telemetry: bool = True,
    ) -> None:
        self.name = name
        self._settings = settings
        self._register = register
        self._readiness_checks = readiness_checks
        self._install_global = install_global_telemetry
        self._health = health.aio.HealthServicer()
        self._serving = False
        self._grpc: grpc.aio.Server | None = None
        self._http: _HttpServer | None = None
        self._http_task: asyncio.Task[None] | None = None
        self._telemetry: Telemetry | None = None
        self.grpc_port = settings.grpc_port
        self.http_port = settings.http_port

    def _http_app(self) -> FastAPI:
        app = FastAPI(title=self.name, docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/health/live")
        async def live() -> dict[str, str]:
            return {"status": "alive"}

        @app.get("/health/ready")
        async def ready(response: Response) -> dict[str, str]:
            is_ready = await self.is_ready()
            response.status_code = 200 if is_ready else 503
            return {"status": "ready" if is_ready else "not_ready"}

        @app.get("/metrics")
        async def metrics() -> Response:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        return app

    async def is_ready(self) -> bool:
        if not self._serving:
            return False
        for check in self._readiness_checks:
            try:
                if not await check():
                    return False
            except Exception:
                logger.exception("A readiness check failed")
                return False
        return True

    async def _set_serving(self, serving: bool) -> None:
        status: Any = (
            health_pb2.HealthCheckResponse.SERVING
            if serving
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        await self._health.set("", status)
        if serving != self._serving:
            logger.info("Health changed", extra={"health": "serving" if serving else "not_serving"})
        self._serving = serving

    async def start(self) -> None:
        settings = self._settings
        info = ServiceInfo(self.name, settings.environment, settings.version)
        configure_logging(info, level=logging.getLevelNamesMapping()[settings.log_level])
        self._telemetry = configure_telemetry(
            info,
            otlp_endpoint=settings.otlp_endpoint,
            metric_reader=PrometheusMetricReader(),
            install_global=self._install_global,
            # Prometheus scrapes /metrics, so sending them too would store them twice.
            export_metrics_over_otlp=False,
        )
        logger.info(
            "Service starting",
            extra={"grpc_port": settings.grpc_port, "http_port": settings.http_port},
        )
        interceptor = ServerContextInterceptor(
            settings.identity_secret, meter_provider=self._telemetry.meter_provider
        )
        server = grpc.aio.server(interceptors=[interceptor])
        health_pb2_grpc.add_HealthServicer_to_server(self._health, server)
        await self._set_serving(False)
        self._register(server)
        self.grpc_port = server.add_insecure_port(f"[::]:{settings.grpc_port}")
        await server.start()
        self._grpc = server

        config = uvicorn.Config(
            self._http_app(),
            host="0.0.0.0",  # noqa: S104  # Kubernetes probes and Prometheus reach the pod address.
            port=settings.http_port,
            log_config=None,
            access_log=False,
        )
        self._http = _HttpServer(config)
        self._http_task = asyncio.create_task(self._http.serve())
        while not self._http.started:
            if self._http_task.done():
                self._http_task.result()
                raise RuntimeError("The HTTP server stopped during startup")
            await asyncio.sleep(0.01)
        self.http_port = self._http.servers[0].sockets[0].getsockname()[1]

        await self._set_serving(True)
        logger.info("Service started", extra={"grpc_port": self.grpc_port})

    async def stop(self) -> None:
        """Stop taking new work, let calls finish, then flush telemetry."""
        logger.info("Service stopping")
        await self._set_serving(False)
        if self._grpc is not None:
            await self._health.enter_graceful_shutdown()
            await self._grpc.stop(self._settings.shutdown_grace_seconds)
        if self._http is not None and self._http_task is not None:
            self._http.should_exit = True
            await self._http_task
        logger.info("Service stopped")
        if self._telemetry is not None:
            self._telemetry.shutdown()


async def run_service(
    name: str,
    register: Register,
    *,
    readiness_checks: Sequence[ReadinessCheck] = (),
) -> None:
    """Run a service until SIGTERM or SIGINT, then shut it down gracefully."""
    runtime = ServiceRuntime(
        name, Settings.from_env(os.environ), register, readiness_checks=readiness_checks
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, stop.set)
    await runtime.start()
    await stop.wait()
    await runtime.stop()
