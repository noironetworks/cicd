#!/usr/bin/env python3
"""Offline Docker/Trivy fixture, records actual CLI invocations and credential scope."""
import hashlib
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
for key, value in os.environ.items():
    if value == "STATUS_CREDENTIAL_SENTINEL":
        sys.exit("credential reached " + name)
with open(os.environ["TOOL_LOG"], "a") as stream:
    stream.write(json.dumps({"tool": name, "args": sys.argv[1:]}) + "\n")
if name == "trivy":
    if os.environ.get("FAIL_TRIVY"):
        sys.exit(7)
    print("Trivy evidence for " + sys.argv[-1])
elif name == "grype":
    content = sys.stdin.read()
    print(content.strip())
    print("NAME VERSION SEVERITY\npackage 1 High\n")
elif sys.argv[1] == "sbom":
    print("SBOM for " + sys.argv[-1])
elif sys.argv[1:3] == ["image", "inspect"]:
    reference = sys.argv[-1]
    if any("{{.Id}}" in argument for argument in sys.argv):
        print("sha256:" + "e" * 64)
    else:
        repository = reference.rsplit(":", 1)[0]
        digest = hashlib.sha256(reference.encode()).hexdigest()
        print(json.dumps([repository + "@sha256:" + digest]))
elif sys.argv[1] in ("pull", "push", "tag"):
    print("pulled " + sys.argv[-1])
else:
    sys.exit("unexpected Docker fixture command: " + " ".join(sys.argv))
