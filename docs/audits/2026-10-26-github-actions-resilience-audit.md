# GitHub Actions resilience audit and remediation plan

| Item | Value |
| --- | --- |
| Audit date | 2026-10-26 |
| Initial evidence baseline | 2026-10-26T21:09:40Z |
| Publication freshness check | 2026-10-26T22:46:23Z–22:46:59Z; the eleven public branch heads below were unchanged |
| Implementation status | Findings and proposed remediation for team review |
| Method | Consolidation of independent developer, workflow architecture, and operations reviews, followed by source, API, registry, and retained-artifact cross-checks |

## 1. Summary and agreed outcomes

The MMR release workflows successfully build and publish artifacts, and several useful safeguards are already in place. End-to-end readiness, recovery, and operational visibility still need work. A successful component job does not currently establish that every production consumer can retrieve a complete, consistent release.

The intended outcome is a release process in which:

- Production publication requires authorized signing, passing tests for the selected source, and the expected artifacts in the consumer registries.
- Source commits, shared CICD code, dependency digests, and produced artifacts are recorded together.
- Partial publication has an explicit state and a bounded repair path.
- Failed, cancelled, disabled, or missing runs produce actionable evidence.
- Security findings remain informational, with unavailable or stale data clearly identified.
- The verified MMR implementation becomes the baseline for subsequent default-branch migration.

Decisions retained for implementation:

1. Stabilize `mmr-6.1.1` first; migrate product default branches afterward. Shared services on `cicd/main` and `cicd-status/main` remain part of MMR reliability work.
2. Retain Jenkins as the release tag producer during this work.
3. Preserve the production signing step: an authorized signer may replace the numeric preview tag at the same approved source commit before production. Freeze both the final numeric tag object and its source commit after production completion.
4. Keep scans informational. Findings or scanner outages must not block artifact or status publication; errors must not be reported as zero findings.
5. Reuse the shared implementation under `cicd/travis/`. That directory name does not establish that Travis is executing it.

This is a public review document. Private operational evidence, infrastructure details, signing identities, and security reproduction instructions are excluded.

## 2. Scope, revisions, and evidence boundaries

### Public branch snapshots

Source observations refer to these exact revisions. Default branches were checked independently of release branches; local feature checkouts were not treated as deployed code.

| Repository | `mmr-6.1.1` | Default branch and SHA |
| --- | --- | --- |
| acc-provision | `b3183b58a8e550274a07618f97561946d59770bb` | `master`: `b0e73e4fe4c773909bc9b14ede7964ae16b02689` |
| acc-provision-operator | `fa49e7ee4bc7c2e76d9cd09e45b37955ee846637` | `master`: `e7f1526f61e95578523e5ad86ddbddeb0af9d22f` |
| aci-containers | `7a10b17098fbbae72b91674e03215b2a646a01dd` | `master`: `dd8b98e69de94b52cf86ae1e14178b9555d12912` |
| opflex | `ba1fd7da1009827aa4fae4ec3888b87719fa204a` | `master`: `bf4812d4f0bcf483cf17a7c1c160af908a1d411b` |
| cicd | `f2a51cf597eb5b37faf7df720793b310bd2b7657` | `main`: `de12c4f75c1377aa207d6adc6a78fcc2149f4299` |
| cicd-status | No MMR branch | `main`: `4ca21edc340a476f38f27f9e3ca4d18f124f72b0` |

CICD's release settings differ intentionally: MMR selects `6.1.1.8`, while `main` selects `6.1.2.1`. A consistency check must operate within the intended release line.

Evidence classifications used below:

- **Observed:** returned by a public API/registry, present in retained execution evidence, or reproduced with an isolated local fixture.
- **Source-verified:** the inspected implementation permits the described failure. This does not establish that it happened in a production release.
- **Unverified:** deployment ownership, external service configuration, or policy that could not be established from the available evidence.

Source inspection cannot prove that external Jenkins or Travis jobs are enabled or disabled. A workflow catalog entry alone cannot prove that its source exists on the owning branch or that it runs successfully. Secret metadata dates do not establish credential expiry or scope. Private operational follow-up must be handled by the relevant owners.

