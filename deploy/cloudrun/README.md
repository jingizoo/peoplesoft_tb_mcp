# pstb on Cloud Run

Two services from one image, on the architecture the DEV project already
tested end to end:

```
users / MCP clients
  → HTTPS (custom domain) → External ALB + Cloud Armor
  → serverless NEG → Cloud Run            [ingress: internal + LB]
      pstb-gui   (the browser workspace)
      pstb-mcp   (the 90-tool MCP server, streamable HTTP)
  → Direct VPC egress → shared-VPC app subnet (10.60.10.0/24)
  → Oracle listener 1521 on the DEV VM (thin driver — no instant client)
```

## Prerequisites (create once)

```bash
gcloud artifacts repositories create pstb --repository-format=docker \
  --location="$REGION" --project="$PROJECT"
for s in pstb-auth-token pstb-operator-token pstb-mcp-token; do
  python3 -c "import secrets;print(secrets.token_urlsafe(24))" | \
    gcloud secrets create "$s" --data-file=- --project="$PROJECT"
done
printf '%s' "$ORACLE_PASSWORD" | \
  gcloud secrets create pstb-oracle-password --data-file=- --project="$PROJECT"
```

Grant the service account `roles/secretmanager.secretAccessor` on each.

## Why direct Oracle is correct here

The other candidate design routes through Integration Broker REST. For
this application that would discard its entire security model: the 90
curated tools, the guards, and the read-only account ARE the boundary,
and the SQL they issue is the product. Direct VPC egress carries TCP/1521
like any private traffic; firewall the DB VM to the service's subnet.

## Trust model on Cloud Run — read before deploying

* `PSTB_TRUSTED_PROXY=1` — the balancer injects forwarded headers, so
  the peer address stops being an identity. The page token
  (`PSTB_AUTH_TOKEN`) is the access control; it is REQUIRED in this mode
  and the process refuses to start without it.
* `PSTB_OPERATOR_TOKEN` — there is no loopback behind a balancer, so
  "machine-local" operator surfaces (approvals, question report,
  coverage gaps) unlock with this second key instead, entered under
  Diagnostics. Keep it distinct from the page token: reading dashboards
  and approving durable knowledge are different privileges.
* `PSTB_MCP_TOKEN` — the MCP service refuses to start without it.
  Ingress rules are a network posture, not authentication.

## Identity-aware front end (recommended once DNS + certs exist)

With IAP enabled on the backend service, colleagues get corporate
sign-in instead of a token paste. Two extra env vars turn on the app's
own verification of the front end's signed assertion:

* `PSTB_TRUSTED_IAP=1` — verify `x-goog-iap-jwt-assertion` on every
  request and accept it as the access control. The token remains a
  valid alternative (machine callers, break-glass) and the sign-in form
  stays as the fallback door.
* `PSTB_IAP_AUDIENCE` — the exact JWT audience, never derived:
  `/projects/<PROJECT_NUMBER>/regions/<REGION>/backendServices/<ID>`,
  where the ID comes from
  `gcloud compute backend-services describe <name> --region <region>
  --format='value(id)'`.

Why in-app verification and not just the network: `--ingress
internal-and-cloud-load-balancing` admits VPC-internal traffic that
never crossed the front end. Such a request carries no signed
assertion, so it falls back to the token requirement — the signature is
the proof, not the route. Verification keys are fetched from a public
Google URL and cached for hours; with `--vpc-egress private-ranges-only`
(the default here) that fetch goes out directly, while `all-traffic`
egress needs a NAT path to the internet.

## State — the part that bites

Everything governed lives in files: approval queues
(`source_knowledge/*.db`), taught facts (`site_memory.json`), the
question log that feeds the failure flywheel, the metadata catalog.
Mount Filestore (NFS) at `/data`, put `config.yaml` there, and
`PSTB_CONFIG=/data/config.yaml` roots every relative path onto the
mount. Two consequences:

* Without the mount, every approval dies with the instance.
* The approval store fsyncs its DIRECTORY; some NFS servers refuse that
  with EINVAL. The error message names it explicitly ("some network
  filesystems do for fsync on a directory") — if you see it, the
  Filestore export needs `sync` semantics or the state needs local-SSD
  relocation. Test one approval end to end before calling it deployed.

## Catalog builds

`build_metadata_catalog.py` (and the join miner inside it) run as a
Cloud Run JOB on the same network/subnet, writing to the same `/data`
mount. The service only reads the artifact.

## Vertex

The service account IS the credential (ADC) — no key file. Set
`GOOGLE_CLOUD_PROJECT`; grant `roles/aiplatform.user`.

## Known constraints

* max-instances=1 by design (in-process session state); session
  affinity on as a second belt.
* `--no-cpu-throttling`: batch exports run on background threads.
* The settings console is DISABLED behind the balancer — there is no
  machine-local path, and its refusal says so. Configuration belongs to
  env vars and Secret Manager on this deployment.
* The pasted `?token=` URL flow is refused in proxy mode: the ALB and
  the platform log request URLs, and the token is the access control.
  Colleagues receive the token from the secret store and send it once as
  an `Authorization: Bearer` header; the cookie takes over from there.
* The startup banner does not print the token here: stdout is persisted
  by Cloud Logging.
