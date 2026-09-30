"""Tests for the Cloud Build deploy pipeline: deploy/ci_pipeline.py, its scripts and pinned inputs.

Hermetic: no network. The Dockerfile, cloudbuild.yaml, lock files and scripts checked here are the
real ones from this directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import zipfile
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

IAC_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ci_pipeline", IAC_DIR / "deploy" / "ci_pipeline.py")
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)

REPO = "asia-southeast1-docker.pkg.dev/proj-1/cctv-audit/cctv-audit-worker"
DIGEST = "sha256:" + "ab" * 32
_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel, text in {
        "Dockerfile": "FROM python:3.12-slim\nCOPY requirements.txt .\nCOPY cctv_audit/ ./cctv_audit/\n",
        "requirements.txt": "fastapi>=0.110\n",
        "cctv_audit/server.py": "app = 1\n",
        "cctv_audit/sub/util.py": "X = 1\n",
        "tests/test_x.py": "def test(): pass\n",
        "main.tf": "# infra\n",
        "study.tfvars": 'project_id = "p"\n',
    }.items():
        _write(tmp_path, rel, text)
    return tmp_path


# ------------------------------------------------------------------ source hash


def test_hash_ignores_everything_the_image_does_not_contain(tree: Path) -> None:
    before = ci.source_hash(tree)
    _write(tree, "main.tf", "# changed infra\n")
    _write(tree, "study.tfvars", 'project_id = "p2"\n')
    _write(tree, "tests/test_x.py", "def test(): assert 1\n")
    _write(tree, "cctv_audit/__pycache__/server.cpython-312.pyc", "bytecode")
    _write(tree, "cctv_audit/debug.log", "noise")
    assert ci.source_hash(tree) == before  # tfvars-only commit -> same tag -> no redeploy


@pytest.mark.parametrize(
    "rel, text",
    [
        ("cctv_audit/server.py", "app = 2\n"),
        ("cctv_audit/sub/util.py", "X = 2\n"),
        ("cctv_audit/new_module.py", "Y = 1\n"),
        ("requirements.txt", "fastapi>=0.111\n"),
        ("Dockerfile", "FROM python:3.13-slim\nCOPY requirements.txt .\nCOPY cctv_audit/ ./cctv_audit/\n"),
    ],
)
def test_hash_changes_for_every_image_input(tree: Path, rel: str, text: str) -> None:
    before = ci.source_hash(tree)
    _write(tree, rel, text)
    assert ci.source_hash(tree) != before


def test_hash_depends_on_path_not_only_content(tree: Path) -> None:
    before = ci.source_hash(tree)
    (tree / "cctv_audit/sub/util.py").rename(tree / "cctv_audit/util.py")
    assert ci.source_hash(tree) != before


def test_missing_image_input_is_an_error(tree: Path) -> None:
    (tree / "requirements.txt").unlink()
    with pytest.raises(ci.PipelineError):
        ci.source_hash(tree)


def test_real_tree_hash_is_stable_and_well_formed() -> None:
    tag = ci.image_tag(IAC_DIR)
    assert tag == ci.image_tag(IAC_DIR)
    assert tag.startswith("src-") and len(tag) == 4 + 64
    rels = {f.relative_to(IAC_DIR).as_posix() for f in ci.image_input_files(IAC_DIR)}
    assert {"Dockerfile", "requirements.txt", "cctv_audit/server.py"} <= rels
    assert not any(r.endswith(".pyc") or "__pycache__" in r for r in rels)


# ------------------------------------------------------------------ Dockerfile parsing
#
# The image tag must hash exactly the build-context paths the Dockerfile reads. These helpers find
# them the way the Dockerfile frontend does: continuations joined, comments dropped, instructions
# case-insensitive, and sources that do not come from the build context skipped.

_HEREDOC = re.compile(r"<<-?([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")
_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@)")


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs. Heredoc bodies are skipped, not parsed as instructions."""
    instructions: list[tuple[str, str]] = []
    pending: list[str] = []
    heredoc_ends: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if heredoc_ends:
            if stripped == heredoc_ends[0]:
                heredoc_ends.pop(0)
            continue
        if stripped.startswith("#") or (not stripped and not pending):
            continue  # comments (also inside a continuation) and parser directives
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        full = " ".join([*pending, stripped]).strip()
        pending = []
        if not full:
            continue
        word, _, rest = full.partition(" ")
        instructions.append((word.upper(), rest.strip()))
        heredoc_ends.extend(match.group(2) for match in _HEREDOC.finditer(rest))
    return instructions