## 3. Current workflows and release state

| Repository | MMR implementation | Default implementation and remaining gap |
| --- | --- | --- |
| acc-provision | PR/branch lint and unit tests; numeric-tag package build and publication | No equivalent migrated workflow on `master` |
| aci-containers | PR/branch validation; numeric-tag publication of eight runtime images | No equivalent migrated workflow on `master` |
| opflex | PR/branch unit tests; base then runtime image build; CodeQL; legacy build-base tag waiter | CodeQL and legacy waiter remain; no equivalent migrated release/UT workflow |
| acc-provision-operator | No Actions producer; obsolete Travis configuration remains | Component [deliberately retired][operator-retirement] from shared builds; clean up configuration rather than restore production by default |
| cicd | Shared scripts and Actions adapters | Scheduled CVE workflow is `disabled_inactivity`; mirror delivery is not established by its catalog entry |
| cicd-status | `main` serves preview and release records | Pages deployment success establishes site deployment, not upstream build or scan freshness |

Workflow source: [ACC workflow][acc-workflow], [ACI workflow][aci-workflow], [OpFlex workflow][opflex-workflow], [legacy waiter][legacy-waiter], [CVE workflow][cve-workflow].

### Verified release observations

- Final `6.1.1.8` tag runs succeeded for [ACC][acc-release-run], [ACI][aci-release-run], and [OpFlex][opflex-release-run]. Their exact source SHAs also have successful branch test runs: [ACC][acc-test-run], [ACI][aci-test-run], and [OpFlex][opflex-test-run].
- These test successes were not enforced before release execution. OpFlex's base build began at `05:43:21Z`, while the same-SHA unit tests finished at `05:48:10Z` on October 23.
- At the publication freshness check, the [status snapshot][status-snapshot] contained nine images and TestPyPI package `6.1.1.8.dev35` in `6.1.1.8.z`. The production `6.1.1.8` stream remained `released: false`, with no production package or images recorded.
- The production PyPI version endpoint returned HTTP 404; the TestPyPI version endpoint returned HTTP 200.
- Registry checks at `22:46:33Z–22:46:35Z` covered all nine runtime images, both plain `6.1.1.8.81c2369` and `.z` tags, and all three public registry namespaces. Plain tags existed on `quay.io/noirolabs`, but were absent on `quay.io/noiro` and Docker Hub. The `.z` tags existed across all three with matching digests per image. There were no authentication, server, or network errors in that check.

The standard runtime set is `aci-containers-controller`, `aci-containers-host`, `aci-containers-host-ovscni`, `aci-containers-operator`, `aci-containers-webhook`, `aci-containers-certmanager`, `cnideploy`, `openvswitch`, and `opflex`. Build-only images are dependencies, not additional user-facing release components. Optional features require a separate supported-image inventory.

## 4. Findings: failure, visibility, and acceptance

IDs preserve the consolidated review mapping. Priorities here are: **P0** before the next production promotion; **P1** the next MMR reliability batch; **P2** subsequent optimization/migration. Scan availability remains advisory at every priority.

### Developer using the workflow

