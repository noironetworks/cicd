import json
import os
import unittest

import test_ci_evidence as fixture


class EvidenceHardening(unittest.TestCase):
    setUp = fixture.EvidenceCLI.setUp
    command = fixture.EvidenceCLI.command
    cli = fixture.EvidenceCLI.cli
    git = fixture.EvidenceCLI.git
    remote = fixture.EvidenceCLI.remote
    images = fixture.EvidenceCLI.images
    prepare = fixture.EvidenceCLI.prepare
    publish = fixture.EvidenceCLI.publish

    def test_every_registry_tag_must_match_recorded_built_image_id(self):
        metadata = self.images()
        for registry in ("quay.io/noiro", "noiro"):
            for tag in ("6.1.1.8.42.z", "6.1.1.8.42.111226.123"):
                env = dict(self.env, MISMATCH_IMAGE_ID_REF=f"{registry}/image-0:{tag}")
                result = self.cli("prepare-images", "--metadata", metadata, "--bundle", self.root / "bad", *self.common,
                                  env=env, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "bad").exists())

    def test_git_config_auth_values_are_removed_before_docker_and_scanners(self):
        self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0="STATUS_CREDENTIAL_SENTINEL")
        self.prepare()

    def test_a_b_a_replay_does_not_regress_timestamp_or_create_commit(self):
        first = self.prepare(self.images(name="first"), "first-bundle")
        metadata = self.images(name="second")
        row = json.loads(metadata.read_text())
        row["image"] = "independent-component"
        metadata.write_text(json.dumps(row))
        second = self.prepare(metadata, "second-bundle")
        for bundle, timestamp in ((first, "2026-11-12 10:00:00 PST"), (second, "2026-11-12 11:00:00 PST")):
            manifest = json.loads((bundle / "manifest.json").read_text())
            manifest["prepared_at"] = timestamp
            (bundle / "manifest.json").write_text(json.dumps(manifest))
        repo, checkout = self.remote()
        self.publish(first, repo)
        self.publish(second, repo)
        head = self.git("--git-dir", str(repo), "rev-parse", "main")
        self.publish(first, repo)
        self.assertEqual(head, self.git("--git-dir", str(repo), "rev-parse", "main"))
        status = self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml")
        self.assertIn("2026-11-12 11:00:00 PST", status)
