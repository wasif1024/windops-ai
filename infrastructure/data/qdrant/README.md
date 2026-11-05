# Qdrant

`windops-qdrant` holds document vectors for the Knowledge Service, and is reachable only inside the
cluster. Its vectors and snapshots live on a persistent volume, so they survive a restart.

It can be rebuilt from PostgreSQL by re-indexing (FR-KNW-005), so it is not a system of record. It does
not decide who may read a document (SRS 8.1).

## Service access

Only the Knowledge Service needs it. It reads the API key from the Secret `windops-qdrant-api-key` in
`windops-application`, and connects to `windops-qdrant.windops-data.svc.cluster.local`, on port 6333 for
HTTP or 6334 for gRPC.

Every query must carry a tenant filter, followed by a document-permission check (DAT-006). M7.3 builds
that into the Knowledge Service. Qdrant cannot enforce it, because this key can read every tenant.

## Back up and restore

Reach the API from your machine, in a second terminal:

```bash
kubectl --context windops --namespace windops-data port-forward service/windops-qdrant 6333:6333
```

Load the key, then back up a collection to a file:

```bash
export QDRANT_API_KEY="$(sed -n 's/^api-key=//p' infrastructure/minikube/secrets.local.qdrant.env)"
curl -sS -X POST -H "api-key: $QDRANT_API_KEY" http://127.0.0.1:6333/collections/documents/snapshots
curl -sS -H "api-key: $QDRANT_API_KEY" -o documents.snapshot \
  http://127.0.0.1:6333/collections/documents/snapshots/<name-from-the-first-command>
```

Restore it, replacing that collection's contents:

```bash
curl -sS -X POST -H "api-key: $QDRANT_API_KEY" -F snapshot=@documents.snapshot \
  "http://127.0.0.1:6333/collections/documents/snapshots/upload?priority=snapshot"
```

`make qdrant-check` runs these calls against a test collection. Scheduled backups arrive in M14.3
(DAT-003).

## Reset

This **deletes all vectors**, which then need re-indexing.

```bash
kubectl --context windops --namespace windops-data delete statefulset windops-qdrant
kubectl --context windops --namespace windops-data delete pvc data-windops-qdrant-0
make infra
```

## When Qdrant is down

The Knowledge Service pauses evidence retrieval, and no uncited procedure claims are produced (SRS 7.7).

## Limits

- Connections use TLS, and clients check the server's certificate, but a client proves who it is with a
  password and not a certificate. See [Service certificates](../../security/pki/README.md).
- One key reads and writes every collection, so tenant separation relies on the Knowledge Service.