| ID | Priority | Finding and failure visibility | Remediation and acceptance |
| --- | --- | --- | --- |
| DX-1 | P0 | **Required checks are not enforced. Observed:** all six active product MMR/master branch endpoints reported empty required checks and enforcement off. A failed or absent test need not block merging. Other review controls may still apply. | Require `Lint and unit tests` for ACC, `ACI unit tests` for ACI, and `OpFlex unit tests` for OpFlex, from GitHub Actions app ID `15368`; the release gate also verifies the expected workflow identity. A failing required test must prevent an ordinary merge. Verify settings rather than infer policy from `protected: true` or a protection-detail 404. |
| DX-2 | P1 | **Tag jobs do not enforce same-SHA tests. Source-verified and observed race:** tests skip tag events, and publishing can start before branch tests finish. A green tag workflow may include a skipped UT job. | Verify exact source SHA, trusted workflow, branch-push event, expected job, latest matching attempt, and completed success. Reject skipped/neutral results and wrong producers. A failed test must stop publication before registry authentication. |
| DX-3 | P1 | **PR validation omits release paths. Source-verified:** ACI's explicit test list excludes `pkg/util`; PR builds do not cover every release build configuration. Green tests can miss existing tests or tagged compilation errors. | Add omitted tests and validate the test-bearing package inventory, including the nested IPAM module. Reuse real release assembly targets without publishing. A deliberately failing omitted test or release-only compile error must fail CI. |
| DX-4 | P2 | **Duplicate runs and shared concurrency obscure feedback. Observed/source-verified:** PR and push events duplicate work; pending branch tests can be displaced. | Use cancellable PR groups, independent eligible push tests per SHA, and separate publication serialization. Rapid updates must preserve the test evidence needed by an approved release. |
| DX-5 | P2 | **Summaries and external reporting can mislead. Source-verified:** OpFlex links an older release, ACC lacks an input summary, and coverage upload shares the test verdict. | Derive links from actual outputs, summarize inputs and artifact identities, and separate optional remote coverage delivery from test execution. Reporting outages must not conceal test failures or misidentify a release. |

Evidence: [test inventory and release assembly][aci-makefile], [release workflow conditions][acc-workflow], [OpFlex workflow and summary][opflex-workflow].

### DevOps engineer designing the workflow

| ID | Priority | Finding and failure visibility | Remediation and acceptance |
| --- | --- | --- | --- |
| AR-1 | P0 | **Production tag authorization needs genuine cryptographic verification. Source-verified and isolated fixture checked:** the current authorization decision does not reliably establish an authorized valid signature. | Require successful verification against an explicitly authorized key and bind approval to the selected tag object and commit. Invalid or unauthorized signatures must fail before upload. Reproduction details are withheld here. |
| AR-2 | P0 | **Package publication does not establish consumer image availability. Observed:** all nine plain runtime tags were absent from both consumer mirror namespaces, while preview tags existed. Package upload can succeed before deployments can pull its image references. | Before production upload, verify supported generated image references and promote only approved digests. Require the complete component set before production completion. Compare against a frozen approved manifest, not the current mutable `.z`. |
| AR-3 | P0 | **Cross-repository inputs are not bound to one approved build. Observed:** ACI consumed an older OpFlex base; ACC embedded an earlier ACI commit. Runs can be green with mixed inputs. | Seed release state idempotently, record build-base readiness, and pass approved source commits/base digests to consumers. Reconcile or rebuild inputs that differ from the approved cohort before production promotion. Concurrent tagging must wait for matching inputs or fail clearly. |
| AR-4 | P1 | **Release tag movement complicates provenance and recovery. Source-verified/public execution evidence:** the retained waiter can rewrite release tags. | Restrict ref updates and use explicit release refs. Preserve authorized same-commit signing conversion before release; freeze the final numeric tag object and its source commit after completion. Static rulesets alone cannot implement a policy based on release metadata. |
| AR-5 | P1 | **Shared CICD code is selected from a moving branch without equivalent PR CI. Source-verified:** the same product SHA can use different scripts on retry. | Record and reuse an approved CICD SHA, protect changes, and run existing updater/script checks in PR CI. Advancing a branch must not change an approved retry's inputs. |
| AR-6 | P1 | **Registry writes, alias movement, and status are not a transaction. Source-verified:** a later image, scan, or status failure can follow earlier successful pushes; package reruns can hit an existing version. | Stage immutable candidates, persist manifests, and record pending/promoting/partial/recorded states. Promotion across registries is resumable, not atomic. Verify identical existing package checksums and repair status independently. |
| AR-7 | P1 | **Version checks can miss stale image pins. Source-verified:** the packaging script does not enforce the result of its version search. | Validate active supported image pins against approved release inputs, and include the check in PR and pre-tag validation. One stale required pin must fail before upload. Historical or unused keys must not become universal dependencies. |
| AR-8 | P2 | **Travis execution paths and retirement leftovers remain. Source-verified:** numeric MMR image paths are partly excluded, but other stages, the waiter, and legacy dependency downloads remain. | Gate obsolete execution while preserving shared scripts. Coordinate waiter retirement with the tag producer. Release-candidate publication must have a supported image path or fail clearly. Do not restore the retired operator by default. |
| AR-9 | P2 | **Default branches lack equivalent migrated release coverage. Source-verified:** default source does not contain the MMR workflow set. External build health remains unverified. | Port verified MMR behavior one repository at a time, with release-line configuration and explicit dependency inputs. Confirm output parity and default-branch PR checks before declaring migration complete. |

