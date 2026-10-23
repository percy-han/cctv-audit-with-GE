"""Round 86A-2: deploy/deploy_portal_service.py (Cloud Run Admin API v2, no gcloud), hermetic."""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

IAC_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("deploy_portal_service", IAC_DIR / "deploy" / "deploy_portal_service.py")
dps = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec and _spec.loader
_spec.loader.exec_module(dps)  # type: ignore[union-attr]

P, NUM, R, SVC = "proj-a", "123456", "asia-southeast1", "p-portal"
NAME = f"projects/{P}/locations/{R}/services/{SVC}"
IMAGE = "asia-southeast1-docker.pkg.dev/proj-a/p-images/w@sha256:" + "a" * 64
SA = "p-worker@proj-a.iam.gserviceaccount.com"
IAP_MEMBER = f"serviceAccount:service-{NUM}@gcp-sa-iap.iam.gserviceaccount.com"


class FakeHttp:
    """Scripted responses keyed by (method, url-without-query prefix); records every call."""

    def __init__(self, routes: Dict[Tuple[str, str], List[Tuple[int, Dict[str, Any]]]]) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    def __call__(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        self.calls.append((method, url, json.loads(json.dumps(body)) if body is not None else None))
        path = url.split("?", 1)[0]
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {url}")
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, json.loads(json.dumps(payload))  # callers may mutate it

    def by(self, method: str, path_suffix: str) -> List[Tuple[str, str, Optional[Dict[str, Any]]]]:
        return [c for c in self.calls if c[0] == method and c[1].split("?", 1)[0].endswith(path_suffix)]


RUN = dps.RUN_API
SU = dps.SERVICE_USAGE_API
GEN_URL = f"{SU}/projects/{NUM}/services/iap.googleapis.com:generateServiceIdentity"
SVC_URL = f"{RUN}/{NAME}"
CREATE_URL = f"{RUN}/projects/{P}/locations/{R}/services"
OP = "projects/proj-a/locations/asia-southeast1/operations/op-1"
GOOD_SVC = {"name": NAME, "iapEnabled": True, "invokerIamDisabled": False, "uri": "https://p-portal-x.a.run.app",
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"}}


def _base_routes(*, exists: bool, policy: Dict[str, Any], set_responses=None):
    get_svc = ([(200, {"name": NAME, "iapEnabled": False})] if exists else [(404, {"error": {"code": 404}})]) + [
        (200, GOOD_SVC)
    ]
    return {
        ("POST", GEN_URL): [(200, {"name": "operations/su-1", "done": False})],
        ("GET", f"{SU}/operations/su-1"): [
            (200, {"name": "operations/su-1", "done": True,
                   "response": {"email": f"service-{NUM}@gcp-sa-iap.iam.gserviceaccount.com"}})
        ],
        ("GET", SVC_URL): get_svc,
        ("POST", CREATE_URL): [(200, {"name": OP, "done": False})],
        ("PATCH", SVC_URL): [(200, {"name": OP, "done": False})],
        ("GET", f"{RUN}/{OP}"): [(200, {"name": OP, "done": True, "response": {}})],
        ("GET", f"{SVC_URL}:getIamPolicy"): [(200, policy)],
        ("POST", f"{SVC_URL}:setIamPolicy"): set_responses or [(200, {})],
    }


def _argv(env: Dict[str, str]) -> List[str]:
    return [
        "--project-id", P, "--project-number", NUM, "--region", R, "--service", SVC,
        "--image", IMAGE, "--service-account", SA, "--env-json", json.dumps(env),
    ]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dps.time, "sleep", lambda _s: None)


def test_create_path_builds_iap_service_and_grants_invoker() -> None:
    http = FakeHttp(_base_routes(exists=False, policy={"etag": "e1"}))
    env = {"PORTAL_MODE": "iap", "MASTER_PROMPT_SHEET_ID": "a,b@c##d", "AUDIT_LOG_BUCKET": "ws"}
    assert dps.main(_argv(env), http=http) == 0

    create = http.by("POST", "/services")
    assert len(create) == 1 and not http.by("PATCH", SVC)
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(create[0][1]).query) == {"serviceId": [SVC]}
    body = create[0][2]
    assert body["iapEnabled"] is True and body["invokerIamDisabled"] is False
    assert body["ingress"] == "INGRESS_TRAFFIC_ALL"
    tpl = body["template"]
    assert tpl["serviceAccount"] == SA and tpl["timeout"] == "300s"
    assert tpl["scaling"] == {"minInstanceCount": 0, "maxInstanceCount": 5}
    assert "maxInstanceRequestConcurrency" not in tpl  # server default
    (container,) = tpl["containers"]
    assert container["image"] == IMAGE
    assert container["resources"] == {"limits": {"cpu": "1", "memory": "1Gi"}}
    assert {e["name"]: e["value"] for e in container["env"]} == env  # commas/@/## survive verbatim

    (set_call,) = http.by("POST", ":setIamPolicy")
    policy = set_call[2]["policy"]
    assert policy["etag"] == "e1"
    assert {"role": "roles/run.invoker", "members": [IAP_MEMBER]} in policy["bindings"]
    assert http.calls[0][1] == GEN_URL  # service identity first


