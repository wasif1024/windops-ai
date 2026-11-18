"""Qdrant, for passage vectors: one collection per embedding model, partitioned by tenant.

A point holds a passage's vector and identifiers, and the model and version that made it
(LLM-011), never its text: PostgreSQL keeps that, and is checked for permission before any passage
is returned (DAT-006, M7.3). A point's ID is derived from its tenant, document, revision and
passage, so indexing again replaces it rather than adding another.

TLS is verified against the cluster's authority, and the API key is read from its mounted file.
"""

import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from windops_resilience import BreakerOpenError, CircuitBreaker

# A fixed namespace, so a passage's point ID is the same on every run.
_POINTS = uuid.UUID("5f0e8a51-7c1b-4f7e-9d0a-3c2b6e4f9a11")
TIMEOUT_SECONDS = 30.0
# The payload fields searches filter on, beside the tenant, and their index types.
INDEXED_FIELDS = {"document_id": "keyword", "revision": "integer", "applies_to": "keyword",
                  "type": "keyword"}  # fmt: skip


class VectorsUnavailableError(Exception):
    """Qdrant cannot be reached, or refused the request."""


@dataclass(frozen=True)
class Point:
    tenant_id: str
    document_id: str
    revision: int
    ordinal: int
    vector: list[float]
    payload: dict[str, Any]

    @property
    def point_id(self) -> str:
        key = f"{self.tenant_id}/{self.document_id}/{self.revision}/{self.ordinal}"
        return str(uuid.uuid5(_POINTS, key))


@dataclass(frozen=True)
class Found:
    document_id: str
    revision: int
    ordinal: int
    score: float
    payload: dict[str, Any]


def search_filter(tenant_id: str, applies_to: str = "",
                  types: tuple[str, ...] = ()) -> dict[str, Any]:  # fmt: skip
    """The tenant comes first, always, from the caller's verified identity (DAT-006)."""
    must: list[dict[str, Any]] = [{"key": "tenant_id", "match": {"value": tenant_id}}]
    if applies_to:
        must.append({"key": "applies_to", "match": {"value": applies_to}})
    if types:
        must.append({"key": "type", "match": {"any": list(types)}})
    return {"must": must}


class VectorStore(Protocol):
    async def ensure_collection(self, name: str, dimensions: int) -> None: ...

    async def upsert(self, collection: str, points: list[Point]) -> None: ...

    async def search(
        self,
        collection: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        applies_to: str = "",
        types: tuple[str, ...] = (),
    ) -> list[Found]: ...

    async def close(self) -> None: ...


def collection_for(model: str, dimensions: int) -> str:
    return "knowledge_" + "".join(c if c.isalnum() else "_" for c in model) + f"_{dimensions}"


class Qdrant:
    def __init__(self, url: str, key_file: Path, authority: Path, breaker: CircuitBreaker) -> None:
        self._url, self._key_file, self._breaker = url.rstrip("/"), key_file, breaker
        self._http = httpx.AsyncClient(
            base_url=self._url, timeout=TIMEOUT_SECONDS,
            verify=ssl.create_default_context(cafile=str(authority)),
        )  # fmt: skip
        self._ready: set[str] = set()

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        key = self._key_file.read_text().strip()

        async def send() -> httpx.Response:
            try:
                response = await self._http.request(method, path, json=body,
                                                    headers={"api-key": key})  # fmt: skip
            except httpx.TimeoutException as error:
                raise TimeoutError("Qdrant did not answer in time") from error
            except httpx.TransportError as error:
                raise ConnectionError("Qdrant cannot be reached") from error
            if response.status_code >= 500:
                response.raise_for_status()
            return response

        try:
            response = await self._breaker.call(send)
        except (BreakerOpenError, TimeoutError, ConnectionError, httpx.HTTPError) as error:
            raise VectorsUnavailableError("The vector store cannot be reached right now") from error
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code >= 300:
            raise VectorsUnavailableError(f"The vector store refused the request: "
                                          f"{response.status_code}")  # fmt: skip
        return response.json().get("result")

    async def ensure_collection(self, name: str, dimensions: int) -> None:
        """Create the collection once, with its tenant partitioning, if it is not there."""
        if name in self._ready:
            return
        found = await self._call("GET", f"/collections/{name}")
        if found is None:
            await self._call("PUT", f"/collections/{name}", {
                "vectors": {"size": dimensions, "distance": "Cosine"},
                # Each tenant's points kept together, as Qdrant's multitenancy guidance says.
                "hnsw_config": {"payload_m": 16, "m": 0},
            })  # fmt: skip
            await self._call("PUT", f"/collections/{name}/index", {
                "field_name": "tenant_id",
                "field_schema": {"type": "keyword", "is_tenant": True},
            })  # fmt: skip
        else:
            size = found["config"]["params"]["vectors"]["size"]
            if size != dimensions:
                raise VectorsUnavailableError(
                    f"The collection {name} holds vectors of {size}, not {dimensions}"
                )
        # What searches filter on. Each is added if missing, so an older collection gets it too.
        have = set((found or {}).get("payload_schema", {}))
        for field, schema in INDEXED_FIELDS.items():
            if field not in have:
                await self._call("PUT", f"/collections/{name}/index?wait=true",
                                 {"field_name": field, "field_schema": schema})  # fmt: skip
        self._ready.add(name)

    async def upsert(self, collection: str, points: list[Point]) -> None:
        await self._call("PUT", f"/collections/{collection}/points?wait=true", {"points": [
            {"id": p.point_id, "vector": p.vector,
             "payload": {"tenant_id": p.tenant_id, "document_id": p.document_id,
                         "revision": p.revision, "ordinal": p.ordinal, **p.payload}}
            for p in points
        ]})  # fmt: skip

    async def search(
        self,
        collection: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        applies_to: str = "",
        types: tuple[str, ...] = (),
    ) -> list[Found]:
        """The closest points in the tenant, best first. A collection not made yet has none."""
        if not tenant_id:
            raise ValueError("A search is always within one tenant")
        if collection not in self._ready:
            found = await self._call("GET", f"/collections/{collection}")
            if found is None:
                return []
            await self.ensure_collection(collection, found["config"]["params"]["vectors"]["size"])
        result = await self._call("POST", f"/collections/{collection}/points/query", {
            "query": vector, "limit": limit, "with_payload": True,
            "filter": search_filter(tenant_id, applies_to, types),
        })  # fmt: skip
        return [
            Found(p["payload"]["document_id"], int(p["payload"]["revision"]),
                  int(p["payload"]["ordinal"]), float(p["score"]), p["payload"])
            for p in result["points"]
            # Qdrant was told the tenant; this makes sure it kept to it.
            if p["payload"].get("tenant_id") == tenant_id
        ]  # fmt: skip

    async def count(self, collection: str, tenant_id: str, document_id: str, revision: int) -> int:
        result = await self._call("POST", f"/collections/{collection}/points/count", {
            "exact": True,
            "filter": {"must": [
                {"key": "tenant_id", "match": {"value": tenant_id}},
                {"key": "document_id", "match": {"value": document_id}},
                {"key": "revision", "match": {"value": revision}},
            ]},
        })  # fmt: skip
        return int(result["count"])

    async def close(self) -> None:
        await self._http.aclose()