def _split_flags(args: str) -> tuple[list[str], str]:
    """Leading --flags of an instruction, and the rest."""
    flags: list[str] = []
    rest = args.strip()
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        flags.append(flag)
        rest = rest.strip()
    return flags, rest


def _dockerfile_context_sources(text: str) -> list[str]:
    """Build-context paths read by COPY/ADD and by RUN --mount=type=bind (default source: all of it).

    Not from the build context, so skipped: --from=<stage|image>, ADD <url|git>, heredoc sources.
    """
    sources: list[str] = []
    for instruction, args in _dockerfile_instructions(text):
        flags, rest = _split_flags(args)
        if instruction in ("COPY", "ADD"):
            if any(flag.startswith("--from=") for flag in flags):
                continue
            paths = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
            sources.extend(p for p in paths[:-1] if not p.startswith("<<") and not _URL.match(p))
        elif instruction == "RUN":
            for flag in flags:
                if not flag.startswith("--mount="):
                    continue
                opts = dict(
                    kv.split("=", 1) if "=" in kv else (kv, "")
                    for kv in flag.removeprefix("--mount=").split(",")
                )
                if opts.get("type", "bind") == "bind" and "from" not in opts:
                    sources.append(opts.get("source", opts.get("src", ".")))
    return sources


@pytest.mark.parametrize(
    "dockerfile, expected",
    [
        ("FROM a\nCOPY requirements.txt .\n", ["requirements.txt"]),
        # multi-stage: files taken from another stage or image are not build-context inputs
        (
            "FROM a AS builder\nCOPY src/ /src/\nFROM b\n"
            "COPY --from=builder /app /dest\nCOPY --from=python:3.12 /x /y\n",
            ["src/"],
        ),
        # continuations, several flags, lowercase instruction, comment inside the continuation
        (
            "FROM a\ncopy --chown=1000:1000 --chmod=644 \\\n  # a comment\n  a.txt \\\n  b.txt /dst/\n",
            ["a.txt", "b.txt"],
        ),
        ('FROM a\nCOPY --link ["dir with space/", "/dst/"]\n', ["dir with space/"]),
        (
            "FROM a\nADD https://example.com/x.tgz /x\nADD git@github.com:o/r.git /r\nADD local.tgz /l\n",
            ["local.tgz"],
        ),
        # heredoc sources are inline text; the body is not an instruction
        ("FROM a\nCOPY <<EOF /etc/conf\nCOPY fake /x\nEOF\nCOPY real.cfg /etc/\n", ["real.cfg"]),
        ("FROM a\nRUN --mount=type=bind,source=req.txt,target=/r pip install -r /r\n", ["req.txt"]),
        ("FROM a\nRUN --mount=target=/src make\n", ["."]),
        ("FROM a\nRUN --mount=type=cache,target=/root/.cache pip install x\n", []),
        ("FROM a AS b\nFROM c\nRUN --mount=type=bind,from=b,source=/o,target=/i true\n", []),
    ],
)
def test_dockerfile_context_parser(dockerfile: str, expected: list[str]) -> None:
    assert _dockerfile_context_sources(dockerfile) == expected