def test_patch_path_uses_explicit_update_mask() -> None:
    http = FakeHttp(_base_routes(exists=True, policy={"etag": "e1"}))
    assert dps.main(_argv({"PORTAL_MODE": "iap"}), http=http) == 0
    (patch,) = http.by("PATCH", SVC)
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(patch[1]).query)
    assert qs == {"updateMask": ["iapEnabled,invokerIamDisabled,ingress,template"]}
    assert patch[2]["iapEnabled"] is True
    assert not http.by("POST", "/services")


def test_invoker_binding_merge_is_idempotent_and_preserves_others() -> None:
    existing = {
        "etag": "e2",
        "bindings": [
            {"role": "roles/run.invoker", "members": ["user:a@x.com", IAP_MEMBER]},
            {"role": "roles/run.viewer", "members": ["group:g@x.com"]},
        ],
    }
    http = FakeHttp(_base_routes(exists=True, policy=existing))
    assert dps.main(_argv({}), http=http) == 0
    assert not http.by("POST", ":setIamPolicy")  # already present -> no write

    policy = {"etag": "e3", "bindings": [{"role": "roles/run.invoker", "members": ["user:a@x.com"]}]}
    assert dps.merge_member(policy, "roles/run.invoker", IAP_MEMBER) is True
    assert policy["bindings"][0]["members"] == ["user:a@x.com", IAP_MEMBER]
    assert dps.merge_member(policy, "roles/run.invoker", IAP_MEMBER) is False


def test_set_iam_policy_retries_on_etag_conflict() -> None:
    http = FakeHttp(
        _base_routes(
            exists=True,
            policy={"etag": "e1"},
            set_responses=[(409, {"error": {"status": "ABORTED"}}), (200, {})],
        )
    )
    assert dps.main(_argv({}), http=http) == 0
    assert len(http.by("POST", ":setIamPolicy")) == 2
    assert len(http.by("GET", ":getIamPolicy")) == 2  # re-read before retrying


def test_set_iam_policy_permission_error_fails_loudly(capsys) -> None:
    http = FakeHttp(
        _base_routes(exists=True, policy={"etag": "e1"}, set_responses=[(403, {"error": {"status": "PERMISSION_DENIED"}})])
    )
    assert dps.main(_argv({}), http=http) == 1
    assert len(http.by("POST", ":setIamPolicy")) == 1
    assert "PERMISSION_DENIED" in capsys.readouterr().err


def test_run_lro_error_surfaces(capsys) -> None:
    routes = _base_routes(exists=False, policy={})
    routes[("GET", f"{RUN}/{OP}")] = [
        (200, {"name": OP, "done": True, "error": {"code": 9, "message": "Revision failed to start: port 8080"}})
    ]
    http = FakeHttp(routes)
    assert dps.main(_argv({}), http=http) == 1
    assert "Revision failed to start" in capsys.readouterr().err
    assert not http.by("POST", ":setIamPolicy")


def test_service_identity_error_surfaces(capsys) -> None:
    routes = _base_routes(exists=False, policy={})
    routes[("POST", GEN_URL)] = [(403, {"error": {"status": "PERMISSION_DENIED"}})]
    http = FakeHttp(routes)
    assert dps.main(_argv({}), http=http) == 1
    assert "generateServiceIdentity" in capsys.readouterr().err
    assert not http.by("GET", SVC)


def test_refuses_success_when_iap_not_enabled_after_deploy(capsys) -> None:
    routes = _base_routes(exists=True, policy={})
    routes[("GET", SVC_URL)] = [(200, {"name": NAME}), (200, {"name": NAME, "iapEnabled": False})]
    assert dps.main(_argv({}), http=FakeHttp(routes)) == 1
    assert "iapEnabled is not true" in capsys.readouterr().err


def test_rejects_unpinned_image_and_bad_env_json(capsys) -> None:
    http = FakeHttp(_base_routes(exists=False, policy={}))
    argv = _argv({})
    argv[argv.index("--image") + 1] = "asia-southeast1-docker.pkg.dev/p/r/w:latest"
    assert dps.main(argv, http=http) == 1
    argv = _argv({})
    argv[argv.index("--env-json") + 1] = "not json"
    assert dps.main(argv, http=FakeHttp(_base_routes(exists=False, policy={}))) == 1


def test_main_tf_runs_the_script_not_gcloud() -> None:
    main = (IAC_DIR / "main.tf").read_text()
    start = main.index('resource "terraform_data" "portal_service"')
    block = main[start : main.index("\n}\n", start)]
    assert "deploy/deploy_portal_service.py" in block
    assert "gcloud" not in block and "^##^" not in main
    assert "when" not in block  # no destroy-time provisioner
    assert "sha256(local.portal_env_json)" in block and "var.container_image" in block
    assert '--env-json "$PORTAL_ENV_JSON"' in block
