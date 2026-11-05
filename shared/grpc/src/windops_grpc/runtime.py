"""Run a service: gRPC, health and metrics endpoints, logs, traces and graceful shutdown."""

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import grpc
import uvicorn
from fastapi import APIRouter, FastAPI, Response
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from windops_observability import ServiceInfo, Telemetry, configure_logging, configure_telemetry

from windops_grpc.interceptor import ServerContextInterceptor
from windops_grpc.settings import Settings
from windops_grpc.tls import server_credentials, stop_when_certificate_changes

logger = logging.getLogger("windops.service")

ReadinessCheck = Callable[[], Awaitable[bool]]
Register = Callable[[grpc.aio.Server], None]
ExceptionHandler = Callable[[Any, Exception], Awaitable[Any]]
# Work that runs beside the servers, such as an event consumer. It returns once `stop` is set.
Background = Callable[[asyncio.Event], Awaitable[None]]


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
        http_router: APIRouter | None = None,
        http_exception_handlers: Mapping[type[Exception], ExceptionHandler] | None = None,
        api_over_https: bool = False,
        install_global_telemetry: bool = True,
    ) -> None:
        self.name = name
        self._settings = settings
        self._register = register
        self._readiness_checks = readiness_checks
        self._http_router = http_router
        self._http_exception_handlers = dict(http_exception_handlers or {})
        # The API only over TLS, and plain HTTP only for probes and Prometheus, which carry no data.
        self._api_over_https = api_over_https and settings.tls_dir is not None
        self._https: _HttpServer | None = None
        self._https_task: asyncio.Task[None] | None = None
        self.https_port = settings.https_port
        self._install_global = install_global_telemetry
        self._health = health.aio.HealthServicer()
        self._serving = False
        self._grpc: grpc.aio.Server | None = None
        self._http: _HttpServer | None = None
        self._http_task: asyncio.Task[None] | None = None
        self._telemetry: Telemetry | None = None
        self.grpc_port = settings.grpc_port
        self.http_port = settings.http_port

    def _http_app(self, *, with_api: bool = True) -> FastAPI:
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

        if with_api and self._http_router is not None:
            app.include_router(self._http_router)
        for kind, handler in self._http_exception_handlers.items():
            app.add_exception_handler(kind, handler)
        return app

    @staticmethod
    async def _serve(config: uvicorn.Config) -> tuple["_HttpServer", "asyncio.Task[None]"]:
        server = _HttpServer(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("The HTTP server stopped during startup")
            await asyncio.sleep(0.01)
        return server, task

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
        address = f"[::]:{settings.grpc_port}"
        if settings.tls_dir is None:
            logger.warning("gRPC is running without TLS, which only tests may do")
            self.grpc_port = server.add_insecure_port(address)
        else:
            self.grpc_port = server.add_secure_port(address, server_credentials(settings.tls_dir))
        await server.start()
        self._grpc = server

        config = uvicorn.Config(
            self._http_app(with_api=not self._api_over_https),
            host="0.0.0.0",  # noqa: S104  # Kubernetes probes and Prometheus reach the pod address.
            port=settings.http_port,
            log_config=None,
            access_log=False,
        )
        self._http, self._http_task = await self._serve(config)
        if self._api_over_https and settings.tls_dir is not None:
            secure = uvicorn.Config(
                self._http_app(),
                host="0.0.0.0",  # noqa: S104  # The gateway reaches the pod address.
                port=settings.https_port,
                log_config=None,
                access_log=False,
                ssl_certfile=str(settings.tls_dir / "tls.crt"),
                ssl_keyfile=str(settings.tls_dir / "tls.key"),
            )
            self._https, self._https_task = await self._serve(secure)
            self.https_port = self._https.servers[0].sockets[0].getsockname()[1]
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
        if self._https is not None and self._https_task is not None:
            self._https.should_exit = True
            await self._https_task
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
    http_router: APIRouter | None = None,
    http_exception_handlers: Mapping[type[Exception], ExceptionHandler] | None = None,
    api_over_https: bool = False,
    background: Sequence[Background] = (),
) -> None:
    """Run a service until SIGTERM or SIGINT, then shut it down gracefully.

    If background work fails, the service stops and raises, so Kubernetes restarts it.
    """
    settings = Settings.from_env(os.environ)
    runtime = ServiceRuntime(
        name,
        settings,
        register,
        readiness_checks=readiness_checks,
        http_router=http_router,
        http_exception_handlers=http_exception_handlers,
        api_over_https=api_over_https,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, stop.set)
    await runtime.start()
    tasks = [asyncio.create_task(_await(item(stop))) for item in background]
    if settings.tls_dir is not None:
        tasks.append(asyncio.create_task(stop_when_certificate_changes(settings.tls_dir, stop)))
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait([stopping, *tasks], return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    await runtime.stop()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def _await(work: Awaitable[None]) -> None:
    await work