def test_image_tag_hashes_exactly_what_the_real_dockerfile_reads() -> None:
    read = {
        os.path.normpath(s.rstrip("/"))
        for s in _dockerfile_context_sources((IAC_DIR / "Dockerfile").read_text())
    }
    assert read, "parser found no COPY lines; the check would be vacuous"
    hashed = set(ci.IMAGE_INPUTS) - {"Dockerfile"}  # the Dockerfile defines the image, it is not copied
    assert read <= hashed, (
        f"Dockerfile reads {sorted(read - hashed)} but IMAGE_INPUTS does not hash it: "
        "a change there would not produce a new image tag"
    )
    assert hashed <= read, (
        f"IMAGE_INPUTS hashes {sorted(hashed - read)} which the image does not contain: "
        "changing it would rebuild and redeploy for nothing"
    )


# ------------------------------------------------------------------ pinned inputs


def test_every_dockerfile_base_image_is_pinned_by_digest() -> None:
    stages: set[str] = set()
    bases: list[str] = []
    for instruction, args in _dockerfile_instructions((IAC_DIR / "Dockerfile").read_text()):
        if instruction != "FROM":
            continue
        _, rest = _split_flags(args)
        words = rest.split()
        if words[0].lower() not in stages and words[0] != "scratch":
            bases.append(words[0])
        if len(words) >= 3 and words[1].upper() == "AS":
            stages.add(words[2].lower())
    assert bases
    for image in bases:
        assert _PINNED.search(image), f"Dockerfile base {image!r} is not pinned by digest"


def _cloudbuild() -> dict:
    return yaml.safe_load((IAC_DIR / "cloudbuild.yaml").read_text())


def test_every_pipeline_step_image_is_pinned_by_digest() -> None:
    names = [step["name"] for step in _cloudbuild()["steps"]]
    assert names
    for name in names:
        assert _PINNED.search(name), f"cloudbuild.yaml step image {name!r} is not pinned by digest"


def test_image_and_pipeline_share_one_python_base() -> None:
    base = next(a for i, a in _dockerfile_instructions((IAC_DIR / "Dockerfile").read_text()) if i == "FROM")
    base_digest = base.split()[0].rsplit("@", 1)[1]
    python_steps = [s["name"] for s in _cloudbuild()["steps"] if s["name"].startswith("python@")]
    assert python_steps
    assert {n.rsplit("@", 1)[1] for n in python_steps} == {base_digest}, "bump both together"


# ------------------------------------------------------------------ dependency locks


def _parse_lock(path: Path) -> dict[str, tuple[str, list[str]]]:
    """name -> (version, sha256 hashes) of a `uv pip compile --generate-hashes` lock."""
    entries: list[str] = []
    pending: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        entries.append(" ".join([*pending, stripped]))
        pending = []
    assert not pending, f"{path.name}: dangling line continuation"

    pins: dict[str, tuple[str, list[str]]] = {}
    for entry in entries:
        requirement, *hash_parts = entry.split(" --hash=")
        assert not requirement.startswith("-"), f"{path.name}: unexpected option line {entry!r}"
        req = Requirement(requirement)
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"{path.name}: {requirement!r} is not pinned with =="
        hashes = [h.strip() for h in hash_parts]
        assert hashes and all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in hashes), (
            f"{path.name}: {req.name} needs sha256 hashes for pip --require-hashes"
        )
        pins[canonicalize_name(req.name)] = (specs[0].version, hashes)
    return pins


def _compile_command(path: Path) -> str:
    lines = [line.lstrip("#").strip() for line in path.read_text().splitlines() if "uv pip compile" in line]
    assert len(lines) == 1, f"{path.name} must state exactly one regeneration command"
    return lines[0]