Evidence: [package publication][package-publisher], [package image pins][version-pins], [ACI base dependency][aci-builder], [package ACI checkout][aci-clone], [shared publication order][image-publisher], [production completion logic][legacy-updater], [packaging version logic][package-builder].

### DevOps engineer maintaining the workflow

| ID | Priority | Finding and failure visibility | Remediation and acceptance |
| --- | --- | --- | --- |
| OP-1 | P0 | **Status history is actively rewritten. Observed:** repeated force-pushes replace status commits roughly every four hours. Pages can remain green while history changes. Actual concurrent release-data loss was not established. | Identify the deployed writer, use new commits and bounded normal-push conflict recovery, then prohibit force-push. Interleaved updates must both remain reachable. Do not identify a specific executing job or branch without deployment evidence. |
| OP-2 | P1 | **Scan freshness and errors are not represented reliably. Observed/source-verified:** no persisted `.8` refresh evidence was found after the build; permissive parsers can report clean counts for invalid data. Missing reports can also fail metadata publication after image push. | Record scan status, time, scanner, and error independently of build success. Preserve the actual build-time base identity. Empty/invalid reports become unknown, not zero. Close report writers before parsing. Scanner outages must permit artifact/status publication and produce an explicit advisory state. |
| OP-3 | P1 | **ACC still uses the legacy unbounded status writer. Source-verified:** upload success followed by status failure has an unreliable repair path. | Implement bounded production and TestPyPI semantics deliberately. The existing bounded helper is TestPyPI-only and cannot be substituted unchanged. Verify registry URL, exact version, release state, and existing artifact hash during recovery. |
| OP-4 | P1 | **Durable provenance depends on expiring evidence. Source-verified:** full workflow artifacts retain 14 days. Historical reconstruction loses script/input identity and full diagnostics. | Store source/CICD identities, input and output digests, package hashes, and run links with release metadata. Provenance must remain usable after Actions artifacts expire. |
| OP-5 | P1 | **Credential ownership and scope need operational review. Operational follow-up:** source/configuration alone does not establish deployed scope, expiry, or ownership. | Use explicit job permissions and narrowly scoped publication/reporting credentials; remove unused credential exposure and unsafe logging. Owners verify rotation, revocation, and blast radius separately. Private details are excluded. |
| OP-6 | P1 | **Executable dependencies move independently of source. Source-verified:** scanners and dependency bundles are not uniformly pinned or verified. | Pin executable inputs and checksums, isolate scanners from publication credentials, and record tool/base identities. A changed download must be rejected before execution. Advisory scan failures remain non-gating. |
| OP-7 | P1 | **Failures and missed runs lack a durable shared record. Source/API-verified:** success-path status updates can leave the previous result looking current; disabled scheduled work may create no failing run. | Add deduplicated failure records and an independently owned liveness monitor. Detect cancellation, timeout, missing triggers, disabled workflows, stale state, and registry/status disagreement. Configure the notification destination explicitly. |
| OP-8 | P2 | **The mirror catalog entry does not establish an operating service. Observed:** intended source remains in an open PR and the recorded run failed. | Establish the owning branch, verified successful delivery, digest/platform checks, and freshness monitoring before relying on the mirror. |
| OP-9 | P2 | **Recovery procedures do not cover the Actions migration. Review finding:** partial registry promotion, status-only repair, and tag/signing recovery need documented contracts. | Publish report-only reconciliation and repair procedures. Demonstrate recovery from each recorded partial state without moving approved release sources. |

Evidence: [original public status commit][status-original], [later replacement][status-replacement], [current status snapshot][status-snapshot], [active image-report parser][image-updater], [legacy ACC status writer][legacy-status-writer], [bounded TestPyPI helper][test-status-writer], [disabled updater source][cve-workflow], [mirror proposal][mirror-pr].

