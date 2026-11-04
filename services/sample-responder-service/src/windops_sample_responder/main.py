"""Start the sample responder, which answers gRPC calls from the sample caller."""

import asyncio
from typing import Any

import grpc
from windops.sample.v1 import sample_pb2, sample_pb2_grpc
from windops_grpc import InvalidRequestError, current_identity, run_service

SERVICE_NAME = "sample-responder"


class SampleResponder(sample_pb2_grpc.SampleResponderServiceServicer):
    async def Echo(  # noqa: N802  # The name comes from the contract.
        self, request: sample_pb2.EchoRequest, context: Any
    ) -> sample_pb2.EchoResponse:
        if not request.message:
            raise InvalidRequestError("A message is required")
        return sample_pb2.EchoResponse(
            message=request.message, tenant_id=current_identity().tenant_id
        )


def register(server: grpc.aio.Server) -> None:
    """Add this service's gRPC servicers to the server."""
    sample_pb2_grpc.add_SampleResponderServiceServicer_to_server(SampleResponder(), server)


def main() -> None:
    asyncio.run(run_service(SERVICE_NAME, register))


if __name__ == "__main__":
    main()
