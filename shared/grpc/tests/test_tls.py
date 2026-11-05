"""Mutual TLS, over real connections, with real certificates from throwaway authorities."""

import asyncio
import datetime
import ipaddress
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from grpc_harness import SECRET, token
from grpc_health.v1 import health_pb2, health_pb2_grpc
from windops_grpc import ServiceRuntime, Settings, SettingsError, current_identity
from windops_grpc.identity import current_peer_service
from windops_grpc.tls import channel_credentials, fingerprint, stop_when_certificate_changes

SERVER_NAME = "windops-test-server"


@dataclass
class Authority:
    key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate

    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _validity(builder: x509.CertificateBuilder) -> x509.CertificateBuilder:
    now = datetime.datetime.now(datetime.UTC)
    return builder.not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(
        now + datetime.timedelta(hours=1)
    )


def make_authority(name: str = "test-ca") -> Authority:
    key = ec.generate_private_key(ec.SECP256R1())
    builder = _validity(
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(_name(name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    )
    return Authority(key, builder.sign(key, hashes.SHA256()))


def issue(authority: Authority, common_name: str, directory: Path, *, trusted: Authority) -> Path:
    """Write a certificate in the shape cert-manager's Secret takes."""
    key = ec.generate_private_key(ec.SECP256R1())
    alternative_names: list[x509.GeneralName] = [
        x509.DNSName(common_name),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    builder = _validity(
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(authority.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .add_extension(x509.SubjectAlternativeName(alternative_names), critical=False)
    )
    certificate = builder.sign(authority.key, hashes.SHA256())
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tls.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "tls.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (directory / "ca.crt").write_bytes(trusted.pem())
    return directory


@pytest.fixture
def authority() -> Authority:
    return make_authority()


def settings(tls_directory: Path | None) -> Settings:
    return Settings(
        environment="test",
        version="test",
        identity_secret=SECRET,
        grpc_port=0,
        http_port=0,
        shutdown_grace_seconds=1,
        tls_dir=tls_directory,
    )


@asynccontextmanager
async def running(directory: Path) -> AsyncIterator[ServiceRuntime]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    runtime = ServiceRuntime(
        "tls-test", settings(directory), lambda server: None, install_global_telemetry=False
    )
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


async def health_status(channel: grpc.aio.Channel) -> int:
    stub = health_pb2_grpc.HealthStub(channel)
    response = await stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=3)
    return int(response.status)


def secure(port: int, credentials: grpc.ChannelCredentials) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(f"localhost:{port}", credentials)


def test_a_client_from_the_same_authority_connects(tmp_path: Path, authority: Authority) -> None:
    async def scenario() -> None:
        server = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        client = issue(authority, "windops-client", tmp_path / "client", trusted=authority)
        async with (
            running(server) as runtime,
            secure(runtime.grpc_port, channel_credentials(client)) as channel,
        ):
            assert await health_status(channel) == health_pb2.HealthCheckResponse.SERVING

    asyncio.run(scenario())


def test_a_plain_connection_is_refused(tmp_path: Path, authority: Authority) -> None:
    async def scenario() -> None:
        server = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        async with (
            running(server) as runtime,
            grpc.aio.insecure_channel(f"localhost:{runtime.grpc_port}") as channel,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await health_status(channel)
            assert error.value.code() is grpc.StatusCode.UNAVAILABLE

    asyncio.run(scenario())


def test_a_client_with_no_certificate_is_refused(tmp_path: Path, authority: Authority) -> None:
    async def scenario() -> None:
        server = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        credentials = grpc.ssl_channel_credentials(root_certificates=authority.pem())
        async with running(server) as runtime, secure(runtime.grpc_port, credentials) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await health_status(channel)
            assert error.value.code() is grpc.StatusCode.UNAVAILABLE

    asyncio.run(scenario())


def test_a_client_from_another_authority_is_refused(tmp_path: Path, authority: Authority) -> None:
    async def scenario() -> None:
        rogue = make_authority("rogue-ca")
        server = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        # It trusts the real CA, so only its own certificate is wrong.
        client = issue(rogue, "windops-client", tmp_path / "client", trusted=authority)
        async with (
            running(server) as runtime,
            secure(runtime.grpc_port, channel_credentials(client)) as channel,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await health_status(channel)
            assert error.value.code() is grpc.StatusCode.UNAVAILABLE

    asyncio.run(scenario())


def test_a_client_refuses_a_server_from_another_authority(
    tmp_path: Path, authority: Authority
) -> None:
    """An impostor with its own CA cannot pose as a service."""

    async def scenario() -> None:
        rogue = make_authority("rogue-ca")
        server = issue(rogue, SERVER_NAME, tmp_path / "server", trusted=authority)
        client = issue(authority, "windops-client", tmp_path / "client", trusted=authority)
        async with (
            running(server) as runtime,
            secure(runtime.grpc_port, channel_credentials(client)) as channel,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await health_status(channel)
            assert error.value.code() is grpc.StatusCode.UNAVAILABLE

    asyncio.run(scenario())


def test_a_client_refuses_a_server_with_the_wrong_name(
    tmp_path: Path, authority: Authority
) -> None:
    """A valid certificate for one service does not let it pose as another."""

    async def scenario() -> None:
        server = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        client = issue(authority, "windops-client", tmp_path / "client", trusted=authority)
        async with running(server) as runtime:
            channel = grpc.aio.secure_channel(
                f"localhost:{runtime.grpc_port}",
                channel_credentials(client),
                options=(("grpc.ssl_target_name_override", "windops-other-service"),),
            )
            async with channel:
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    await health_status(channel)
                assert error.value.code() is grpc.StatusCode.UNAVAILABLE

    asyncio.run(scenario())


def test_a_service_sees_who_called_it_and_as_which_user(
    tmp_path: Path, authority: Authority
) -> None:
    """The certificate says which service is calling, and the token says for whom."""
    seen: list[tuple[str | None, str]] = []

    async def echo(request: bytes, context: object) -> bytes:
        seen.append((current_peer_service(), current_identity().tenant_id))
        return request

    def register(server: grpc.aio.Server) -> None:
        handler: Any = grpc.unary_unary_rpc_method_handler(echo)
        server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler("windops.test.Echo", {"Call": handler}),)
        )

    async def scenario() -> None:
        server_files = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        client_files = issue(authority, "windops-caller", tmp_path / "client", trusted=authority)
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        runtime = ServiceRuntime(
            "tls-test", settings(server_files), register, install_global_telemetry=False
        )
        await runtime.start()
        try:
            async with secure(runtime.grpc_port, channel_credentials(client_files)) as channel:
                call: Any = channel.unary_unary("/windops.test.Echo/Call")
                await call(b"hi", timeout=3, metadata=(("x-windops-identity", token()),))
        finally:
            await runtime.stop()
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    asyncio.run(scenario())
    assert seen == [("windops-caller", "tenant-a")]


def test_a_certificate_alone_does_not_replace_a_signed_identity(
    tmp_path: Path, authority: Authority
) -> None:
    """Both checks apply, so a valid service still needs a user or service identity (GRP-004)."""

    async def echo(request: bytes, context: object) -> bytes:
        return request

    def register(server: grpc.aio.Server) -> None:
        handler: Any = grpc.unary_unary_rpc_method_handler(echo)
        server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler("windops.test.Echo", {"Call": handler}),)
        )

    async def scenario() -> None:
        server_files = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        client_files = issue(authority, "windops-caller", tmp_path / "client", trusted=authority)
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        runtime = ServiceRuntime(
            "tls-test", settings(server_files), register, install_global_telemetry=False
        )
        await runtime.start()
        try:
            async with secure(runtime.grpc_port, channel_credentials(client_files)) as channel:
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    call: Any = channel.unary_unary("/windops.test.Echo/Call")
                    await call(b"hi", timeout=3)
                assert error.value.code() is grpc.StatusCode.UNAUTHENTICATED
        finally:
            await runtime.stop()
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    asyncio.run(scenario())


def test_a_renewed_certificate_stops_the_service_so_it_restarts(
    tmp_path: Path, authority: Authority
) -> None:
    async def scenario() -> None:
        directory = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        stop = asyncio.Event()
        watcher = asyncio.create_task(
            stop_when_certificate_changes(directory, stop, interval_seconds=0.01)
        )
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        issue(authority, SERVER_NAME, directory, trusted=authority)
        await asyncio.wait_for(watcher, timeout=5)
        assert stop.is_set()

    asyncio.run(scenario())


def test_an_unreadable_certificate_does_not_stop_the_service(
    tmp_path: Path, authority: Authority
) -> None:
    """Kubernetes swaps these files, and a read can land mid-way, which must not restart it."""

    async def scenario() -> None:
        directory = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
        stop = asyncio.Event()
        watcher = asyncio.create_task(
            stop_when_certificate_changes(directory, stop, interval_seconds=0.01)
        )
        await asyncio.sleep(0.05)
        saved = (directory / "tls.crt").read_bytes()
        (directory / "tls.crt").unlink()
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        (directory / "tls.crt").write_bytes(saved)
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        stop.set()
        await asyncio.wait_for(watcher, timeout=5)

    asyncio.run(scenario())


def test_the_fingerprint_changes_with_any_file(tmp_path: Path, authority: Authority) -> None:
    directory = issue(authority, SERVER_NAME, tmp_path / "server", trusted=authority)
    first = fingerprint(directory)
    assert fingerprint(directory) == first
    (directory / "ca.crt").write_bytes(make_authority("new-ca").pem())
    assert fingerprint(directory) != first


BASE_ENV = {"WINDOPS_IDENTITY_SECRET": "x" * 32}


def test_a_service_needs_its_certificate_directory_to_start() -> None:
    """GRP-005: no environment runs without mTLS by accident."""
    with pytest.raises(SettingsError, match="WINDOPS_TLS_DIR"):
        Settings.from_env(BASE_ENV)


def test_the_directory_is_read_from_the_environment() -> None:
    env = {**BASE_ENV, "WINDOPS_TLS_DIR": "/var/run/windops/tls"}
    assert Settings.from_env(env).tls_dir == Path("/var/run/windops/tls")


def test_plain_grpc_needs_the_test_environment_too() -> None:
    env = {**BASE_ENV, "WINDOPS_ENVIRONMENT": "test", "WINDOPS_PLAINTEXT_GRPC": "true"}
    assert Settings.from_env(env).tls_dir is None


@pytest.mark.parametrize("environment", ["local", "ci", "production-example", ""])
def test_plain_grpc_is_refused_everywhere_else(environment: str) -> None:
    env = {**BASE_ENV, "WINDOPS_ENVIRONMENT": environment, "WINDOPS_PLAINTEXT_GRPC": "true"}
    with pytest.raises(SettingsError, match="WINDOPS_TLS_DIR"):
        Settings.from_env(env)


def test_the_test_environment_alone_does_not_turn_it_off() -> None:
    with pytest.raises(SettingsError, match="WINDOPS_TLS_DIR"):
        Settings.from_env({**BASE_ENV, "WINDOPS_ENVIRONMENT": "test"})
