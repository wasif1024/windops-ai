"""Keys must carry their service and tenant, and never hold credentials."""

import pytest
from windops_cache import MAX_TTL_SECONDS, build_key
from windops_cache.keys import validate_ttl


def test_key_carries_service_tenant_and_name() -> None:
    assert build_key("fault-detection", "tenant-1", "rule:7") == "fault-detection:tenant-1:rule:7"


@pytest.mark.parametrize("tenant", ["", "a:b", "a b", "a*", "a\nb"])
def test_tenant_that_could_reach_another_tenants_keys_is_refused(tenant: str) -> None:
    with pytest.raises(ValueError, match="tenant"):
        build_key("audit", tenant, "name")


@pytest.mark.parametrize("service", ["", "Audit", "a:b", "audit*", "-audit"])
def test_unsafe_service_name_is_refused(service: str) -> None:
    with pytest.raises(ValueError, match="service"):
        build_key(service, "tenant-1", "name")


@pytest.mark.parametrize("name", ["", "a b", "a*", "a\nb"])
def test_unsafe_key_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="key name"):
        build_key("audit", "tenant-1", name)


@pytest.mark.parametrize(
    "name", ["user-token", "PASSWORD", "client_secret", "aws_credentials", "api_key", "session:1"]
)
def test_credentials_are_refused(name: str) -> None:
    """DAT-004."""
    with pytest.raises(ValueError, match="DAT-004"):
        build_key("audit", "tenant-1", name)


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1])
def test_ttl_out_of_range_is_refused(ttl: int) -> None:
    with pytest.raises(ValueError, match="TTL"):
        validate_ttl(ttl)
