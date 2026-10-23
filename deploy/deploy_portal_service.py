#!/usr/bin/env python3
"""Deploys the supervisor portal Cloud Run service `<p>-portal` behind Cloud Run direct IAP.

Run by `terraform_data.portal_service` (main.tf) via local-exec inside the Cloud Build terraform
step, whose image has Python + google-auth but NO gcloud CLI. Uses only the stdlib (urllib) and
google-auth, like deploy/deploy_reasoning_engine.py. Idempotent; every step fails loudly:

1. Service Usage `generateServiceIdentity` for iap.googleapis.com (creates the IAP service agent
   `service-<num>@gcp-sa-iap.iam.gserviceaccount.com` if missing) and waits for its LRO.
2. Cloud Run Admin API v2: GET the service; POST `services?serviceId=` when absent, otherwise PATCH
   with an explicit `updateMask` limited to the fields managed here; waits for the LRO; re-reads the
   service and refuses to succeed unless `iapEnabled` is true.
3. `roles/run.invoker` on the service for the IAP service agent (getIamPolicy -> merge ->
   setIamPolicy, etag-guarded, retried on concurrent modification and on IAM not yet knowing a
   freshly created service agent).

Field names verified against https://run.googleapis.com/$discovery/rest?version=v2
(GoogleCloudRunV2Service.iapEnabled / invokerIamDisabled / ingress / template; RevisionTemplate
serviceAccount / timeout / scaling.{min,max}InstanceCount / containers[].resources.limits).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

RUN_API = "https://run.googleapis.com/v2"
SERVICE_USAGE_API = "https://serviceusage.googleapis.com/v1beta1"
INVOKER_ROLE = "roles/run.invoker"

# Top-level Service fields this script owns. `template` is replaced as a whole (every revision
# setting of the portal is declared here); labels, annotations, traffic, etc. are left alone.
MANAGED_UPDATE_MASK = "iapEnabled,invokerIamDisabled,ingress,template"

# (method, url, body) -> (http_status, parsed_json_body)
HttpCall = Callable[[str, str, Optional[Dict[str, Any]]], Tuple[int, Dict[str, Any]]]


class DeployError(RuntimeError):
    """Any failure that must stop `terraform apply`."""


# ---------------------------------------------------------------------------------- transport


def _get_access_token() -> str:
    """ADC first (the deployer SA in Cloud Build); gcloud only as a workstation fallback."""
    adc_error: object = "ADC returned no token"
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        if creds.token:
            return creds.token
    except Exception as exc:  # noqa: BLE001 - reported together with the fallback's error
        adc_error = exc
    try:
        return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DeployError(
            f"No credentials: Application Default Credentials failed ({adc_error}); gcloud fallback failed ({exc})."
        ) from exc


def make_urllib_http(project_id: str) -> HttpCall:
    def _call(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {_get_access_token()}")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Goog-User-Project", project_id)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read() or b"{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text) if text else {}
            except ValueError:
                parsed = {"_raw": text}
            return exc.code, parsed

    return _call


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _fail(what: str, status: int, body: Dict[str, Any]) -> DeployError:
    return DeployError(f"{what} failed: HTTP {status}: {json.dumps(body, ensure_ascii=False)[:2000]}")


# ------------------------------------------------------------------------------ 1. IAP agent


def ensure_iap_service_identity(
    http: HttpCall,
    project_number: str,
    *,
    timeout_sec: float = 300,
    poll_sec: float = 5,
    sleep: Optional[Callable[[float], None]] = None,
) -> str:
    """Creates (idempotently) the IAP service agent and returns its member string."""
    url = f"{SERVICE_USAGE_API}/projects/{project_number}/services/iap.googleapis.com:generateServiceIdentity"
    status, op = http("POST", url, {})
    if not _ok(status):
        raise _fail("generateServiceIdentity(iap.googleapis.com)", status, op)
    deadline = time.monotonic() + timeout_sec
    while not op.get("done"):
        if time.monotonic() > deadline:
            raise DeployError(f"Timed out waiting for generateServiceIdentity operation {op.get('name')}")
        (sleep or time.sleep)(poll_sec)
        status, op = http("GET", f"{SERVICE_USAGE_API}/{op['name']}", None)
        if not _ok(status):
            raise _fail("generateServiceIdentity operation poll", status, op)
    if op.get("error"):
        raise DeployError(f"generateServiceIdentity(iap.googleapis.com) failed: {json.dumps(op['error'])}")
    email = str(((op.get("response") or {}).get("email")) or "")
    if not email:
        email = f"service-{project_number}@gcp-sa-iap.iam.gserviceaccount.com"
    print(f"✅ IAP service agent: {email}")
    return f"serviceAccount:{email}"


# -------------------------------------------------------------------------- 2. Cloud Run service


def build_service_body(
    *,
    image: str,
    service_account: str,
    env: Dict[str, str],
    min_instances: int = 0,
    max_instances: int = 5,
    cpu: str = "1",
    memory: str = "1Gi",
    timeout_sec: int = 300,
) -> Dict[str, Any]:
    """Cloud Run v2 Service body for the portal (only fields this script manages)."""
    if not image or "@sha256:" not in image:
        raise DeployError(f"--image must be pinned by digest (got {image!r})")
    if not service_account:
        raise DeployError("--service-account is required")
    return {
        "iapEnabled": True,
        "invokerIamDisabled": False,
        "ingress": "INGRESS_TRAFFIC_ALL",
        "template": {
            "serviceAccount": service_account,
            "timeout": f"{int(timeout_sec)}s",
            "scaling": {"minInstanceCount": int(min_instances), "maxInstanceCount": int(max_instances)},
            "containers": [
                {
                    "image": image,
                    "env": [{"name": k, "value": str(v)} for k, v in sorted(env.items())],
                    "resources": {"limits": {"cpu": cpu, "memory": memory}},
                }
            ],
        },
    }


def _service_name(project_id: str, region: str, service: str) -> str:
    return f"projects/{project_id}/locations/{region}/services/{service}"


def wait_for_run_operation(
    http: HttpCall,
    op: Dict[str, Any],
    *,
    timeout_sec: float = 900,
    poll_sec: float = 5,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while not op.get("done"):
        if time.monotonic() > deadline:
            raise DeployError(f"Timed out waiting for Cloud Run operation {op.get('name')}")
        (sleep or time.sleep)(poll_sec)
        status, op = http("GET", f"{RUN_API}/{op['name']}", None)
        if not _ok(status):
            raise _fail("Cloud Run operation poll", status, op)
    if op.get("error"):
        raise DeployError(f"Cloud Run operation {op.get('name')} failed: {json.dumps(op['error'], ensure_ascii=False)}")
    return op


def upsert_service(
    http: HttpCall,
    *,
    project_id: str,
    region: str,
    service: str,
    body: Dict[str, Any],
    timeout_sec: float = 900,
    poll_sec: float = 5,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    """Creates or patches the service, waits for it, and verifies IAP is on. Returns the service."""
    name = _service_name(project_id, region, service)
    status, current = http("GET", f"{RUN_API}/{name}", None)
    if status == 404:
        url = f"{RUN_API}/projects/{project_id}/locations/{region}/services?" + urllib.parse.urlencode(
            {"serviceId": service}
        )
        status, op = http("POST", url, body)
        action = "create"
    elif _ok(status):
        url = f"{RUN_API}/{name}?" + urllib.parse.urlencode({"updateMask": MANAGED_UPDATE_MASK})
        status, op = http("PATCH", url, body)
        action = "patch"
    else:
        raise _fail(f"GET {name}", status, current)
    if not _ok(status):
        raise _fail(f"Cloud Run service {action} ({name})", status, op)
    print(f"⏳ Cloud Run service {action} submitted: {op.get('name', name)}")
    wait_for_run_operation(http, op, timeout_sec=timeout_sec, poll_sec=poll_sec, sleep=sleep)
    status, svc = http("GET", f"{RUN_API}/{name}", None)
    if not _ok(status):
        raise _fail(f"GET {name} after {action}", status, svc)
    if svc.get("iapEnabled") is not True:
        raise DeployError(f"{name}: iapEnabled is not true after {action}; refusing to leave the portal without IAP")
    if svc.get("invokerIamDisabled") is True:
        raise DeployError(f"{name}: invokerIamDisabled is true after {action}; the portal must keep invoker IAM on")
    cond = svc.get("terminalCondition") or {}
    if cond.get("state") == "CONDITION_FAILED":
        raise DeployError(f"{name} is not serving: {json.dumps(cond, ensure_ascii=False)}")
    print(f"✅ Cloud Run service {action} done, IAP enabled: {svc.get('uri', name)}")
    return svc


# ------------------------------------------------------------------------- 3. invoker binding


def merge_member(policy: Dict[str, Any], role: str, member: str) -> bool:
    """Adds `member` to `role` in place; returns False when it was already there."""
    bindings: List[Dict[str, Any]] = policy.setdefault("bindings", [])
    for binding in bindings:
        if binding.get("role") == role and not binding.get("condition"):
            members = binding.setdefault("members", [])
            if member in members:
                return False
            members.append(member)
            return True
    bindings.append({"role": role, "members": [member]})
    return True


def _retryable_iam_error(status: int, body: Dict[str, Any]) -> bool:
    if status in (409, 412):
        return True
    text = json.dumps(body)
    # A just-created service agent can take a while to become known to IAM.
    return status == 400 and ("does not exist" in text or "not found" in text.lower())


def ensure_invoker_binding(
    http: HttpCall,
    *,
    project_id: str,
    region: str,
    service: str,
    member: str,
    attempts: int = 8,
    backoff_sec: float = 5,
    sleep: Optional[Callable[[float], None]] = None,
) -> bool:
    """Ensures `member` holds roles/run.invoker on the service. Returns True if a change was made."""
    name = _service_name(project_id, region, service)
    last: Tuple[int, Dict[str, Any]] = (0, {})
    for attempt in range(1, attempts + 1):
        status, policy = http("GET", f"{RUN_API}/{name}:getIamPolicy?options.requestedPolicyVersion=3", None)
        if not _ok(status):
            raise _fail(f"getIamPolicy {name}", status, policy)
        if not merge_member(policy, INVOKER_ROLE, member):
            print(f"✅ {member} already has {INVOKER_ROLE} on {service}")
            return False
        policy.setdefault("version", 3 if any(b.get("condition") for b in policy["bindings"]) else 1)
        status, body = http("POST", f"{RUN_API}/{name}:setIamPolicy", {"policy": policy})
        if _ok(status):
            print(f"✅ Granted {INVOKER_ROLE} on {service} to {member}")
            return True
        last = (status, body)
        if not _retryable_iam_error(status, body):
            break
        print(f"⏳ setIamPolicy retry {attempt}/{attempts} (HTTP {status})")
        (sleep or time.sleep)(backoff_sec * attempt)
    raise _fail(f"setIamPolicy {name}", *last)


# ----------------------------------------------------------------------------------------- CLI


def _parse_env(args: argparse.Namespace) -> Dict[str, str]:
    raw = args.env_json
    if args.env_file:
        with open(args.env_file, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        env = json.loads(raw or "{}")
    except ValueError as exc:
        raise DeployError(f"--env-json / --env-file is not valid JSON: {exc}") from exc
    if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
        raise DeployError("--env-json must be a JSON object of NAME -> value")
    return {k: "" if v is None else str(v) for k, v in env.items()}


def main(argv: Optional[List[str]] = None, http: Optional[HttpCall] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project-id", required=True)
    p.add_argument("--project-number", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--service-account", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--env-json", help="JSON object of container env vars")
    g.add_argument("--env-file", help="path of a JSON file with the container env vars")
    p.add_argument("--min-instances", type=int, default=0)
    p.add_argument("--max-instances", type=int, default=5)
    p.add_argument("--cpu", default="1")
    p.add_argument("--memory", default="1Gi")
    p.add_argument("--timeout-sec", type=int, default=300)
    args = p.parse_args(argv)
    call = http or make_urllib_http(args.project_id)
    try:
        member = ensure_iap_service_identity(call, args.project_number)
        body = build_service_body(
            image=args.image,
            service_account=args.service_account,
            env=_parse_env(args),
            min_instances=args.min_instances,
            max_instances=args.max_instances,
            cpu=args.cpu,
            memory=args.memory,
            timeout_sec=args.timeout_sec,
        )
        upsert_service(call, project_id=args.project_id, region=args.region, service=args.service, body=body)
        ensure_invoker_binding(
            call, project_id=args.project_id, region=args.region, service=args.service, member=member
        )
    except DeployError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
