"""Real promotion CLI with offline Skopeo; authorization is a patched boundary.

The release-policy suite separately tests Git/GPG signatures and trusted checks.
No environment switch or fixture authorization path exists in the production CLI.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("aci-containers-host", "aci-containers-controller", "cnideploy", "aci-containers-operator",
         "openvswitch", "aci-containers-webhook", "aci-containers-certmanager", "aci-containers-host-ovscni", "opflex")


def raw(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class PromotionCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "registry.json"
        self.receipt = self.root / "receipt.json"
        self.log = self.root / "operations.jsonl"
        helper = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        config = raw({"architecture": "amd64", "os": "linux"})
        manifest = raw({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {"digest": sha(config)}, "layers": []})
        arm_config = raw({"architecture": "arm64", "os": "linux"})
        arm = raw({"schemaVersion": 2, "config": {"digest": sha(arm_config)}, "layers": []})
        index = raw({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": sha(manifest), "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": sha(arm), "platform": {"os": "linux", "architecture": "arm64"}}]})
        self.expected = sha(index)
        self.lock = {"schema_version": 1, "release_tag": "6.1.1.8", "cicd_commit": helper,
                     "sources": {"noironetworks/aci-containers": "b" * 40, "noironetworks/opflex": "c" * 40},
                     "images": {}, "dependencies": [{"reference": "quay.io/noirolabs/opflex-build-base:6.1.1.8.42.z", "digest": sha(manifest)}]}
        sources, configs = {}, {}
        for name in NAMES:
            repo = "noironetworks/opflex" if name == "opflex" else "noironetworks/aci-containers"
            entry = {"platform": "linux/amd64", "producer": {"repository": repo, "source_sha": self.lock["sources"][repo],
                     "cicd_commit": helper, "run_id": "1001", "run_attempt": 1}, "registries": {}}
            for registry in ("quay.io/noiro", "docker.io/noiro"):
                repository = registry + "/" + name
                entry["registries"][registry] = {"reference": repository + ":6.1.1.8.42", "digest": self.expected}
                sources[repository + "@" + self.expected] = index
                sources[repository + "@" + sha(manifest)] = manifest
                configs[repository + "@" + sha(manifest)] = config
            self.lock["images"][name] = entry
        base = "quay.io/noirolabs/opflex-build-base@" + sha(manifest)
        sources[base], configs[base] = manifest, config
        self.db.write_text(json.dumps({"sources": sources, "configs": configs, "targets": {}, "copies": []}))
        self.lock_path = self.root / "lock.json"
        self.lock_path.write_text(json.dumps(self.lock))
        self.checks = self.root / "checks.json"
        self.checks.write_text('{"authorized": true}')
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text("{}")
        self.key = self.root / "key.asc"
        self.key.write_text("fixture key")
        self.quay = self.root / "quay.json"
        self.docker = self.root / "docker.json"
        self.quay.write_text('{"auths":{"quay.io":{"auth":"fixture"}}}')
        self.docker.write_text('{"auths":{"docker.io":{"auth":"fixture"}}}')
        self.skopeo = self.root / "skopeo"
        self.skopeo.write_text("""#!/usr/bin/env python3
import json,os,pathlib,sys
for value in os.environ.values():
 if value=='CREDENTIAL_SENTINEL': sys.exit('credential leaked to Skopeo')
path=pathlib.Path(os.environ['PROMOTION_DB'])
db=json.loads(path.read_text())
args=sys.argv[1:]
with open(os.environ['PROMOTION_LOG'],'a') as out: out.write(json.dumps(args)+'\\n')
ref=args[-1].removeprefix('docker://')
if args[0]=='inspect':
 data=(db['configs'] if '--config' in args else {**db['sources'],**db['targets']}).get(ref)
 if data is None:
  print('manifest unknown',file=sys.stderr); sys.exit(1)
 sys.stdout.buffer.write(data.encode())
elif args[0]=='copy':
 if not all(flag in args for flag in ('--all','--preserve-digests','--src-authfile','--dest-authfile')): sys.exit(98)
 source=args[-2].removeprefix('docker://')
 if '@sha256:' not in source or ref.endswith('.z'): sys.exit(97)
 db['copies'].append(ref)
 number=len(db['copies'])
 if number==int(os.environ.get('FAIL_COPY_N','0')):
  path.write_text(json.dumps(db)); sys.exit(3)
 db['targets'][ref]=db['sources'][source]
 if number==int(os.environ.get('BAD_COPY_N','0')): db['targets'][ref]='{}'
 path.write_text(json.dumps(db))
 if number==int(os.environ.get('LOST_ACK_N','0')): sys.exit(3)
