"""Round 59: several copies of the stack can live in one project without touching each other.

Pins the contract that makes a second stack (another name_prefix) safe next to the first:
1. deploy/deploy_reasoning_engine.py only updates this stack's ReasoningEngine (by display name,
   and never one that runs as another stack's service account) and only this stack's agent in a
   Gemini Enterprise app; it binds no app it was not told about (no implicit `cctv-audit`).
2. Every resource name in main.tf / bootstrap/main.tf derives from name_prefix; no literal project,
   tenant or region value is left in either root, and the pinned legacy names of the first
   environment live only in its tfvars.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
RE_BASE = "projects/1/locations/us-central1/reasoningEngines"
OWN_SA = "stack-a-worker@my-project.iam.gserviceaccount.com"
OTHER_SA = "stack-b-worker@my-project.iam.gserviceaccount.com"
DE = "https://discoveryengine.googleapis.com/v1alpha/projects/my-project/locations/global/collections/default_collection"


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "deploy_reasoning_engine_stack_test", ROOT / "deploy" / "deploy_reasoning_engine.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = _load_deploy_module()


def _engine(eng_id: str, display_name: str, sa: str, updated: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    return {
        "name": f"{RE_BASE}/{eng_id}",
        "displayName": display_name,
        "spec": {"serviceAccount": sa},
        "updateTime": updated,
    }


# --------------------------------------------------------------------------- ReasoningEngine lookup


def test_engine_lookup_ignores_other_stacks_and_the_old_hardcoded_legacy_name():
    engines = [
        _engine("1", "stack-b-agent", OTHER_SA),
        _engine("2", "cctv-audit-agent", OTHER_SA),  # used to be matched implicitly by every stack
        _engine("3", "stack-a-agent", OWN_SA, "2026-09-02T00:00:00Z"),
        _engine("4", "stack-a-agent", OWN_SA, "2026-09-03T00:00:00Z"),
    ]
    found = deploy.find_stack_reasoning_engines(engines, ["stack-a-agent"], OWN_SA)
    assert [e["name"].rsplit("/", 1)[1] for e in found] == ["4", "3"]  # newest first


def test_engine_lookup_legacy_name_only_when_configured():
    engines = [_engine("7", "old-name", OWN_SA)]
    assert deploy.find_stack_reasoning_engines(engines, ["stack-a-agent"], OWN_SA) == []
    assert deploy.find_stack_reasoning_engines(engines, ["stack-a-agent", "old-name"], OWN_SA) == engines


def test_engine_lookup_refuses_same_named_engine_of_another_service_account():
    engines = [_engine("9", "stack-a-agent", OTHER_SA)]
    with pytest.raises(SystemExit, match="another stack"):
        deploy.find_stack_reasoning_engines(engines, ["stack-a-agent"], OWN_SA)


def test_create_patches_only_its_own_engine(monkeypatch):
    calls: list[tuple[str, str]] = []
    listing = {
        "reasoningEngines": [
            _engine("1", "stack-b-agent", OTHER_SA, "2026-09-05T00:00:00Z"),
            _engine("2", "stack-a-agent", OWN_SA),
        ]
    }

    def fake_call(method, url, body=None, project_id="", ignore_errors=False):
        calls.append((method, url))
        if method == "GET" and url.endswith("/reasoningEngines"):
            return listing
        if method == "GET" and "/operations/" in url:
            return {"done": True, "response": {"name": f"{RE_BASE}/2"}}
        if method == "GET" and url.endswith("/agents"):
            return {"agents": []}
        if method == "GET":
            return {"name": url}  # data store / GE app exist
        return {"name": f"{RE_BASE}/2/operations/op-1"}

    monkeypatch.setattr(deploy, "_call", fake_call)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    rc = deploy.main(
        [
            "--project-id", "my-project", "--location", "us-central1", "create",
            "--image-uri", "europe-west4-docker.pkg.dev/my-project/stack-a-images/w@sha256:" + "0" * 64,
            "--display-name", "stack-a-agent", "--gcp-location", "europe-west4",
            "--master-prompt-sheet-id", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd",
            "--service-account", OWN_SA, "--ge-engine-id", "stack-a-ge",
        ]
    )
    assert rc == 0
    engine_mutations = [(m, u) for m, u in calls if m != "GET" and "aiplatform.googleapis.com" in u]
    assert len(engine_mutations) == 1 and engine_mutations[0][0] == "PATCH"
    assert f"{RE_BASE}/2?" in engine_mutations[0][1]
    assert not any(f"{RE_BASE}/1" in u for _, u in calls)


def test_create_requires_display_name_and_region():
    body_kwargs = dict(
        project_id="my-project",
        location="us-central1",
        image_uri="img",
        master_prompt_sheet_id="1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd",
    )
    with pytest.raises(ValueError, match="display_name"):
        deploy.build_reasoning_engine_body(**body_kwargs, display_name="", gcp_location="europe-west4")
    with pytest.raises(ValueError, match="gcp_location"):
        deploy.build_reasoning_engine_body(**body_kwargs, display_name="stack-a-agent", gcp_location="")


def test_engine_description_uses_configured_brand_only():
    common = dict(
        project_id="my-project",
        location="us-central1",
        image_uri="img",
        master_prompt_sheet_id="1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd",
        display_name="stack-a-agent",
        gcp_location="europe-west4",
    )
    assert deploy.build_reasoning_engine_body(**common)["description"].startswith("门店 CCTV AI 合规稽核 Agent")
    branded = deploy.build_reasoning_engine_body(**common, company_name="CHAGEE", tenant_label="霸王茶姬")
    # Byte-identical to the description the first environment's engine has always carried.
    assert branded["description"].startswith("CHAGEE 霸王茶姬门店 CCTV AI 合规稽核 Agent（")


# --------------------------------------------------------------------------- Gemini Enterprise binding


def _adk_agent(agent_id: str, engine: str, display_name: str, bound_to: str) -> dict[str, Any]:
    return {
        "name": f"projects/my-project/locations/global/collections/default_collection/engines/{engine}"
        f"/assistants/default_assistant/agents/{agent_id}",
        "displayName": display_name,
        "adkAgentDefinition": {"provisionedReasoningEngine": {"reasoningEngine": bound_to}},
    }


def test_select_stack_agents_matches_own_engine_or_owned_namesake_only():
    engines = {
        f"{RE_BASE}/deleted": {"_http_error": 404},
        f"{RE_BASE}/prod": {"name": f"{RE_BASE}/prod", "spec": {"serviceAccount": OTHER_SA}},
    }
    agents = [
        _adk_agent("a1", "shared", "Stack B agent", f"{RE_BASE}/other"),
        _adk_agent("a2", "shared", "custom name", f"{RE_BASE}/mine"),
        _adk_agent("a3", "shared", "Stack A agent", f"{RE_BASE}/deleted"),
        _adk_agent("a4", "shared", "Stack A agent", f"{RE_BASE}/prod"),
        {"name": "deep_research", "displayName": "Deep Research"},
    ]
    picked, refused = deploy.select_stack_agents(
        agents, f"{RE_BASE}/mine", "Stack A agent", service_account=OWN_SA, get_engine=engines.__getitem__
    )
    assert [a["name"].rsplit("/", 1)[1] for a in picked] == ["a2", "a3"]
    assert [a["name"].rsplit("/", 1)[1] for a, _ in refused] == ["a4"]
    assert OTHER_SA in refused[0][1]


def test_select_stack_agents_without_known_sa_never_adopts_a_live_namesake():
    engines = {f"{RE_BASE}/x": {"spec": {"serviceAccount": ""}}}
    agents = [_adk_agent("n", "shared", "Stack A agent", f"{RE_BASE}/x")]
    picked, refused = deploy.select_stack_agents(
        agents, f"{RE_BASE}/mine", "Stack A agent", service_account="", get_engine=engines.__getitem__
    )
    assert picked == [] and len(refused) == 1


def test_reasoning_engine_url_uses_the_engines_region():
    assert deploy.reasoning_engine_url(f"{RE_BASE}/7") == (
        f"https://us-central1-aiplatform.googleapis.com/v1/{RE_BASE}/7"
    )


class _FakeDiscoveryEngine:
    """Records every call; knows which apps exist and which agents they hold."""

    def __init__(
        self,
        engines: dict[str, list[dict[str, Any]]],
        reasoning_engines: dict[str, str | int] | None = None,
    ):
        self.engines = engines
        # ReasoningEngine name -> the service account it runs as, or an HTTP error code for its GET.
        self.reasoning_engines = reasoning_engines or {}
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method, url, body=None, project_id="", ignore_errors=False):
        self.calls.append((method, url, body))
        if method == "GET" and "aiplatform.googleapis.com" in url:
            name = url.split("/v1/", 1)[1]
            owner = self.reasoning_engines.get(name, 404)
            if isinstance(owner, int):
                return {"_http_error": owner, "_error_body": "err"}
            return {"name": name, "spec": {"serviceAccount": owner}}
        if method == "GET" and "/dataStores/" in url:
            return {"name": url}
        if method == "GET" and url.endswith("/agents"):
            engine = url.split("/engines/")[1].split("/")[0]
            return {"agents": self.engines.get(engine, [])}
        if method == "GET" and "/engines/" in url:
            engine = url.rsplit("/", 1)[1]
            return {"name": engine} if engine in self.engines else {"_http_error": 404, "_error_body": "nf"}
        return {"name": "created-or-patched"}

    def mutations(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


def _bind(fake: _FakeDiscoveryEngine, monkeypatch, **kwargs):
    monkeypatch.setattr(deploy, "_call", fake)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    return deploy.ensure_gemini_enterprise_and_bind_agent(
        project_id="my-project",
        reasoning_engine_name=f"{RE_BASE}/mine",
        ge_engine_id="stack-a-ge",
        ge_display_name="Stack A app",
        agent_display_name="Stack A agent",
        **kwargs,
    )


def test_no_implicit_binding_to_cctv_audit(monkeypatch):
    other_agent = _adk_agent("x", "cctv-audit", "门店视频稽核", f"{RE_BASE}/prod")
    fake = _FakeDiscoveryEngine({"stack-a-ge": [], "cctv-audit": [other_agent]})
    _bind(fake, monkeypatch)
    assert not any("/engines/cctv-audit" in url for _, url, _ in fake.calls)
    posts = fake.mutations()
    assert len(posts) == 1 and posts[0][1] == f"{DE}/engines/stack-a-ge/assistants/default_assistant/agents"


def test_extra_app_updates_only_this_stacks_agent(monkeypatch):
    mine = _adk_agent("m", "cctv-audit", "门店视频稽核", f"{RE_BASE}/mine")
    theirs = _adk_agent("t", "cctv-audit", "Stack B agent", f"{RE_BASE}/other")
    fake = _FakeDiscoveryEngine({"stack-a-ge": [], "cctv-audit": [mine, theirs]})
    _bind(fake, monkeypatch, extra_engine_ids=["cctv-audit", "does-not-exist"])
    patched = [url for m, url, _ in fake.mutations() if m == "PATCH"]
    assert len(patched) == 1 and patched[0].split("?")[0].endswith("/agents/m")
    assert not any("/agents/t" in url for _, url, _ in fake.calls)
    # A missing extra app is skipped, never created.
    assert not any("engineId=does-not-exist" in url for _, url, _ in fake.calls)
    # The existing agent keeps its display name.
    patch_body = next(b for m, _, b in fake.mutations() if m == "PATCH")
    assert patch_body["displayName"] == "门店视频稽核"


def test_new_app_uses_configured_company_and_example_folder(monkeypatch):
    fake = _FakeDiscoveryEngine({})
    _bind(fake, monkeypatch, company_name="ACME", tenant_label="Acme Tea",
          example_folder_url="https://drive.google.com/drive/folders/abc")
    create = next(b for m, u, b in fake.mutations() if "engines?engineId=stack-a-ge" in u)
    assert create["commonConfig"] == {"companyName": "ACME"}
    agent = next(b for m, u, b in fake.mutations() if u.endswith("/agents"))
    assert agent["displayName"] == "Stack A agent"
    assert agent["starterPrompts"][0]["text"].endswith("https://drive.google.com/drive/folders/abc")
    assert "Acme Tea" in agent["description"] and "霸王茶姬" not in agent["description"]

    fake_plain = _FakeDiscoveryEngine({})
    _bind(fake_plain, monkeypatch)
    create_plain = next(b for m, u, b in fake_plain.mutations() if "engines?engineId=stack-a-ge" in u)
    assert "commonConfig" not in create_plain


def test_failed_agent_update_is_reported_not_claimed_as_success(monkeypatch, capsys):
    mine = _adk_agent("m", "stack-a-ge", "Stack A agent", f"{RE_BASE}/mine")
    fake = _FakeDiscoveryEngine({"stack-a-ge": [mine]})
    original = fake.__call__

    def failing(method, url, body=None, project_id="", ignore_errors=False):
        if method == "PATCH":
            fake.calls.append((method, url, body))
            return {"_http_error": 400, "_error_body": "bad field"}
        return original(method, url, body, project_id, ignore_errors)

    monkeypatch.setattr(deploy, "_call", failing)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    deploy.ensure_gemini_enterprise_and_bind_agent(
        project_id="my-project",
        reasoning_engine_name=f"{RE_BASE}/mine",
        ge_engine_id="stack-a-ge",
        ge_display_name="Stack A app",
        agent_display_name="Stack A agent",
    )
    out = capsys.readouterr()
    assert "FAILED: HTTP 400 bad field" in out.err
    assert "✅ Updated" not in out.out


# --------------------------------------------------------------------------- Terraform naming


def _tf(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["main.tf", "bootstrap/main.tf", "cloudbuild.yaml"])
def test_no_project_tenant_or_region_literal_in_stack_code(path):
    text = _tf(path)
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    if path == "cloudbuild.yaml":
        # Only the substitution defaults (first environment) may carry its names.
        code = code.split("substitutions:")[0] + code.split("options:")[1]
    for literal in ("chagee-cctv", "cctv-staging",
                    "asia-southeast1", "us-central1", "Asia/Singapore", '"cctv-audit"', "CHAGEE"):
        assert literal not in code, f"{path} still hard-codes {literal!r}"


def test_both_roots_derive_names_from_name_prefix():
    main, boot = _tf("main.tf"), _tf("bootstrap/main.tf")
    for text in (main, boot):
        assert re.search(r'regex\("\^\[a-z\]\[a-z0-9-\]\{0,18\}\[a-z0-9\]\$", var\.name_prefix\)', text)
        assert '"${var.name_prefix}-worker"' in text
        assert '"${var.name_prefix}-images"' in text
    for derived in ("-watchdog", "-agent", "-ge", "-staging", "-workspace"):
        assert f'{derived}"' in main
    for derived in ("-deployer", "-deploy"):
        assert f'"${{var.name_prefix}}{derived}"' in boot
    # Cloud Run URL env follows the service name instead of a literal.
    assert 'value = "https://${local.worker_service_name}-${data.google_project.current.number}.${var.region}.run.app"' in main


def test_reasoning_engine_triggers_are_unchanged_for_the_first_environment():
    """The trigger list decides whether an apply redeploys the ReasoningEngine; its inputs must
    evaluate to exactly what they were before name_prefix, or the next prod apply redeploys it."""
    main = _tf("main.tf")
    start = main.index("triggers_replace = [")
    block = main[start: main.index("\n  ]", start)]
    items = [line.strip().rstrip(",") for line in block.splitlines()[1:] if line.strip()]
    assert items == [
        "var.project_id",
        "var.reasoning_engine_location",
        "var.container_image",
        "var.master_prompt_sheet_id",
        "local.ge_engine_id",
        "google_storage_bucket.staging_bucket.name",
        "data.google_service_account.audit_worker_sa.email",
        'var.enable_standalone_cloud_run ? google_cloud_run_v2_service.cctv_audit_worker[0].uri : ""',
        "var.workspace_impersonate_user",
    ]


def test_bootstrap_passes_every_stack_substitution_to_cloud_build():
    boot = _tf("bootstrap/main.tf")
    block = boot[boot.index("build_substitutions = {"): boot.index("}", boot.index("build_substitutions = {"))]
    for key in ("_ENV", "_REGION", "_REPO", "_IMAGE", "_DEPLOYER_SA_ID"):
        assert key in block
    assert "merge(local.build_substitutions" in boot
    assert "--substitutions=${join(" in boot


@pytest.mark.parametrize("prefix,ok", [
    ("chagee-cctv-audit", True), ("ab", True), ("a" * 20, True),
    ("a", False), ("a" * 21, False), ("1abc", False), ("abc-", False), ("Abc", False), ("ab_c", False),
])
def test_name_prefix_rule_keeps_derived_ids_valid(prefix, ok):
    rule = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")
    assert bool(rule.match(prefix)) is ok
    if ok:
        assert len(f"{prefix}-deployer") <= 30 and len(f"{prefix}-worker") >= 6  # service-account ID limits


# --------------------------------------------------------------------------- Round 59b: agent ownership
# A second stack that reuses production's agent display name and lists one of production's GE apps
# must never rebind production's agent to its own ReasoningEngine.

PROD_RE = f"{RE_BASE}/prod"


def _patched_agents(fake: _FakeDiscoveryEngine) -> list[str]:
    return [url.split("?")[0].rsplit("/", 1)[1] for m, url, _ in fake.mutations() if m == "PATCH"]


def test_prod_owned_namesake_agent_is_not_patched_and_deploy_fails(monkeypatch, capsys):
    prod_agent = _adk_agent("prod", "cctv-audit", "Stack A agent", PROD_RE)
    fake = _FakeDiscoveryEngine({"stack-a-ge": [], "cctv-audit": [prod_agent]}, {PROD_RE: OTHER_SA})
    with pytest.raises(SystemExit) as exc:
        _bind(fake, monkeypatch, extra_engine_ids=["cctv-audit"], service_account=OWN_SA)
    assert "cctv-audit" in str(exc.value)
    assert not any("/engines/cctv-audit/" in url for _, url, _ in fake.mutations())
    assert "Not touching agent" in capsys.readouterr().err


def test_agent_bound_to_this_stacks_engine_is_patched_but_foreign_namesake_is_not(monkeypatch):
    mine = _adk_agent("mine", "cctv-audit", "门店视频稽核", f"{RE_BASE}/mine")
    prod_agent = _adk_agent("prod", "cctv-audit", "Stack A agent", PROD_RE)
    fake = _FakeDiscoveryEngine({"stack-a-ge": [], "cctv-audit": [mine, prod_agent]}, {PROD_RE: OTHER_SA})
    _bind(fake, monkeypatch, extra_engine_ids=["cctv-audit"], service_account=OWN_SA)
    assert _patched_agents(fake) == ["mine"]


def test_namesake_bound_to_deleted_engine_is_claimed(monkeypatch):
    orphan = _adk_agent("orphan", "stack-a-ge", "Stack A agent", f"{RE_BASE}/deleted")
    prod_agent = _adk_agent("prod", "stack-a-ge", "Stack A agent", PROD_RE)
    fake = _FakeDiscoveryEngine(
        {"stack-a-ge": [orphan, prod_agent]}, {f"{RE_BASE}/deleted": 404, PROD_RE: OTHER_SA}
    )
    _bind(fake, monkeypatch, service_account=OWN_SA)
    assert _patched_agents(fake) == ["orphan"]
    patch_body = next(b for m, _, b in fake.mutations() if m == "PATCH")
    assert patch_body["adkAgentDefinition"]["provisionedReasoningEngine"]["reasoningEngine"] == f"{RE_BASE}/mine"


def test_namesake_bound_to_an_engine_of_this_stacks_sa_is_claimed(monkeypatch):
    previous = _adk_agent("prev", "stack-a-ge", "Stack A agent", f"{RE_BASE}/previous")
    prod_agent = _adk_agent("prod", "stack-a-ge", "Stack A agent", PROD_RE)
    fake = _FakeDiscoveryEngine(
        {"stack-a-ge": [previous, prod_agent]}, {f"{RE_BASE}/previous": OWN_SA, PROD_RE: OTHER_SA}
    )
    _bind(fake, monkeypatch, service_account=OWN_SA)
    assert _patched_agents(fake) == ["prev"]


@pytest.mark.parametrize("code", [403, 500])
def test_engine_lookup_error_other_than_404_leaves_namesake_untouched(monkeypatch, capsys, code):
    unknown = _adk_agent("unknown", "stack-a-ge", "Stack A agent", PROD_RE)
    fake = _FakeDiscoveryEngine({"stack-a-ge": [unknown]}, {PROD_RE: code})
    with pytest.raises(SystemExit):
        _bind(fake, monkeypatch, service_account=OWN_SA)
    assert not any("/agents" in url for _, url, _ in fake.mutations())
    assert f"HTTP {code}" in capsys.readouterr().err


def test_unbound_namesake_is_claimed(monkeypatch):
    unbound = {
        "name": f"{DE}/engines/stack-a-ge/assistants/default_assistant/agents/unbound",
        "displayName": "Stack A agent",
        "adkAgentDefinition": {},
    }
    prod_agent = _adk_agent("prod", "stack-a-ge", "Stack A agent", PROD_RE)
    fake = _FakeDiscoveryEngine({"stack-a-ge": [unbound, prod_agent]}, {PROD_RE: OTHER_SA})
    _bind(fake, monkeypatch, service_account=OWN_SA)
    assert _patched_agents(fake) == ["unbound"]


def test_init_sop_sheet_xlsx_export_and_sa_impersonation(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET
    import zipfile
    from unittest import mock

    spec = importlib.util.spec_from_file_location(
        "init_sop_sheet_test", ROOT / "scripts" / "init_sop_sheet.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tabs = mod.load_snapshot(mod.DEFAULT_SNAPSHOT)
    xlsx_path = tmp_path / "out.xlsx"
    rc = mod.main(["--export-xlsx", str(xlsx_path)])
    assert rc == 0 and xlsx_path.is_file()

    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet_names = [el.attrib["name"] for el in wb.findall("main:sheets/main:sheet", ns)]
        assert sheet_names == [t["title"] for t in tabs]
        for idx, tab in enumerate(tabs, start=1):
            ws = ET.fromstring(zf.read(f"xl/worksheets/sheet{idx}.xml"))
            parsed_rows = []
            for row_el in ws.findall("main:sheetData/main:row", ns):
                cells = []
                for c_el in row_el.findall("main:c", ns):
                    assert c_el.attrib.get("t") == "inlineStr"
                    t_el = c_el.find("main:is/main:t", ns)
                    cells.append(t_el.text or "" if t_el is not None else "")
                parsed_rows.append(cells)
            assert parsed_rows == [[str(c) for c in r] for r in tab["values"]]

    tfv = tmp_path / "demo.tfvars"
    tfv.write_text(
        'project_id = "my-proj"\n'
        'name_prefix = "cctv-demo"\n'
        'workspace_impersonate_user = "bot@example.com"\n',
        encoding="utf-8",
    )
    sa, bot = mod.resolve_impersonation_targets(tfv, "", "")
    assert sa == "cctv-demo-worker@my-proj.iam.gserviceaccount.com"
    assert bot == "bot@example.com"

    # When DWD refresh fails (e.g., before Step 6 DWD is configured), falls back to direct SA impersonation
    # using plain cloud-platform ADC (never requesting spreadsheets scope on the user's gcloud client ID).
    adc_scopes_requested = []
    imp_calls = []

    class _FakeImpCreds:
        def __init__(self, *, source_credentials, target_principal, target_scopes, subject=None, lifetime=3600):
            imp_calls.append((target_principal, tuple(target_scopes), subject))
            self.subject = subject

        def refresh(self, _req):
            if self.subject:
                raise RuntimeError("unauthorized_client: DWD not yet active")

    def _fake_adc(scopes=None):
        adc_scopes_requested.append(tuple(scopes or ()))
        return (mock.MagicMock(), "my-proj")

    with (
        mock.patch("google.auth.default", side_effect=_fake_adc),
        mock.patch("google.auth.impersonated_credentials.Credentials", _FakeImpCreds),
    ):
        creds = mod.build_sheets_credentials(service_account=sa, impersonate_user=bot)
        assert creds.subject is None
        assert adc_scopes_requested == [("https://www.googleapis.com/auth/cloud-platform",)]
        assert len(imp_calls) == 2
        assert imp_calls[0] == (sa, ("https://www.googleapis.com/auth/spreadsheets",), "bot@example.com")
        assert imp_calls[1] == (sa, ("https://www.googleapis.com/auth/spreadsheets",), None)


# --------------------------------------------------------------------------- Round 63: native terraform destroy


def test_destroy_stack_deletes_only_this_stacks_resources_and_preserves_foreign_stacks(monkeypatch):
    calls: list[tuple[str, str]] = []
    re_list = {
        "reasoningEngines": [
            _engine("1", "stack-b-agent", OTHER_SA),
            {
                **_engine("2", "stack-a-agent", OWN_SA),
                "spec": {
                    "serviceAccount": OWN_SA,
                    "deploymentSpec": {"env": [{"name": "GCP_LOCATION", "value": "asia-southeast1"}]},
                },
            },
        ]
    }
    agents_list = {
        "agents": [
            _adk_agent("a1", "stack-a-ge", "Stack A agent", f"{RE_BASE}/2"),
            {"name": "built_in", "displayName": "Deep Research"},
        ]
    }

    def fake_call(method, url, body=None, project_id="", ignore_errors=False):
        calls.append((method, url))
        if method == "GET" and url.endswith("/reasoningEngines"):
            return re_list
        if method == "GET" and url.endswith("/agents"):
            return agents_list
        if method == "GET" and "/storage/v1/b/my-project-stack-a-staging/o?" in url:
            return {
                "items": [
                    {"name": "jobs/u1/j1.json", "generation": "111"},
                    {"name": "jobs/media/j1/seg_0.mp4", "generation": "222"},
                ]
            }
        if method == "GET" and url.endswith("/services/stack-a-worker"):
            return {"name": url, "template": {"serviceAccount": OWN_SA}}
        return {}

    monkeypatch.setattr(deploy, "_call", fake_call)
    rc = deploy.main(
        [
            "--project-id", "my-project",
            "--location", "us-central1",
            "destroy-stack",
            "--ge-engine-id", "stack-a-ge",
            "--staging-bucket", "my-project-stack-a-staging",
            "--service-account", OWN_SA,
        ]
    )
    assert rc == 0
    deleted_urls = [u for m, u in calls if m == "DELETE"]
    # Stack A's GE agent, GE Engine, and DataStore are deleted
    assert any(u.endswith("/agents/a1") for u in deleted_urls)
    assert any(u.endswith("/engines/stack-a-ge") for u in deleted_urls)
    assert any(u.endswith("/dataStores/stack-a-ge-store") for u in deleted_urls)
    # Stack A's ReasoningEngine is force-deleted; Stack B's ReasoningEngine is untouched
    assert any(u.endswith(f"{RE_BASE}/2?force=true") for u in deleted_urls)
    assert not any(f"{RE_BASE}/1" in u for _, u in calls)
    # Staging bucket objects are deleted
    assert any("/o/jobs%2Fu1%2Fj1.json?generation=111" in u for u in deleted_urls)
    assert any("/o/jobs%2Fmedia%2Fj1%2Fseg_0.mp4?generation=222" in u for u in deleted_urls)
    # Stack A's Cloud Run service is deleted
    assert any(u.endswith("/locations/asia-southeast1/services/stack-a-worker") for u in deleted_urls)


def test_destroy_stack_refuses_foreign_ge_engine_foreign_cloud_run_and_tfstate_bucket(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_call(method, url, body=None, project_id="", ignore_errors=False):
        calls.append((method, url))
        if method == "GET" and url.endswith("/reasoningEngines"):
            return {"reasoningEngines": [_engine("1", "stack-b-agent", OTHER_SA)]}
        if method == "GET" and url.endswith("/agents"):
            return {"agents": [_adk_agent("b1", "shared-ge", "Stack B agent", f"{RE_BASE}/1")]}
        if method == "GET" and url.endswith("/services/stack-a-worker"):
            return {"name": url, "template": {"serviceAccount": OTHER_SA}}
        return {}

    monkeypatch.setattr(deploy, "_call", fake_call)
    summary = deploy.destroy_stack(
        project_id="my-project",
        location="us-central1",
        service_account=OWN_SA,
        ge_engine_id="shared-ge",
        staging_bucket="my-project-tfstate",
        image_uri="asia-southeast1-docker.pkg.dev/my-project/stack-a-images/w@sha256:" + "0" * 64,
    )
    deleted_urls = [u for m, u in calls if m == "DELETE"]
    assert deleted_urls == []
    assert summary["deleted_ge_engine"] is False
    assert summary["deleted_bucket_objects"] == 0
    assert summary["deleted_cloud_run_service"] == ""


def test_create_rejects_destroy_placeholder_image_uri():
    with pytest.raises(SystemExit, match="placeholder"):
        deploy.main(
            [
                "--project-id", "my-project",
                "--location", "us-central1",
                "create",
                "--image-uri",
                "placeholder-docker.pkg.dev/project/repo/image@sha256:" + "0" * 64,
                "--display-name", "stack-a-agent",
                "--gcp-location", "asia-southeast1",
                "--master-prompt-sheet-id", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd",
                "--ge-engine-id", "stack-a-ge",
            ]
        )


def test_terraform_roots_support_native_destroy_without_manual_workarounds():
    main, boot = _tf("main.tf"), _tf("bootstrap/main.tf")
    assert "when    = destroy" in main
    assert "destroy-stack" in main
    assert "deletion_protection = false" in main
    assert "force_destroy               = true" in main
    assert 'default = "placeholder-docker.pkg.dev/project/repo/image@sha256:' in main
    assert "prevent_destroy" not in boot



def test_workspace_bucket_versioned_seeded_and_wired():
    """Round 71: GCS mode is turnkey - versioned workspace bucket, seeded SOP workbook, GCS_SOP_URI env."""
    main = _tf("main.tf")
    start = main.index('resource "google_storage_bucket" "workspace_bucket"')
    bucket = main[start: main.index("\n}\n", start)]
    assert "name                        = local.workspace_bucket_name" in bucket
    assert "versioning {\n    enabled = true\n  }" in bucket
    # Only archived generations beyond the 30 newest are pruned; no age-based delete of live objects.
    assert "num_newer_versions = 30" in bucket and 'with_state         = "ARCHIVED"' in bucket
    assert not re.search(r"\bage\s*=", bucket)
    assert '"${var.project_id}-${var.name_prefix}-workspace"' in main
    assert 'gcs_sop_uri            = "gs://${local.workspace_bucket_name}/sop/master_sheet.xlsx"' in main
    for res in ("workspace_bucket_rw", "workspace_bucket_meta_reader"):
        s = main.index(f'resource "google_storage_bucket_iam_member" "{res}"')
        blk = main[s: main.index("\n}\n", s)]
        assert "bucket = google_storage_bucket.workspace_bucket.name" in blk
        assert 'member = "serviceAccount:${data.google_service_account.audit_worker_sa.email}"' in blk
    s = main.index('resource "terraform_data" "seed_sop_workbook"')
    seed = main[s: main.index("\n}\n", s)]
    assert 'seed-object' in seed and '--name "sop/master_sheet.xlsx"' in seed
    assert '--source "${path.module}/sop/master_sheet.xlsx"' in seed
    assert "google_storage_bucket.workspace_bucket.name" in seed.split("provisioner")[0]
    assert 'resource "google_storage_bucket_object" "default_sop_workbook"' not in main
    rm = main[main.index("removed {\n  from = google_storage_bucket_object.default_sop_workbook"):]
    assert "destroy = false" in rm[: rm.index("\n}\n")]
    assert (ROOT / "sop" / "master_sheet.xlsx").is_file()
    sop = 'gs://${google_storage_bucket.workspace_bucket.name}/sop/master_sheet.xlsx'
    assert f'name  = "GCS_SOP_URI"\n        value = "{sop}"' in main
    assert f'--gcs-sop-uri "{sop}"' in main
    dep = main[main.index("depends_on = [", main.index('resource "terraform_data" "vertex_reasoning_engine"')):]
    dep = dep[: dep.index("]")]
    assert "google_storage_bucket_iam_member.workspace_bucket_rw" in dep
    assert "terraform_data.seed_sop_workbook" in dep
    assert 'output "workspace_bucket_name"' in main and 'output "gcs_sop_uri"' in main


def test_reasoning_engine_body_carries_gcs_sop_uri_only_when_set():
    common = dict(
        project_id="my-project",
        location="us-central1",
        image_uri="img",
        master_prompt_sheet_id="",
        display_name="stack-a-agent",
        gcp_location="europe-west4",
    )
    env = lambda body: {e["name"]: e["value"] for e in body["spec"]["deploymentSpec"]["env"]}  # noqa: E731
    assert "GCS_SOP_URI" not in env(deploy.build_reasoning_engine_body(**common))
    uri = "gs://my-project-stack-a-workspace/sop/master_sheet.xlsx"
    assert env(deploy.build_reasoning_engine_body(**common, gcs_sop_uri=uri))["GCS_SOP_URI"] == uri


def _tf_block(main: str, header: str) -> str:
    start = main.index(header)
    return main[start: main.index("\n}\n", start)]


def test_vertex_reasoning_engine_replacement_never_triggers_destroy_stack():
    """Round 71b (incident 2026-10-10): Terraform runs `when = destroy` provisioners on REPLACEMENT.

    vertex_reasoning_engine is replaced on every new container_image digest, so it must never carry
    the full-stack teardown; that lives on stack_teardown, keyed only by stack-identity invariants.
    """
    main = _tf("main.tf")
    vre = _tf_block(main, 'resource "terraform_data" "vertex_reasoning_engine"')
    assert not re.search(r"when\s*=\s*destroy", vre)
    assert "destroy-stack" not in vre
    assert "terraform_data.stack_teardown" in vre  # destroyed before the teardown runs

    td = _tf_block(main, 'resource "terraform_data" "stack_teardown"')
    assert "when    = destroy" in td and "destroy-stack" in td
    triggers = td[td.index("triggers_replace = ["): td.index("\n  ]", td.index("triggers_replace = ["))]
    for forbidden in (
        "var.container_image",
        "var.master_prompt_sheet_id",
        "var.workspace_impersonate_user",
        "cctv_audit_worker",
    ):
        assert forbidden not in triggers, forbidden
    items = [line.strip().rstrip(",") for line in triggers.splitlines()[1:] if line.strip()]
    assert items == [
        "var.project_id",
        "var.reasoning_engine_location",
        "local.ge_engine_id",
        "google_storage_bucket.staging_bucket.name",
        "data.google_service_account.audit_worker_sa.email",
    ]
    for i, flag in enumerate(("--project-id", "--location")):
        assert f'{flag} "${{self.triggers_replace[{i}]}}"' in td
    assert '--ge-engine-id "${self.triggers_replace[2]}"' in td
    assert '--staging-bucket "${self.triggers_replace[3]}"' in td
    assert '--service-account "${self.triggers_replace[4]}"' in td
    # Exactly one destroy-time provisioner in the whole root, and only on stack_teardown.
    assert len(re.findall(r"when\s*=\s*destroy", main)) == 1
    worker = _tf_block(main, 'resource "google_cloud_run_v2_service" "cctv_audit_worker"')
    assert "terraform_data.stack_teardown" in worker


def test_new_app_falls_back_to_v2_datastore_while_store_is_being_deleted(monkeypatch):
    """Incident 2026-10-10: a just-deleted `<ge>-store` stays reserved for hours (400 'is being deleted')."""
    fake = _FakeDiscoveryEngine({})
    calls: list[tuple[str, str, Any]] = []

    def call(method, url, body=None, project_id="", ignore_errors=False):
        calls.append((method, url, body))
        if method == "GET" and "/dataStores/" in url:
            return {"_http_error": 404, "_error_body": "nf"}
        if method == "POST" and "dataStoreId=stack-a-ge-store" in url and not url.endswith("-v2"):
            return {"_http_error": 400, "_error_body": "DataStore ... is being deleted, please wait"}
        return fake(method, url, body, project_id, ignore_errors)

    monkeypatch.setattr(deploy, "_call", call)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    deploy.ensure_gemini_enterprise_and_bind_agent(
        project_id="my-project",
        reasoning_engine_name=f"{RE_BASE}/mine",
        ge_engine_id="stack-a-ge",
        ge_display_name="Stack A app",
        agent_display_name="Stack A agent",
    )
    ds_posts = [u for m, u, _ in calls if m == "POST" and "dataStoreId=" in u]
    assert ds_posts == [f"{DE}/dataStores?dataStoreId=stack-a-ge-store", f"{DE}/dataStores?dataStoreId=stack-a-ge-store-v2"]
    engine_body = next(b for m, u, b in calls if m == "POST" and "engines?engineId=stack-a-ge" in u)
    assert engine_body["dataStoreIds"] == ["stack-a-ge-store-v2"]


def test_existing_v2_datastore_is_reused(monkeypatch):
    fake = _FakeDiscoveryEngine({})
    calls: list[tuple[str, str, Any]] = []

    def call(method, url, body=None, project_id="", ignore_errors=False):
        calls.append((method, url, body))
        if method == "GET" and url.endswith("/dataStores/stack-a-ge-store"):
            return {"_http_error": 404, "_error_body": "nf"}
        return fake(method, url, body, project_id, ignore_errors)

    monkeypatch.setattr(deploy, "_call", call)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    deploy.ensure_gemini_enterprise_and_bind_agent(
        project_id="my-project",
        reasoning_engine_name=f"{RE_BASE}/mine",
        ge_engine_id="stack-a-ge",
        ge_display_name="Stack A app",
        agent_display_name="Stack A agent",
    )
    assert not any(m == "POST" and "dataStoreId=" in u for m, u, _ in calls)
    engine_body = next(b for m, u, b in calls if m == "POST" and "engines?engineId=stack-a-ge" in u)
    assert engine_body["dataStoreIds"] == ["stack-a-ge-store-v2"]


def test_failed_engine_creation_fails_the_deploy_after_binding_extra_apps(monkeypatch):
    mine = _adk_agent("m", "cctv-audit", "门店视频稽核", f"{RE_BASE}/mine")
    fake = _FakeDiscoveryEngine({"cctv-audit": [mine]})

    def call(method, url, body=None, project_id="", ignore_errors=False):
        if method == "POST" and "engines?engineId=stack-a-ge" in url:
            fake.calls.append((method, url, body))
            return {"_http_error": 409, "_error_body": "Engine is being deleted"}
        return fake(method, url, body, project_id, ignore_errors)

    monkeypatch.setattr(deploy, "_call", call)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    with pytest.raises(SystemExit, match="could not be created"):
        deploy.ensure_gemini_enterprise_and_bind_agent(
            project_id="my-project",
            reasoning_engine_name=f"{RE_BASE}/mine",
            ge_engine_id="stack-a-ge",
            ge_display_name="Stack A app",
            agent_display_name="Stack A agent",
            extra_engine_ids=["cctv-audit"],
        )
    # The extra app's agent was still bound; nothing was posted into the missing app.
    assert any(m == "PATCH" and "/agents/m" in u for m, u, _ in fake.calls)
    assert not any("/engines/stack-a-ge/assistants" in u for m, u, _ in fake.calls if m != "GET")


def test_destroy_stack_deletes_engine_datastores_including_v2(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_call(method, url, body=None, project_id="", ignore_errors=False):
        calls.append((method, url))
        if method == "GET" and url.endswith("/reasoningEngines"):
            return {"reasoningEngines": []}
        if method == "GET" and url.endswith("/agents"):
            return {"agents": []}
        if method == "GET" and url.endswith("/engines/stack-a-ge"):
            return {"name": url, "dataStoreIds": ["custom-store"]}
        return {}

    monkeypatch.setattr(deploy, "_call", fake_call)
    summary = deploy.destroy_stack(
        project_id="my-project", location="us-central1", service_account=OWN_SA, ge_engine_id="stack-a-ge"
    )
    deleted = [u for m, u in calls if m == "DELETE" and "/dataStores/" in u]
    assert [u.rsplit("/", 1)[1] for u in deleted] == ["custom-store", "stack-a-ge-store", "stack-a-ge-store-v2"]
    assert summary["deleted_ge_datastore"] is True


def test_agent_patch_never_sends_immutable_state(monkeypatch):
    """Discovery Engine rejects `state` in an agent PATCH updateMask (HTTP 400 immutable path)."""
    mine = _adk_agent("m", "stack-a-ge", "Stack A agent", f"{RE_BASE}/mine")
    fake = _FakeDiscoveryEngine({"stack-a-ge": [mine]})
    _bind(fake, monkeypatch)
    patch_url, patch_body = next((u, b) for m, u, b in fake.mutations() if m == "PATCH")
    mask = patch_url.split("updateMask=", 1)[1].split(",")
    assert "state" not in mask and "adkAgentDefinition" in mask
    assert "state" not in patch_body



def test_seed_object_never_overwrites_an_existing_object(tmp_path, monkeypatch, capsys):
    """Round 72b: the default SOP seed is a create-only upload (`ifGenerationMatch=0`)."""
    import io as _io
    import urllib.error as _ue

    src = tmp_path / "master_sheet.xlsx"
    src.write_bytes(b"PK-seed")
    seen: list[Any] = []

    class _Resp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen.append(req)
        if len(seen) == 1:
            return _Resp(b'{"generation": "123"}')
        raise _ue.HTTPError(req.full_url, 412, "Precondition Failed", {}, _io.BytesIO(b"{}"))

    monkeypatch.setattr(deploy, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake_urlopen)
    assert deploy.seed_gcs_object("bkt", "sop/master_sheet.xlsx", str(src), "application/x") == "created"
    assert deploy.seed_gcs_object("bkt", "sop/master_sheet.xlsx", str(src), "application/x") == "exists"
    assert all("ifGenerationMatch=0" in r.full_url for r in seen)
    assert "name=sop%2Fmaster_sheet.xlsx" in seen[0].full_url and seen[0].data == b"PK-seed"
    assert "left untouched" in capsys.readouterr().out

    def boom(req, timeout=0):
        raise _ue.HTTPError(req.full_url, 403, "Forbidden", {}, _io.BytesIO(b"denied"))

    monkeypatch.setattr(deploy.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit, match="HTTP 403"):
        deploy.seed_gcs_object("bkt", "sop/master_sheet.xlsx", str(src), "application/x")
