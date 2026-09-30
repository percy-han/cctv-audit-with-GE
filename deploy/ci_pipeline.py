"""Helpers for the deploy pipeline (cloudbuild.yaml). Standard library only.

Subcommands:
  hash               print the image tag for the source tree (src-<sha256>)
  resolve            decide whether the worker image for this tree already exists
  pin                write the digest Artifact Registry holds for this tree's tag (must exist)
  check-identity     fail unless the build runs as the expected service account
  install-terraform  download a pinned Terraform release and verify its SHA256

Why the image tag is a hash of the source:
  The tag is computed over exactly what the Dockerfile copies (IMAGE_INPUTS). The same tree
  always gives the same tag, so a commit that only changes tfvars reuses the existing image,
  Terraform sees the same digest, and Cloud Run / the ReasoningEngine are not redeployed. Any code
  change gives a new tag, a new digest, and a rollout. Terraform only ever receives the digest,
  and that digest is read from Artifact Registry (pin), never from a builder's local image store.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

# Everything the Dockerfile COPYs. tests/test_ci_pipeline.py fails if the Dockerfile gains a COPY
# that is not listed here (the hash would then miss real image changes).
IMAGE_INPUTS = ("Dockerfile", "requirements.txt", "cctv_audit")

# Never present in the Cloud Build tree (.gcloudignore includes .gitignore); skipped locally so a
# developer's stray caches do not change the tag.
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd", ".log")

_METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}

Fetch = Callable[[str], tuple[int, str]]


class PipelineError(RuntimeError):
    """A condition under which the pipeline must stop instead of guessing."""


# --------------------------------------------------------------------------- source hash


def image_input_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in IMAGE_INPUTS:
        path = root / rel
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                files.extend(
                    Path(dirpath) / name for name in filenames if not name.endswith(_SKIP_SUFFIXES)
                )
        else:
            raise PipelineError(f"image input {rel!r} not found under {root}")
    return sorted(files, key=lambda f: f.relative_to(root).as_posix())


def source_hash(root: Path) -> str:
    """sha256 over (relative path, content sha256) of every image input, in sorted path order."""
    digest = hashlib.sha256()
    for file in image_input_files(root):
        digest.update(file.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def image_tag(root: Path) -> str:
    return f"src-{source_hash(root)}"


# --------------------------------------------------------------------------- HTTP helpers


def _metadata(path: str) -> str:
    req = urllib.request.Request(f"{_METADATA}/{path}", headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        raise PipelineError(
            f"metadata server unreachable ({exc}); this subcommand only runs inside Cloud Build"
        ) from exc


def _authorized_get(url: str) -> tuple[int, str]:
    """GET with the build identity's token; retries 429/5xx and network errors with backoff."""
    token = json.loads(_metadata("token"))["access_token"]
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    attempts, delay = 4, 2.0
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP or attempt == attempts:
                return exc.code, exc.read().decode(errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            if attempt == attempts:
                raise PipelineError(f"GET {url} failed after {attempts} attempts: {exc}") from exc
        time.sleep(delay)
        delay *= 2
    raise PipelineError(f"GET {url}: no attempt made")


# --------------------------------------------------------------------------- resolve


def _split_image_repo(image_repo: str) -> tuple[str, str, str, str]:
    """<location>-docker.pkg.dev/<project>/<repo>/<image> -> (location, project, repo, image)."""
    match = re.fullmatch(r"([a-z0-9-]+)-docker\.pkg\.dev/([^/]+)/([^/]+)/([^@:]+)", image_repo)
    if not match:
        raise PipelineError(f"not an Artifact Registry image path: {image_repo!r}")
    return match.group(1), match.group(2), match.group(3), match.group(4)


def existing_digest_ref(image_repo: str, tag: str, fetch: Fetch = _authorized_get) -> str | None:
    """Digest reference of image_repo:tag, None if the tag does not exist.

    Only a 404 means "absent". Every other failure raises: rebuilding on a transient error would
    produce a different digest for the same source and trigger a pointless redeploy.
    """
    location, project, repo, image = _split_image_repo(image_repo)
    url = (
        "https://artifactregistry.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/repositories/{repo}/"
        f"packages/{urllib.parse.quote(image, safe='')}/tags/{urllib.parse.quote(tag, safe='')}"
    )
    status, body = fetch(url)
    if status == 404:
        return None
    if status != 200:
        raise PipelineError(f"Artifact Registry HTTP {status} for {url}: {body[:500]}")
    version = json.loads(body).get("version", "")
    digest = urllib.parse.unquote(version.rsplit("/versions/", 1)[-1])
    if not _DIGEST_RE.fullmatch(digest):
        raise PipelineError(f"tag {tag} points to unexpected version {version!r}")
    return f"{image_repo}@{digest}"


def resolve(root: Path, image_repo: str, out_dir: Path, fetch: Fetch = _authorized_get) -> str:
    """Write exactly one of out_dir/image_ref.txt (reuse) or out_dir/image_tag.txt (build)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_file, tag_file = out_dir / "image_ref.txt", out_dir / "image_tag.txt"
    ref_file.unlink(missing_ok=True)
    tag_file.unlink(missing_ok=True)

    tag = image_tag(root)
    ref = existing_digest_ref(image_repo, tag, fetch)
    if ref:
        ref_file.write_text(ref + "\n")
        return f"reuse {ref} (tag {tag})"
    tag_file.write_text(f"{image_repo}:{tag}\n")
    return f"build {image_repo}:{tag}"


def pin(
    root: Path,
    image_repo: str,
    out_dir: Path,
    fetch: Fetch = _authorized_get,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Write out_dir/image_ref.txt = the digest Artifact Registry holds for this tree's tag.

    Runs right before terraform, whatever resolve decided. The tag must exist by now (it was just
    pushed, or it already existed); if it does not, the build step failed to push it and deploying
    any other image would be wrong, so this raises. A few seconds of retries on 404 only cover the
    registry making a fresh push visible to its API.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_file = out_dir / "image_ref.txt"
    ref_file.unlink(missing_ok=True)

    tag = image_tag(root)
    for attempt in range(1, attempts + 1):
        ref = existing_digest_ref(image_repo, tag, fetch)
        if ref:
            ref_file.write_text(ref + "\n")
            return f"deploy {ref} (tag {tag})"
        if attempt < attempts:
            sleep(5.0)
    raise PipelineError(
        f"{image_repo}:{tag} is not in Artifact Registry after {attempts} checks; "
        "the build-image step did not push the image for this source tree"
    )


# --------------------------------------------------------------------------- identity


def check_identity(expected: str, whoami: Callable[[], str] = lambda: _metadata("email")) -> str:
    actual = whoami().strip()
    if actual != expected:
        raise PipelineError(
            f"build runs as {actual!r}, expected {expected!r}. Start it with "
            f"--service-account=projects/<project>/serviceAccounts/{expected} (or via the trigger)."
        )
    return actual


# --------------------------------------------------------------------------- terraform


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def install_terraform(
    version: str, sha256: str, dest: Path, download: Callable[[str], bytes] = _download
) -> Path:
    url = f"https://releases.hashicorp.com/terraform/{version}/terraform_{version}_linux_amd64.zip"
    archive = download(url)
    actual = hashlib.sha256(archive).hexdigest()
    if actual != sha256.lower():
        raise PipelineError(f"{url}: sha256 {actual} != pinned {sha256}")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        dest.write_bytes(zf.read("terraform"))
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."), help="directory holding the Dockerfile")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash")
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--image-repo", required=True)
    p_resolve.add_argument("--out-dir", type=Path, required=True)
    p_pin = sub.add_parser("pin")
    p_pin.add_argument("--image-repo", required=True)
    p_pin.add_argument("--out-dir", type=Path, required=True)
    p_ident = sub.add_parser("check-identity")
    p_ident.add_argument("--expected", required=True)
    p_tf = sub.add_parser("install-terraform")
    p_tf.add_argument("--version", required=True)
    p_tf.add_argument("--sha256", required=True)
    p_tf.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "hash":
            print(image_tag(args.root))
        elif args.cmd == "resolve":
            print(resolve(args.root, args.image_repo, args.out_dir))
        elif args.cmd == "pin":
            print(pin(args.root, args.image_repo, args.out_dir))
        elif args.cmd == "check-identity":
            print(f"running as {check_identity(args.expected)}")
        elif args.cmd == "install-terraform":
            print(f"installed {install_terraform(args.version, args.sha256, args.dest)}")
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