else: sys.exit(96)
""")
        self.skopeo.chmod(0o755)
        self.env = dict(os.environ, PROMOTION_DB=str(self.db), PROMOTION_LOG=str(self.log),
                        TRAVIS_TAGGER="CREDENTIAL_SENTINEL", PYPI_PASS="CREDENTIAL_SENTINEL",
                        GIT_CONFIG_VALUE_0="CREDENTIAL_SENTINEL")
        self.launcher = """import json,sys,pathlib
sys.path.insert(0,sys.argv.pop(1))
import promote_consumer_images as module
def authorize(repo,tag,policy,key,lock,checks):
 if not json.loads(pathlib.Path(checks).read_text()).get('authorized'): raise module.policy.PolicyError('unauthorized source fixture')
 return {'tag':tag,'tag_object':'d'*40,'source_sha':'a'*40},json.loads(pathlib.Path(lock).read_text())
module.policy.authorize_source=authorize
def check(repo,identity):
 path=pathlib.Path(repo)/'checks-count'
 count=int(path.read_text())+1 if path.exists() else 1
 path.write_text(str(count))
 if count==int(__import__('os').environ.get('TAG_CHANGE_AT','0')): raise module.policy.PolicyError('remote tag changed')
module.policy.assert_remote_tag=check
sys.exit(module.main())
"""

    def run_cli(self, **environment):
        return subprocess.run([sys.executable, "-c", self.launcher, str(ROOT / "travis"),
                               "--repository", str(self.root), "--tag", "6.1.1.8", "--policy", str(self.policy_path),
                               "--public-key", str(self.key), "--lock", str(self.lock_path), "--checks", str(self.checks),
                               "--receipt", str(self.receipt), "--quay-authfile", str(self.quay),
                               "--docker-authfile", str(self.docker), "--skopeo", str(self.skopeo)],
                              env=dict(self.env, **environment), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def database(self):
        return json.loads(self.db.read_text())

    def test_unauthorized_source_never_opens_credentials_or_registry(self):
        self.checks.write_text('{"authorized": false}')
        self.quay.unlink()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthorized", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.receipt.exists())

    def test_complete_cohort_required_and_no_exotic_consumer_reference(self):
        self.lock["images"].pop("opflex")
        self.lock_path.write_text(json.dumps(self.lock))
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_all_sources_and_existing_destinations_preflight_before_copy(self):
        db = self.database()
        db["sources"].pop(next(iter(db["sources"])))
        self.db.write_text(json.dumps(db))
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.database()["copies"])
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "failed")

    def test_existing_different_consumer_tag_is_never_replaced(self):
        db = self.database()
        db["targets"]["quay.io/noiro/opflex:6.1.1.8.42"] = "{}"
        self.db.write_text(json.dumps(db))
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.database()["copies"])

    def test_multiarch_promotion_preserves_all_and_noop_rechecks_actual_targets(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.database()["copies"]), 18)
        self.assertEqual(len(self.database()["targets"]), 18)
        self.assertTrue(all(sha(value) == self.expected and len(json.loads(value)["manifests"]) == 2
                            for value in self.database()["targets"].values()))
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.database()["copies"]), 18)
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "complete")

    def test_partial_failure_resumes_forward_without_recopy(self):
        self.assertNotEqual(self.run_cli(FAIL_COPY_N="3").returncode, 0)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(len(receipt["results"]), 2)
        first_two = self.database()["copies"][:2]
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(self.database()["copies"].count(reference) == 1 for reference in first_two))
        self.assertEqual(len(self.database()["targets"]), 18)

    def test_lost_ack_is_reconciled_and_target_mismatch_keeps_receipt(self):
        result = self.run_cli(LOST_ACK_N="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(self.receipt.read_text())
        self.assertTrue(any(item["state"] == "verified-after-ack-loss" for item in receipt["results"].values()))

    def test_remote_tag_is_rechecked_immediately_before_each_write(self):
        result = self.run_cli(TAG_CHANGE_AT="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.database()["copies"])

    def test_digest_mismatch_fails_and_forward_resume_skips_repaired_target(self):
        self.assertNotEqual(self.run_cli(BAD_COPY_N="2").returncode, 0)
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "partial")
        db = self.database()
        bad = db["copies"][1]
        repository = bad.rsplit(":", 1)[0]
        db["targets"][bad] = db["sources"][repository + "@" + self.expected]
        self.db.write_text(json.dumps(db))
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.database()["copies"].count(bad), 1)