## 5. Corrections established during cross-checking

1. **Required-check uncertainty is resolved.** The branch endpoint reports enforcement off and empty checks even when a protection-detail endpoint is unavailable. This establishes the status-check gap, not the absence of every review control.
2. **Passing tests were not always earlier than publishing.** All final SHAs eventually passed; OpFlex publication began before its UT completed. A release gate must inspect actual job completion, not run creation time.
3. **CI-only source changes do not establish identical artifacts.** Retained base SBOMs from the [prior OpFlex build][prior-base-run] and [final OpFlex build][opflex-release-run] differ in dependency packages. The impact on ACI's copied subset remains unverified. ACC also embeds source/build provenance in its binary.
4. **The active writer is distinct from the dormant workflow.** Public commit ancestry establishes active replacement of status history. The disabled CICD updater has additional defects, including relative report paths and an unsafe push pattern; it must not be re-enabled unchanged. Its defects do not establish the implementation of the active external writer.
5. **Unchanged scan blobs are not proof that no refresh was attempted.** Report the lack of persisted freshness evidence and the displayed age/state. Pages commit time is not scan time.
6. **Preview on `cicd-status/main` is valid.** A `.z` record there does not imply production completion. Build-only operation and official status publication should nevertheless be explicit modes; official publication must not silently skip status after configuration loss.
7. **The bounded ACC helper is not production-ready.** It currently selects TestPyPI/dev semantics. Generalize it with tested inputs or provide equivalent bounded production reconciliation before switching callers.
8. **A final alias loop is still non-atomic.** Interruption during alias promotion requires an explicit partial state and resume/repair path. Preview builds must not implicitly promote consumer production tags.
9. **Compatibility requires coordinated changes.** Adding an attempt suffix to existing dated tags changes validators and consumers. Prefer an additional immutable attempt identity while retaining legacy aliases unless a tag-format migration is approved.
10. **Deployment environments apply to jobs.** If production uses an approval environment, bind it to a separate production job, the approved tag object, and immutable inputs. Coordinate ref restrictions with the authorized signing conversion.
11. **Optional/retired images require classification.** The standard set is nine runtime images; conditional GBP behavior and historical unused pins need an explicit supported-feature decision. The retired `acc-provision-operator` is distinct from the supported `aci-containers-operator` image.

## 6. Release-version update map and interfaces to consolidate

| Contract | Current responsibility | Required guard |
| --- | --- | --- |
| Release version and upstream identity | CICD release-line configuration | Match the selected product release; record the approved CICD revision |
| Tag-producer release selection | Existing orchestration | Verify the same release intent before applying explicit refs |
| Package version and image pins | ACC packaging configuration | Validate every required supported image reference before production upload |
| Workflow branch/configuration selection | Product workflow inputs and repository configuration | Keep MMR and default release settings distinct; avoid a repository-wide override silently selecting the wrong line |
| Preview/release records | Status metadata and publishers | Seed idempotently and record expected components before consumers wait |
| Consumer registry promotion | Production release operation | Verify all supported plain tags against the approved per-registry manifest |

A release bump is still distributed across these contracts. The target is one reviewed authoritative release configuration plus generated or validated consumer updates, rather than independent manual edits. Values that must be checked into package metadata still need reviewed updates.

The minimal additions are an immutable input manifest, explicit build-base readiness, per-component attempt/publication state, and advisory scan status/time/scanner fields. Preserve existing image/tag contracts and older status consumers during migration. Unknown scan counts must render as unknown rather than as zero.

## 7. Remediation batches and dependencies