@pytest.mark.parametrize(
    "in_file, lock_file",
    [
        ("requirements.in", "requirements.txt"),
        ("deploy/requirements-deploy.in", "deploy/requirements-deploy.txt"),
    ],
)
def test_lock_matches_its_input(in_file: str, lock_file: str) -> None:
    """Every input requirement is pinned, inside its range, with hashes; the lock came from this input.

    Extras' own dependencies (uvicorn[standard]) are not re-resolved here: uv did that when compiling,
    and pip --require-hashes refuses to build an image from a lock missing any dependency.
    """
    lock = _parse_lock(IAC_DIR / lock_file)
    requirements = [
        Requirement(line.split("#", 1)[0].strip())
        for line in (IAC_DIR / in_file).read_text().splitlines()
        if line.split("#", 1)[0].strip()
    ]
    assert requirements and lock
    for req in requirements:
        name = canonicalize_name(req.name)
        assert name in lock, f"{in_file} requires {req} but {lock_file} does not pin it: regenerate the lock"
        version, _ = lock[name]
        assert req.specifier.contains(version, prereleases=True), (
            f"{lock_file} pins {name}=={version}, outside {req} from {in_file}: regenerate the lock"
        )
    command = _compile_command(IAC_DIR / in_file)
    assert command == _compile_command(IAC_DIR / lock_file), f"{lock_file} was not generated by the command {in_file} documents"
    assert f" {in_file} " in f" {command} " and f"-o {lock_file}" in command


# ------------------------------------------------------------------ scripts


def _shell_logical_lines(path: Path) -> list[str]:
    lines: list[str] = []
    pending: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        lines.append(" ".join([*pending, stripped]))
        pending = []
    return [line for line in lines if line and not line.startswith("#")]


def test_image_build_installs_only_the_hashed_lock() -> None:
    runs = [a for i, a in _dockerfile_instructions((IAC_DIR / "Dockerfile").read_text()) if i == "RUN"]
    pip_runs = [cmd for cmd in runs if "pip install" in cmd]
    assert pip_runs
    for cmd in pip_runs:
        words = cmd.split()
        assert "--require-hashes" in words and "requirements.txt" in words, cmd


def test_deploy_step_installs_only_the_hashed_lock() -> None:
    pip_lines = [line for line in _shell_logical_lines(IAC_DIR / "deploy/ci_terraform.sh") if "pip install" in line]
    assert pip_lines
    for line in pip_lines:
        words = line.split()
        assert "--require-hashes" in words and "deploy/requirements-deploy.txt" in words, line


def test_terraform_step_checks_identity_then_pins_the_digest_before_terraform() -> None:
    lines = _shell_logical_lines(IAC_DIR / "deploy/ci_terraform.sh")

    def first(pred) -> int:
        return next(i for i, line in enumerate(lines) if pred(line))

    identity = first(lambda l: "ci_pipeline.py check-identity" in l)
    pin = first(lambda l: "ci_pipeline.py pin" in l)
    read_ref = first(lambda l: "image_ref.txt" in l)
    pip = first(lambda l: "pip install" in l)
    init = first(lambda l: l.startswith("terraform init"))
    assert identity < pin < read_ref < init  # the deployed digest always comes from pin
    assert identity < pip  # nothing from PyPI runs before the identity is confirmed


# ------------------------------------------------------------------ IAM split: bootstrap vs pipeline
#
# The deployer SA applies ../main.tf unattended, so it must not be able to widen anyone's
# permissions. Everything that changes project or service-account IAM lives in bootstrap/ and is
# applied by a human.

# Roles that change who holds which permission, or let the holder act as / mint tokens for others.
_IAM_POWER_ROLES = {
    "roles/owner",
    "roles/editor",
    "roles/resourcemanager.projectIamAdmin",
    "roles/iam.securityAdmin",
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/iam.serviceAccountTokenCreator",
    "roles/iam.serviceAccountKeyAdmin",
    "roles/iam.workloadIdentityUser",
}
_BOOTSTRAP = IAC_DIR / "bootstrap" / "main.tf"


