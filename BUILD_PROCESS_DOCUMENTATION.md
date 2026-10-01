# ACI Containers Build and Release Process

**Verified:** 2026-10-01
**Scope:** The MMR 6.1.1 release branch and repository default branches. This document describes the current GitHub Actions path and its known gaps; the 6.1.1.8 release tags have not yet been created.

## Current summary

The GitHub Actions migration is partial. The MMR branch has tag-triggered package/image workflows for acc-provision, aci-containers, and OpFlex. Those workflows reuse the existing CICD build and publishing scripts from the **travis/** directory. That directory name is historical: Actions sets compatible environment variables and invokes the scripts directly.

The default branches of the product repositories have not migrated their build jobs to Actions. Travis configuration files remain in the repositories, and the MMR files do not yet contain a permanent condition that prevents those jobs from running if a Travis hook is re-enabled.

## Repository and workflow map

| Repository | MMR 6.1.1 | Default branch | Role |
|---|---|---|---|
| [acc-provision](https://github.com/noironetworks/acc-provision/tree/mmr-6.1.1) | [Package publisher](https://github.com/noironetworks/acc-provision/blob/mmr-6.1.1/.github/workflows/acc-provision-tag-publish.yml), on numeric version tags | .travis.yml only; no Actions workflow | Builds the package and publishes to PyPI or TestPyPI from tag-message intent |
| [aci-containers](https://github.com/noironetworks/aci-containers/tree/mmr-6.1.1) | [Image builder](https://github.com/noironetworks/aci-containers/blob/mmr-6.1.1/.github/workflows/aci-containers-images.yml), on numeric version tags | .travis.yml only; no Actions workflow | Builds eight images and publishes them through shared CICD scripts |
| [opflex](https://github.com/noironetworks/opflex/tree/mmr-6.1.1) | [Image builder](https://github.com/noironetworks/opflex/blob/mmr-6.1.1/.github/workflows/opflex-images.yml), plus [build-base tag workflow](https://github.com/noironetworks/opflex/blob/mmr-6.1.1/.github/workflows/push-git-tag.yaml) and CodeQL | CodeQL and the Travis-dependent build-base tag workflow; no release image publisher | Builds the OpFlex base/runtime images on MMR release tags |
| [acc-provision-operator](https://github.com/noironetworks/acc-provision-operator/tree/mmr-6.1.1) | No Actions workflow; .travis.yml remains | .travis.yml only; no Actions workflow | MMR has no verified Actions image producer |
| [support](https://github.com/noironetworks/support/tree/mmr-6.1.1) | Jenkins scripts, no MMR build workflow | Issue automation workflow only | Jenkins tagger and internal pipeline support |
| [cicd](https://github.com/noironetworks/cicd/tree/mmr-6.1.1) | Shared scripts and release configuration; no MMR Actions workflow | Scheduled CVE updater; no PR validation workflow | Shared build, publish, and status-update scripts |
| [cicd-status](https://github.com/noironetworks/cicd-status/tree/main) | Not applicable | Release portal data; no Actions workflow found | Stores release records and artifacts |

## Release flow on MMR

### 1. Set and verify release inputs

A release bump is represented in several repositories:

- CICD travis/globals.sh: RELEASE_TAG and UPSTREAM_ID
- support jenkins/aci-containers/globals.sh: RELEASE_TAG and UPSTREAM_SHA
- acc-provision provision/setup.py: package version
- acc-provision provision/acc_provision/versions.yaml: image pins

The values must agree. The release workflows also compare the tag-derived version with the CICD branch's RELEASE_TAG. The current 6.1.1.8 bump merged through [acc-provision #1459](https://github.com/noironetworks/acc-provision/pull/1459), [CICD #85](https://github.com/noironetworks/cicd/pull/85), and [support #3447](https://github.com/noironetworks/support/pull/3447). Their configured upstream value is 81c2369.

### 2. Create release tags

The MMR GHA workflows listen for numeric version tags. They require an annotated tag that resolves to the current configured source-branch head. Create tags only after all intended source changes are merged and the target SHAs have been checked.

Support's Jenkins git_tag.sh creates annotated tags. For OpFlex build-base builds it creates both a *-opflex-build-base tag and the plain release tag. It currently pushes with git push --tags; pushing only the intended tag refs is safer and makes the Actions event explicit. Jenkins job configuration and tag scheduling live outside this repository and must be checked in Jenkins.

### 3. Run the GitHub Actions workflows

- **acc-provision:** checks the tag, clones the configured CICD branch, sets the Travis-compatible variables, builds the package with build-acc-provision.sh, and publishes through push-to-pypi.sh.
- **aci-containers:** checks the tag and upstream value, prepares the compatibility environment, runs build-push-aci-containers-images.sh, validates the publication manifests/digests, and optionally updates CICD status. SKIP_CICD_STATUS is currently false.
- **OpFlex:** checks the tag and source, runs the shared OpFlex build script for the base/runtime image jobs, and optionally updates CICD status. SKIP_CICD_STATUS is currently false.
- **acc-provision-operator:** no corresponding MMR Actions build was found. The MMR .travis.yml refers to travis/build-push-acc-provision-operator-image.sh, which is removed from CICD's MMR branch.

All three release workflows upload artifacts with 14-day retention; ACI and OpFlex also write step summaries. A dedicated team notification for failed or timed-out image builds is not configured.

### 4. Update the CICD status portal

The portal's data is in cicd-status/docs/release_artifacts/releases.yaml. The acc-provision publisher uses push-to-cicd-status.sh and update-release.py; the ACI and OpFlex Actions paths use publish-github-actions-test-status.sh and update-github-actions-release.py.

At this audit, the portal had a 6.1.1.7 record and no 6.1.1.8 record. Make sure the new release record/stream is available before ACI or OpFlex status publication, or serialize record creation so parallel repository builds cannot race. A successful image build alone does not prove the portal has complete release metadata.

## Pull requests and testing

The ACC, ACI, and OpFlex release workflows are triggered by version-tag pushes, not PR creation. No PR test workflow was found for those repositories or acc-provision-operator. OpFlex CodeQL does run for pull requests, but it downloads dependency packages from a Travis-hosted S3 artifact and does not run the repository test suite. CICD has a Python test file for its Actions status updater but no PR workflow that runs it.

Add unprivileged PR build/test workflows before enabling required checks. PR jobs should not publish packages, push images, or require release credentials. Add CICD PR checks for Python tests, shell syntax, and the shared-script interfaces consumed by Actions.

## Local source builds

This document covers release automation. For developer builds, use the source repository's build instructions: [aci-containers README](https://github.com/noironetworks/aci-containers/blob/mmr-6.1.1/README.md), [OpFlex build guide](https://github.com/noironetworks/opflex/blob/mmr-6.1.1/docs/building_and_running.md), [acc-provision build notes](https://github.com/noironetworks/acc-provision/blob/mmr-6.1.1/provision/README.md), and [acc-provision-operator README](https://github.com/noironetworks/acc-provision-operator/blob/mmr-6.1.1/README.md).

## Current migration gaps

1. **Travis configurations are not permanently gated.** The MMR .travis.yml files still define tag jobs. Verify the Travis service hooks are disabled and add a root-level condition such as `if: repo = __travis_disabled__` that rejects all builds. Keep the shared scripts under travis/ because GHA calls them. See Travis's [conditional-build documentation](https://docs.travis-ci.com/user/conditional-builds-stages-jobs/).
2. **OpFlex still depends on Travis at runtime.** Its *-opflex-build-base workflow polls the Travis API in an unbounded loop. Its CodeQL workflow downloads build dependencies from a Travis S3 URL. Replace both dependencies or remove the obsolete trigger workflow.
3. **The operator image producer is missing on MMR.** Add the supported Actions build or identify and document the external producer before treating the release as complete.
4. **PR validation is missing.** Tag-only release workflows do not validate routine source changes; OpFlex CodeQL is not a substitute for tests.
5. **Failure notification is missing.** Artifacts and summaries aid diagnosis but do not alert a team. Add notifications for job failure and timeout with tag, run link, failed job, and artifact link.
6. **Workflow YAML still contains release-specific drift.** ACI and OpFlex success summaries link to the 6.1.1.7.z portal page even when building a later tag. Derive the link from the workflow's release value.
7. **PyPI tag verification needs hardening.** push-to-pypi.sh greps git tag -v output for a key fingerprint but does not require a successful verification result and valid signature status. This was deferred for 6.1.1.7; complete the follow-up before a production PyPI release.
8. **Default-branch build migration remains.** acc-provision, acc-provision-operator, aci-containers, and OpFlex default branches still contain Travis build jobs and have no equivalent default-branch Actions image/package workflow. CICD and support have separate shared-script/Jenkins control-plane roles.

## Resilience checks that still apply to shared scripts

Moving orchestration to Actions does not retire the reliability requirements inside shared CICD scripts. The MMR workflows still call these scripts, so keep these checks on the roadmap:

- Confirm every build and registry-push failure reaches the Actions job as a nonzero result, with bounded retry behavior for transient registry/status failures.
- Verify expected tags and immutable digests in each registry. The ACI workflow validates publication manifests; apply equivalent evidence to OpFlex and the operator image producer.
- Confirm security scans and artifact validation complete before release status is marked complete.
- Define recovery behavior for mutable or force-pushed release tags.
- Pin external build inputs and verify cross-repository artifacts to keep builds reproducible.

These areas were not fully re-audited in this migration pass; the GHA move does not resolve them by itself.

## Recommended order

### Before creating the 6.1.1.8 tags

- Permanently gate the four product .travis.yml build configurations and confirm Travis service-side hooks are disabled.
- Replace or disable the OpFlex Travis-polling workflow and replace the Travis S3 dependency used by CodeQL.
- Decide and verify the acc-provision-operator image producer.
- Ensure the 6.1.1.8 status record can be created before image status updates.
- Fix stale status links, harden PyPI tag verification, and push only the intended tag refs.
- Only then create annotated tags at the verified source branch heads and monitor every release workflow.

### After the release path is stable

1. Add PR tests/build checks and CICD shared-script CI.
2. Add team notifications for image build failures/timeouts.
3. Extract common GHA environment and tag/version validation into tested shared CICD helpers; keep the existing build/publish scripts as the implementation.
4. Migrate default-branch builds repo by repo, comparing output tags and digests before removing each Travis build entry point.

## 6.1.1.8 audit snapshot

As of 2026-10-01, all three bump PRs are merged and the corresponding acc-provision, CICD, and support MMR branch heads match their merge commits. The release configuration is ready in CICD, support, and acc-provision, but the numeric source tags are absent. No .8 release build has run, the status portal has no .8 record, and the operator-image producer and Travis-dependent OpFlex paths remain unresolved.

This snapshot reflects GitHub source and Actions metadata only. Jenkins job settings, Travis organization settings, secret values, and any manual release procedure were not inspected.
