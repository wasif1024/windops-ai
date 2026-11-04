# Phase 1 exit gate

**Result:** passed on 2026-11-04, on the local Minikube cluster.

SRS section 12 sets the gate: *a sample gRPC call is traced through the gateway, and its breaker opens
under an injected fault.* Run it yourself with:

```bash
make cluster
make phase-1-gate
```

## How it was run

`make clean` first removed all WindOps workloads and namespaces. The check then rebuilt images, created
secrets, deployed the stack and services, and applied the routes, using the documented commands.

## Evidence

Output from the run:

```text
The gateway reaches the sample services
Tempo has spans from the gateway and both services
Trace 233bd1e88f390e69596bc8830a6c6d63: gateway, then caller, then client, then responder, status OK
Loki has the responder's log line, with the same trace ID
Tempo restarted while holding the trace, and still has it
Injected the fault: the responder is scaled to zero
The breaker opened after 6 failed requests
While open, requests fail fast: the slowest of 5 took 31 ms
Prometheus shows the breaker as open
The WindopsCircuitBreakerOpen alert is active
Loki has the breaker's state change
The caller stayed up during the fault, with no restart
The breaker let probes through, and calls succeed again
Recovered 62s after the responder returned
Prometheus shows the breaker as closed
The Phase 1 exit gate passes.
```

| Part of the gate | What the check requires |
| --- | --- |
| Traced through the gateway | Each span's parent is the previous hop, from gateway to caller to client to responder, and the gRPC status is OK |
| Logs join the trace | The responder's log line in Loki has the same trace ID |
| Breaker opens under a fault | With the responder scaled to zero, requests return 503 until the reason becomes `breaker_open` |
| It fails fast | Each of 5 requests through the gateway takes under a second while the breaker is open |
| It is visible | Prometheus shows the breaker open, its alert is active, and Loki has the state change |
| The caller stays up | It does not restart during the fault (RES-006) |
| It recovers | After the 30-second open period, calls succeed, and 3 successful probes close the breaker |

Recovery took 31 seconds in an earlier run, and 62 here. A probe that fails while the responder is still starting
sends the breaker through another 30-second wait, which is the likely cause of the longer time.

## Problems the gate found

| Problem | Fix |
| --- | --- |
| The gateway sent no spans, because this APISIX version reads collector settings from plugin metadata | Store those settings in `infrastructure/gateway/plugin-metadata`, applied with the routes |
| APISIX logged errors for unused stream plugins | Enable only the plugins routes use |
| Tempo ran out of memory at 320 MiB when writing stored traces | Raise its limit to 512 MiB, and test a restart while it holds real traces |
| The gateway restarted on a fresh start, because etcd was not ready yet | The gateway now waits for etcd before starting |

## Limits

- The gate ran on a cluster with existing images. It removed workloads and namespaces, but did not delete
  the Minikube cluster itself. M14.5 proves a bootstrap from a clean machine.
- Requests entered through `kubectl port-forward`, over plain HTTP. M3.7 adds TLS.
- CI has no cluster, so this check runs locally, and is not part of every pull request.
- Calls use one shared signing key, without mTLS, until M3.3.
- These are sample services, and memory (2.3 GiB of 10 GiB here) does not yet predict the full system.
