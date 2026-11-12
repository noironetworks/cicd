# Release-run monitoring and explicit notifications

`travis/ci_monitor.py` is an inactive command-line building block. It creates no
schedule, workflow, tag, release request, or deployment. Observation and sending
are separate commands; this change performs no live notification.

## Owner inputs

An owner must provide a JSON file with `schema_version: 1` and a nonempty
`requests` array. Every entry requires these explicit fields:

| Field | Required value |
| --- | --- |
| `repository` | Exact GitHub owner/repository |
| `tag` | Numeric release tag `N.N.N.N` |
| `source_sha` | Reviewed full source commit SHA |
| `requested_at` | Time the owner requested the release run; ISO time with timezone |
| `deadline` | Owner deadline for observing that requested run; ISO time with timezone |
| `workflow` | Exact workflow filename, such as `acc-provision-tag-publish.yml` |

Record request time and deadline in the requesting system. Do not infer either
from a tag annotation, signer clock, tagger date, repository commit time, or a
historical run. A monitor cannot detect an unrecorded release request. Missing,
empty, invalid, or unavailable configuration produces `unknown`.

## Read-only observation

```sh
python3 travis/ci_monitor.py check \
  --requests /path/to/owner-watched-requests.json \
  --output /path/to/monitor-report.json
```

The command invokes only `gh api --method GET`, first for the configured
workflow, then for its push runs with the configured tag as the branch selector.
It requires the exact workflow path, canonical repository, tag label, source
SHA, and a run created after the explicit request. The latest run/attempt wins;
a newer wrong-source run or pending rerun cannot be hidden by an older success.

| State | Meaning |
| --- | --- |
| `waiting` | No matching workflow/tag push run, before the explicit deadline |
| `missed` | No matching run, at or after that deadline |
| `disabled` | GitHub explicitly reports the watched workflow disabled |
| `wrong-source` | Latest watched tag run has a different source SHA |
| `pending` | Matching run exists but has not completed |
| `failed` | Matching run failed, was cancelled/timed out, or otherwise did not succeed |
| `healthy` | Matching workflow reports successful completion |
| `unknown` | Configuration or bounded API evidence cannot establish a result |

The deadline checks whether a run appeared. It is not an invented build timeout.
`started_late` separately records an observed run created after that deadline.
A workflow success does not prove image/package publication or production
qualification; retained receipts, immutable inputs, and the signing policy
remain separate gates.

Observation allows at most 20 requests, two API calls per request, 100 returned
runs per query, a 10-second child timeout, and a 60-second overall observation
budget. Truncated history and exhausted/unavailable evidence remain `unknown`.
`--now` accepts an explicit timestamp for offline replay. The JSON report carries
the operational result; a successful command exit means the report was produced.

Only GitHub read credentials reach `gh`; status, registry, package, and webhook
credentials are excluded from its environment. Configure an Actions read token
with the narrow repository access actually required. No remote observation is
performed by `notify`.

## Explicit notification

Only after an owner approves an endpoint and its recipient routing, provide its
HTTPS URL through the notifier's `CI_MONITOR_WEBHOOK` environment variable (or
name a dedicated variable with `--webhook-env`):

```sh
python3 travis/ci_monitor.py notify \
  --report /path/to/monitor-report.json \
  --receipt /path/to/persistent/notification-receipts.json
```

No webhook setting yields `not_configured` and no acceptance receipt. Reports
without concrete `missed`, `disabled`, `wrong-source`, or `failed` states yield
`no_alerts`. Unknown observation/configuration is not silently called healthy;
the deployment owner must separately route monitor errors and unknown states.

Notification sends one JSON payload containing the actionable request identities
and a stable `idempotency_key`, also supplied in the `Idempotency-Key` header.
Polling timestamps do not change that key. The notifier runs no `gh`, build,
scanner, or package uploader. It rejects HTTP endpoints, URL credentials,
fragments, and redirects, including an HTTPS-to-HTTP redirect.

Only an HTTP 2xx acknowledgement records `accepted` in the atomic local receipt.
This means the receiver accepted the request, not that a person received or read
it. An unset endpoint, connection failure, non-2xx response, or redirect creates
no acceptance receipt. The configured receiver must honor the stable idempotency
key to handle a lost acknowledgement or a crash between acceptance and local
receipt persistence. Local persistence alone cannot guarantee remote exactly-once
message delivery.

Receipts retain up to 1,000 accepted keys per endpoint fingerprint and use an
exclusive in-flight lock. A full history or existing lock fails closed. The
owner must investigate stale locks and rotate/retain receipt history deliberately;
the command does not remove another process's lock or silently forget old keys.
The endpoint URL and its credential material are never stored in the receipt.

## Activation gate and verification

Deployment wiring is external and remains inactive. Before enabling it, an owner
must approve the watched request source/deadlines, read-only GitHub access,
notification destination and receiver idempotency behavior, persistent receipt
storage, scheduling, and handling of unknown states or notifier errors. Existing
release approval and signing identities remain unchanged.

Offline qualification uses stub `gh` responses, temporary files, and a genuine
loopback HTTPS receiver with an ephemeral test certificate. It covers missed and
disabled runs, terminal failures, wrong source, reruns, unavailable evidence,
scoped credentials, absent notification configuration, idempotent acceptance,
redirect/error rejection, and receipt locking. It sends no real messages:

```sh
python3 -m unittest discover -s tests -p test_ci_monitor.py -v
```