def _hcl_blocks(text: str, kind: str, type_: str) -> list[str]:
    """Every `<kind> "<type_>" "<name>" { ... }` block, brace-matched."""
    blocks = []
    for match in re.finditer(rf'^{kind}\s+"{re.escape(type_)}"\s+"[^"]+"\s*\{{', text, re.M):
        depth = 0
        for i in range(match.end() - 1, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                blocks.append(text[match.start() : i + 1])
                break
    return blocks


def test_deployer_holds_no_role_that_can_change_iam() -> None:
    boot = _BOOTSTRAP.read_text()
    roles_list = re.search(r"deployer_project_roles\s*=\s*toset\(\[(.*?)\]\)", boot, re.S)
    assert roles_list, "deployer_project_roles not found in bootstrap/main.tf"
    roles = set(re.findall(r'"(roles/[^"]+)"', roles_list.group(1)))
    assert roles and not roles & _IAM_POWER_ROLES, f"deployer could change IAM via {sorted(roles & _IAM_POWER_ROLES)}"

    deployer = "google_service_account.deployer.email"
    project_grants = [b for b in _hcl_blocks(boot, "resource", "google_project_iam_member") if deployer in b]
    assert len(project_grants) == 1 and "for_each = local.deployer_project_roles" in project_grants[0], (
        "every project-level grant to the deployer must come from deployer_project_roles"
    )
    sa_grants = [b for b in _hcl_blocks(boot, "resource", "google_service_account_iam_member") if deployer in b]
    assert len(sa_grants) == 1, "the deployer's only service-account grant is actAs on the worker"
    assert re.search(r"service_account_id\s*=\s*google_service_account\.worker\.name\s", sa_grants[0])
    assert re.search(r'role\s*=\s*"roles/iam\.serviceAccountUser"', sa_grants[0])


def test_pipeline_stack_changes_no_project_or_service_account_iam() -> None:
    """../main.tf may only grant resource-level roles the deployer holds (bucket, Cloud Run, repo)."""
    types = set(re.findall(r'^resource\s+"([^"]+)"', (IAC_DIR / "main.tf").read_text(), re.M))
    offending = sorted(
        t
        for t in types
        if t.startswith(("google_project_iam_", "google_service_account", "google_folder_iam_", "google_organization_iam_"))
    )
    assert not offending, f"main.tf manages {offending}; the deployer cannot apply that (move it to bootstrap/)"


# ------------------------------------------------------------------ resolve / pin


class FakeRegistry:
    def __init__(self, status: int, body: str = "") -> None:
        self.status, self.body, self.urls = status, body, []

    def __call__(self, url: str) -> tuple[int, str]:
        self.urls.append(url)
        return self.status, self.body


def _tag_body(digest: str = DIGEST) -> str:
    return json.dumps(
        {"version": f"projects/proj-1/locations/asia-southeast1/repositories/cctv-audit/packages/cctv-audit-worker/versions/{digest}"}
    )


def _no_sleep(_: float) -> None:
    pytest.fail("must not wait")


def test_existing_tag_resolves_to_digest_via_the_right_api_path() -> None:
    fetch = FakeRegistry(200, _tag_body())
    assert ci.existing_digest_ref(REPO, "src-abc", fetch) == f"{REPO}@{DIGEST}"
    assert fetch.urls == [
        "https://artifactregistry.googleapis.com/v1/projects/proj-1/locations/asia-southeast1/"
        "repositories/cctv-audit/packages/cctv-audit-worker/tags/src-abc"
    ]


def test_only_404_means_not_built() -> None:
    assert ci.existing_digest_ref(REPO, "src-abc", FakeRegistry(404)) is None
    for status in (401, 403, 429, 500):
        with pytest.raises(ci.PipelineError):
            ci.existing_digest_ref(REPO, "src-abc", FakeRegistry(status, "boom"))


def test_malformed_tag_version_is_rejected() -> None:
    with pytest.raises(ci.PipelineError):
        ci.existing_digest_ref(REPO, "src-abc", FakeRegistry(200, _tag_body("sha256:short")))


def test_non_registry_image_path_is_rejected() -> None:
    with pytest.raises(ci.PipelineError):
        ci.existing_digest_ref("docker.io/library/python", "src-abc", FakeRegistry(404))


def test_resolve_writes_exactly_one_decision_and_clears_stale_ones(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "ci-out"
    tag = ci.image_tag(tree)

    ci.resolve(tree, REPO, out, FakeRegistry(404))
    assert (out / "image_tag.txt").read_text().strip() == f"{REPO}:{tag}"
    assert not (out / "image_ref.txt").exists()

    ci.resolve(tree, REPO, out, FakeRegistry(200, _tag_body()))
    assert (out / "image_ref.txt").read_text().strip() == f"{REPO}@{DIGEST}"
    assert not (out / "image_tag.txt").exists()


def test_pin_writes_the_registry_digest_for_this_tree(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "ci-out"
    out.mkdir()
    (out / "image_ref.txt").write_text(f"{REPO}@sha256:{'cd' * 32}\n")  # left over from resolve
    fetch = FakeRegistry(200, _tag_body())
    ci.pin(tree, REPO, out, fetch, sleep=_no_sleep)
    assert (out / "image_ref.txt").read_text().strip() == f"{REPO}@{DIGEST}"
    assert fetch.urls == [
        "https://artifactregistry.googleapis.com/v1/projects/proj-1/locations/asia-southeast1/"
        f"repositories/cctv-audit/packages/cctv-audit-worker/tags/{ci.image_tag(tree)}"
    ]


def test_pin_fails_if_the_tag_was_never_pushed_and_leaves_no_ref(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "ci-out"
    out.mkdir()
    (out / "image_ref.txt").write_text(f"{REPO}@{DIGEST}\n")  # stale: must never reach terraform
    fetch, sleeps = FakeRegistry(404), []
    with pytest.raises(ci.PipelineError):
        ci.pin(tree, REPO, out, fetch, attempts=3, sleep=sleeps.append)
    assert len(fetch.urls) == 3 and sleeps == [5.0, 5.0]
    assert not (out / "image_ref.txt").exists()


def test_pin_waits_for_a_fresh_push_to_become_visible(tree: Path, tmp_path: Path) -> None:
    responses = iter([(404, ""), (200, _tag_body())])
    calls: list[str] = []

    def fetch(url: str) -> tuple[int, str]:
        calls.append(url)
        return next(responses)

    ci.pin(tree, REPO, tmp_path / "ci-out", fetch, sleep=lambda _: None)
    assert (tmp_path / "ci-out" / "image_ref.txt").read_text().strip() == f"{REPO}@{DIGEST}"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 500])
def test_pin_stops_at_once_on_real_errors(tree: Path, tmp_path: Path, status: int) -> None:
    fetch = FakeRegistry(status, "boom")
    with pytest.raises(ci.PipelineError):
        ci.pin(tree, REPO, tmp_path / "ci-out", fetch, sleep=_no_sleep)
    assert len(fetch.urls) == 1


# ------------------------------------------------------------------ identity + terraform


def test_check_identity_rejects_any_other_service_account() -> None:
    expected = "chagee-cctv-deployer@proj-1.iam.gserviceaccount.com"
    assert ci.check_identity(expected, lambda: expected + "\n") == expected
    with pytest.raises(ci.PipelineError):
        ci.check_identity(expected, lambda: "123-compute@developer.gserviceaccount.com")


def _zip_with_terraform(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("terraform", payload)
        zf.writestr("LICENSE.txt", "license")
    return buf.getvalue()


def test_install_terraform_verifies_checksum_before_writing(tmp_path: Path) -> None:
    archive = _zip_with_terraform(b"\x7fELF fake binary")
    good = hashlib.sha256(archive).hexdigest()
    urls: list[str] = []

    def download(url: str) -> bytes:
        urls.append(url)
        return archive

    bad_dest = tmp_path / "bad"
    with pytest.raises(ci.PipelineError):
        ci.install_terraform("1.9.5", "0" * 64, bad_dest, download)
    assert not bad_dest.exists()

    dest = ci.install_terraform("1.9.5", good, tmp_path / "terraform", download)
    assert dest.read_bytes() == b"\x7fELF fake binary"
    assert os.access(dest, os.X_OK)
    assert urls[-1] == "https://releases.hashicorp.com/terraform/1.9.5/terraform_1.9.5_linux_amd64.zip"
