"""The table this service owns: each forecast it retrieved, by tenant, never changed once written.

A retrieval is kept as the provider gave it, normalized, with its provenance (WTH-003). It is
both the cache and the record a policy result can point to, so a forecast it judged on can be
read again exactly (M6.4).
"""

from sqlalchemy import Column, DateTime, Float, Index, MetaData, Table, Text

SERVICE_NAME = "weather"
METADATA = MetaData()

FORECASTS = Table(
    "forecasts",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("forecast_id", Text, primary_key=True),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("revision", Text, nullable=False),
    # The rounded coordinates sent to the provider: the cache's key.
    Column("latitude", Float, nullable=False),
    Column("longitude", Float, nullable=False),
    Column("elevation_m", Float, nullable=False),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    Column("valid_to", DateTime(timezone=True), nullable=False),
    # The normalized hours as canonical JSON, and the sha256 of the provider's own response.
    Column("hours", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Index("forecasts_newest", "tenant_id", "latitude", "longitude", "retrieved_at"),
)
TENANT_TABLES = ("forecasts",)