| Batch | Scope and owner category | Exit condition |
| --- | --- | --- |
| B0: publication authorization and checks | CICD/product maintainers plus repository administrators; AR-1, DX-1 | Authorized signature verification and MMR required checks work. Ref restrictions accommodate the approved pre-production signing conversion. |
| B1: approved production cohort | Release/build maintainers; AR-2, AR-3, AR-7 | Required supported inputs and artifacts are frozen, consumer references exist, and production completion rejects missing components. Reconcile or rebuild consumers with mismatched inputs. |
| B2: status writer safety and scan honesty | Status-service and deployment owners; OP-1, OP-2, OP-7 | Active writer uses normal bounded updates, concurrent changes survive, scan state is explicit, and failure/missed-run reporting is operational. Prohibit force-push only after the deployed writer is compatible. |
| B3: readiness and validation contracts | CICD and product maintainers; AR-5, DX-2, DX-3, DX-4 | Idempotent seeding and build-base outputs exist before waits are enabled. Consumers use pinned inputs. Trusted same-SHA tests gate publishing; obsolete PR runs cancel without discarding required branch evidence. |
| B4: staged publication and repair | CICD/product/status maintainers; AR-4, AR-6, OP-3, OP-4 | Immutable attempt evidence survives publication failures. Production and TestPyPI reconciliation preserve correct semantics. Partial promotion resumes without changing approved sources. |
| B5: dependency and operational hardening | Build and operations owners; OP-5, OP-6, OP-8, OP-9, DX-5 | Executable inputs are verified, credentials are scoped, durable evidence/runbooks exist, and service ownership/freshness are verified. |
| B6: retire obsolete paths and port defaults | Product/orchestration owners; AR-8, AR-9 | Remaining obsolete triggers are gated or retired with producer coordination. Verified MMR behavior is ported to defaults one repository at a time. |

Failure reporting should start early; it need not wait for the entire publication redesign. Shared services on `main` may need changes during MMR stabilization. Broad product default-branch migration follows the verified MMR improvements.

Keep the existing tag producer, shared build scripts, canonical-repository publish guards, unprivileged PR tests, isolated registry credentials, publication manifests, and the OpFlex base/runtime digest contract. Extend these controls rather than duplicate the implementation.

## 8. Acceptance scenarios

| Scenario | Required result |
| --- | --- |
| Invalid/unauthorized tag signature; authorized same-commit signing conversion | Reject invalid authorization before upload; accept only the authorized conversion before release. Reject changes to either the final numeric tag object or its source commit after completion. |
| Missing plain consumer image or incomplete component manifest | Production upload/completion fails with the missing references listed. |
| Branch/CICD/base alias changes after approval | Consumers use recorded immutable inputs or reject the mismatch. Historical repair does not require the current branch tip to remain unchanged. |
| Pending, failed, skipped, neutral, cancelled, wrong-workflow, or stale test result | Select the newest trusted workflow run and its latest attempt for the exact source SHA and branch before evaluating the conclusion. An older successful attempt must not override a newer failed, cancelled, or pending attempt. Reject skipped/neutral results and wrong workflows/producers. API errors fail clearly within a bounded budget. |
| Existing omitted test or release-only compilation error | PR validation fails, including the relevant nested-module/tagged build configuration. |
| Image push fails before alias promotion | Preserve the previous accepted aliases and record staged/partial outputs. |
| Cancellation or failure during multi-registry alias promotion | Record the partial state and previous/target digests; resume or repair deterministically. Do not promise atomic registry updates. |
| Package upload succeeds but status write fails | Verify the already-published checksum and repair status without replacing or rebuilding the package. TestPyPI and production URLs/versions/release flags remain correct. |
| Concurrent status writers | Both updates survive with reachable commits and normal pushes. |
| Scanner outage, malformed/empty report, queued/unsupported response, or small buffered response | Display explicit unknown/error/stale status, retain the recorded build base, and permit artifact/status publication. Valid zero findings remain distinguishable from missing data. |
| Disabled schedule, missing trigger, expired credential, or timed-out run | Produce an actionable shared record with run/release identity and bounded recovery guidance. |
| Workflow artifacts expire | Release metadata still identifies source/CICD revisions, dependencies, produced digests, package checksums, and runs. |

Use disposable repositories, mocked I/O, and dedicated test namespaces for failure injection. The audit did not trigger a production release or execute these full pipeline scenarios. Isolated verification/parser fixtures support the relevant findings; they are not end-to-end production tests.

## 9. Review questions and evidence limits

