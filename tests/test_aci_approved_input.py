"""Exercise the shared ACI wrapper with fake Docker and bounded copy I/O."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

import test_ci_evidence as evidence_fixture

ROOT = evidence_fixture.ROOT


class ACIApprovedInput(unittest.TestCase):
    setUp = evidence_fixture.EvidenceCLI.setUp
    command = evidence_fixture.EvidenceCLI.command
    cli = evidence_fixture.EvidenceCLI.cli
    git = evidence_fixture.EvidenceCLI.git
    prepare = evidence_fixture.EvidenceCLI.prepare

    def project(self, *, prepared=True, expected=True):
        project = self.root / "aci"
        docker = project / "docker/travis"
        docker.mkdir(parents=True)
        (project / "dist-static").mkdir()
        for name in ("host", "controller", "cnideploy", "operator", "openvswitch", "webhook", "certmanager"):
            image = "ubi-minimal" if name in ("host", "openvswitch") else "ubi"
            (docker / ("Dockerfile-" + name)).write_text(
                f"ARG UBI=registry.access.redhat.com/ubi9/{image}:latest\nFROM ${{UBI}}\n")
        copy_script = project / "docker/copy_iptables.sh"
        copy_script.write_text("#!/usr/bin/env python3\nimport json,os,pathlib,sys\n"
                               "pathlib.Path(os.environ['COPY_INPUT_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
                               "sys.exit(77)\n")
        copy_script.chmod(0o755)
        self.git("init", str(project))
        self.git("add", ".", cwd=project)
        self.git("commit", "-m", "ACI fixture", cwd=project)
        artifacts = self.root / "aci-artifacts"
        env = dict(self.env, GITHUB_ACTIONS="true", GHA_TRIGGER_TAG="6.1.1.8", GHA_DATE_TAG="111226",
                   GITHUB_REPOSITORY="noironetworks/aci-containers", GITHUB_REF="refs/tags/6.1.1.8",
                   TRAVIS_TAG="6.1.1.8", UPSTREAM_ID="42", TRAVIS_BUILD_NUMBER="123",
                   CI_ARTIFACT_DIR=str(artifacts), COPY_INPUT_LOG=str(self.root / "copy-input.json"),
                   QUAY_NOIRO_DOCKER_CONFIG=str(self.root / "quay"))
        env.pop("CI_SCAN_POLICY", None)
        env.pop("EXPECTED_OPFLEX_BASE_DIGEST", None)
        env.pop("CI_EVIDENCE_MODE", None)
        if prepared:
            env["CI_EVIDENCE_MODE"] = "prepared-v1"
        if expected:
            env["EXPECTED_OPFLEX_BASE_DIGEST"] = "sha256:" + "a" * 64
        return project, artifacts, env

    def wrapper(self, project, env):
        return self.command(["bash", str(ROOT / "travis/build-push-aci-containers-images.sh")],
                            cwd=project, env=env, check=False)

    def test_prepared_wrapper_copies_only_the_locked_input_when_z_is_different(self):
        project, artifacts, env = self.project()
        result = self.wrapper(project, env)
        self.assertEqual(result.returncode, 77, result.stderr)
        digest = env["EXPECTED_OPFLEX_BASE_DIGEST"]
        reference = "quay.io/noirolabs/opflex-build-base@" + digest
        self.assertEqual(json.loads((self.root / "copy-input.json").read_text()), [reference, "dist-static"])
        proof = json.loads((artifacts / "base-provenance/aci-iptables-base.json").read_text())
        self.assertEqual(proof["reference"], reference)
        self.assertEqual(proof["digest"], digest)
        self.assertEqual(proof["image_id"], "sha256:" + "e" * 64)
        self.assertEqual(proof["role"], "opflex-build-base")
        self.assertTrue(proof["immutable"])
        self.assertTrue(proof["captured_before_build"])
        mutable = "quay.io/noirolabs/opflex-build-base:6.1.1.8.42.z"
        self.assertNotEqual("sha256:" + hashlib.sha256(mutable.encode()).hexdigest(), digest)
        calls = [json.loads(line) for line in (self.root / "tools.jsonl").read_text().splitlines()]
        self.assertFalse(any(call["tool"] == "docker" and mutable in call["args"] for call in calls))

    def test_legacy_wrapper_retains_tag_input_and_prepared_requires_digest(self):
        project, artifacts, env = self.project(prepared=False, expected=False)
        result = self.wrapper(project, env)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertEqual(json.loads((self.root / "copy-input.json").read_text())[0],
                         "quay.io/noirolabs/opflex-build-base:6.1.1.8.42.z")
        self.assertFalse((artifacts / "base-provenance/aci-iptables-base.json").exists())
        (self.root / "copy-input.json").unlink()
        env["CI_EVIDENCE_MODE"] = "prepared-v1"
        result = self.wrapper(project, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "copy-input.json").exists())

    def test_actual_aci_producers_emit_and_deduplicate_the_verified_input(self):
        project, artifacts, env = self.project()
        self.assertEqual(self.wrapper(project, env).returncode, 77)
        for image in ("aci-containers-host", "aci-containers-controller"):
            self.command(["bash", str(ROOT / "travis/push-images.sh"), "quay.io/noirolabs", image,
                          "6.1.1.8.42", "6.1.1.8.42,6.1.1.8.42.111226.123",
                          "registry.access.redhat.com/ubi9/ubi:latest"], env=env)
        metadata = artifacts / "image-metadata.jsonl"
        rows = [json.loads(line) for line in metadata.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row["build_inputs"]) == 1 for row in rows))
        self.assertEqual(rows[0]["build_inputs"], rows[1]["build_inputs"])
        bundle = self.prepare(metadata)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(len(manifest["build_inputs"]), 1)
        self.assertEqual(manifest["build_inputs"][0]["digest"], env["EXPECTED_OPFLEX_BASE_DIGEST"])
        self.assertTrue(any(item["path"].endswith("-build-input-provenance.txt") for item in manifest["files"]))
