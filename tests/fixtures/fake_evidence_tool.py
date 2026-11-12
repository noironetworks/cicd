#!/usr/bin/env python3
"""Offline Docker/Trivy fixture, records actual CLI invocations and credential scope."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

name = Path(sys.argv[0]).name
for key, value in os.environ.items():
    if value == "STATUS_CREDENTIAL_SENTINEL":
        sys.exit("credential reached " + name)
with open(os.environ["TOOL_LOG"], "a") as stream:
    stream.write(json.dumps({"tool": name, "args": sys.argv[1:]}) + "\n")
mode = os.environ.get("SCAN_FIXTURE_MODE", "ok")
if name in ("grype", "syft", "trivy") and mode == "exec-failure":
    sys.exit(9)
if name in ("grype", "syft", "trivy") and mode == "timeout":
    time.sleep(2)
if name in ("grype", "trivy") and mode == "empty":
    sys.exit(0)
if name in ("grype", "trivy") and mode == "malformed":
    print("malformed scanner output")
    sys.exit(0)
if name == "trivy":
    if os.environ.get("FAIL_TRIVY"):
        sys.exit(7)
    if "--format" in sys.argv:
        digest = sys.argv[-1]
        print(json.dumps({"SchemaVersion": 2, "Metadata": {"ImageID": digest}, "Results": []}))
    else:
        print("Trivy evidence for " + sys.argv[-1])
elif name == "grype":
    if "json" in sys.argv:
        digest = sys.argv[1].removeprefix("docker:")
        if mode == "digest-mismatch":
            digest = "sha256:" + "f" * 64
        matches = [] if mode == "zero" else [{"vulnerability": {"severity": "High"}}]
        print(json.dumps({"source": {"target": {"imageID": digest}}, "matches": matches}))
    else:
        content = sys.stdin.read()
        print(content.strip())
        print("NAME VERSION SEVERITY\npackage 1 High\n")
elif name == "syft":
    print(json.dumps({"spdxVersion": "SPDX-2.3", "name": sys.argv[1], "packages": []}))
elif sys.argv[1] == "sbom":
    print("SBOM for " + sys.argv[-1])
elif sys.argv[1:3] == ["image", "inspect"]:
    reference = sys.argv[-1]
    if os.environ.get("REQUIRE_PULL_REF") == reference:
        pulled = Path(os.environ["PULLED_REFS_FILE"])
        if not pulled.exists() or reference not in json.loads(pulled.read_text()):
            sys.exit("base was not pulled before inspection")
    if any("{{.Id}}" in argument for argument in sys.argv):
        image = reference.rsplit("/", 1)[-1].split(":")[0]
        value = hashlib.sha256(image.encode()).hexdigest() if os.environ.get("DYNAMIC_IMAGE_IDS") else os.environ.get("FAKE_IMAGE_ID", "e" * 64)
        if os.environ.get("MISMATCH_IMAGE_ID_REF") == reference:
            value = "f" * 64
        print("sha256:" + value)
    else:
        repository = reference.split("@", 1)[0]
        if ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        digest = reference.split("@sha256:", 1)[1] if "@sha256:" in reference else hashlib.sha256(reference.encode()).hexdigest()
        print(json.dumps([repository + "@sha256:" + digest]))
elif sys.argv[1] in ("pull", "push", "tag"):
    if sys.argv[1] == "pull" and os.environ.get("PULLED_REFS_FILE"):
        pulled = Path(os.environ["PULLED_REFS_FILE"])
        refs = json.loads(pulled.read_text()) if pulled.exists() else []
        refs.append(sys.argv[-1])
        pulled.write_text(json.dumps(refs))
    if sys.argv[1] == "push" and os.environ.get("FAIL_PUSH_MATCH") in (sys.argv[-1], "all"):
        sys.exit(8)
    print("pulled " + sys.argv[-1])
else:
    sys.exit("unexpected Docker fixture command: " + " ".join(sys.argv))