Owners should confirm the deployed tag/status writers and their ref policy, the supported optional-image inventory, the final production approvers, notification ownership, credential scopes/rotation, service-side Travis retirement, and the intended mirror service. These are deployment/product questions, not facts inferable from a workflow file.

The publication freshness check is a dated snapshot, not a continuous monitor. Before acting on a finding, refresh the relevant branch, workflow/settings, run, registry, and package evidence. The report does not assert that every hypothesized failure has already occurred, that historical source similarity proves binary equivalence, or that a green Pages deployment proves release/security readiness.

Related documentation work: [build-process update PR #86][build-doc-pr]. The audit provides the review backlog; implementation and subsequent verification establish remediation completion.

## Public evidence references

Source links below are pinned to full audited commits. Run links identify historical execution evidence. Settings and registry/package observations are time-bound and should be rechecked before promotion.

[acc-workflow]: https://github.com/noironetworks/acc-provision/blob/b3183b58a8e550274a07618f97561946d59770bb/.github/workflows/acc-provision-tag-publish.yml
[aci-workflow]: https://github.com/noironetworks/aci-containers/blob/7a10b17098fbbae72b91674e03215b2a646a01dd/.github/workflows/aci-containers-images.yml
[opflex-workflow]: https://github.com/noironetworks/opflex/blob/ba1fd7da1009827aa4fae4ec3888b87719fa204a/.github/workflows/opflex-images.yml
[legacy-waiter]: https://github.com/noironetworks/opflex/blob/ba1fd7da1009827aa4fae4ec3888b87719fa204a/.github/workflows/push-git-tag.yaml
[cve-workflow]: https://github.com/noironetworks/cicd/blob/de12c4f75c1377aa207d6adc6a78fcc2149f4299/.github/workflows/update-released-cve.yml
[aci-makefile]: https://github.com/noironetworks/aci-containers/blob/7a10b17098fbbae72b91674e03215b2a646a01dd/Makefile
[package-publisher]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/push-to-pypi.sh
[version-pins]: https://github.com/noironetworks/acc-provision/blob/b3183b58a8e550274a07618f97561946d59770bb/provision/acc_provision/versions.yaml
[aci-builder]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/build-push-aci-containers-images.sh
[aci-clone]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/clone-aci-containers-repo.sh
[image-publisher]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/push-images.sh
[legacy-updater]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/update-release.py
[package-builder]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/build-acc-provision.sh
[image-updater]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/update-github-actions-release.py
[legacy-status-writer]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/push-to-cicd-status.sh
[test-status-writer]: https://github.com/noironetworks/cicd/blob/f2a51cf597eb5b37faf7df720793b310bd2b7657/travis/publish-github-actions-test-acc-provision-status.sh
[status-original]: https://github.com/noironetworks/cicd-status/commit/4d05d4a82610a3c133ad7b285ab2b84aabfa0a0f
[status-replacement]: https://github.com/noironetworks/cicd-status/commit/bbb2aa1119f15a2a08839696ce895bd33d7970c8
[status-snapshot]: https://github.com/noironetworks/cicd-status/blob/4ca21edc340a476f38f27f9e3ca4d18f124f72b0/docs/release_artifacts/releases.yaml
[acc-release-run]: https://github.com/noironetworks/acc-provision/actions/runs/39906679357
[aci-release-run]: https://github.com/noironetworks/aci-containers/actions/runs/39907740370
[opflex-release-run]: https://github.com/noironetworks/opflex/actions/runs/39907312328
[acc-test-run]: https://github.com/noironetworks/acc-provision/actions/runs/39904998462
[aci-test-run]: https://github.com/noironetworks/aci-containers/actions/runs/39904996629
[opflex-test-run]: https://github.com/noironetworks/opflex/actions/runs/39905198063
[mirror-pr]: https://github.com/noironetworks/cicd/pull/83
[build-doc-pr]: https://github.com/noironetworks/cicd/pull/86
[operator-retirement]: https://github.com/noironetworks/cicd/commit/50657ba9ebb3db31d8052488d3575c0bc8cf5ffa
[prior-base-run]: https://github.com/noironetworks/opflex/actions/runs/38398500469
