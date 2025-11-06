# ACI Containers Build System Documentation

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [System Architecture](#system-architecture)
3. [Build Process Overview](#build-process-overview)
4. [Repository Details](#repository-details)
5. [Build Timeline & Dependencies](#build-timeline--dependencies)
6. [Container Images Reference](#container-images-reference)
7. [Local Development Builds](#local-development-builds)
8. [Troubleshooting](#troubleshooting)
9. [Appendix](#appendix)

---

## Executive Summary

The ACI Containers build system is an automated CI/CD pipeline that produces Docker containers and Python packages for the ACI CNI (Application Centric Infrastructure Container Network Interface) ecosystem. The system uses **Jenkins + Travis CI** orchestration where:

1. **Jenkins** monitors GitHub pull requests and triggers git tag creation
2. **Travis CI** detects tags and executes container builds
3. **Container registries** receive and distribute the built images

### Key Metrics
- **Number of Repositories**: 5 (aci-containers, opflex, acc-provision, acc-provision-operator, cicd)
- **Container Images Produced**: 11 primary images
- **Build Duration**: 1.5 - 2.5 hours
- **Primary Registry**: quay.io/noirolabs
- **Current Release**: <Release Tag>

---

## System Architecture

### High-Level Flow

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   GitHub    │      │   Jenkins   │      │  Travis CI  │      │  Registries │
│             │──┬──▶│             │──┬──▶│             │──┬──▶│             │
│  PR Merged  │  │   │  Tag Maker  │  │   │  Builder    │  │   │  Quay.io    │
└─────────────┘  │   └─────────────┘  │   └─────────────┘  │   │  Docker.io  │
                 │                     │                     │   └─────────────┘
                 │                     │                     │
                 └─────────────────────┴─────────────────────┘
                           Automated Pipeline
```

### Component Relationships

```
                    ┌──────────────────────┐
                    │   cicd Repository    │
                    │  (Shared Scripts)    │
                    └──────────┬───────────┘
                               │ Cloned by all builds
            ┌──────────────────┼──────────────────┐
            │                  │                  │
    ┌───────▼────────┐ ┌───────▼────────┐ ┌──────▼───────────┐
    │     opflex     │ │ aci-containers │ │  acc-provision   │
    │   (Base Layer) │ │  (Core Layer)  │ │  (Tool Layer)    │
    └───────┬────────┘ └───────┬────────┘ └──────┬───────────┘
            │                  │                  │
            │         ┌────────▼────────┐         │
            │         │ acc-provision-  │         │
            └────────▶│    operator     │◀────────┘
                      │ (Orchestration) │
                      └─────────────────┘
```

---

## Build Process Overview

### Jenkins Integration Details

**Important**: The Jenkins jobs are **freestyle jobs** configured in Jenkins UI and are **not part of the source code repositories**. They serve as the trigger mechanism for the entire build pipeline.

#### Jenkins Job Architecture

Each repository has a dedicated Jenkins freestyle job:
- `aci-containers-build-job`
- `opflex-build-job`
- `acc-provision-build-job`
- `acc-provision-operator-build-job`

#### Jenkins Job Configuration Requirements

**Source Code Management**:
- Repository: `https://github.com/noironetworks/cicd`
- Branch: `main` (or appropriate branch)
- Purpose: Access shared build scripts

**Build Triggers**:
- GitHub webhook (monitors PR merge events)
- Manual trigger (for emergency builds)

**Environment Variables** (Must be configured in Jenkins):
```bash
RELEASE_TAG=<Release Tag>              # Current release version
TRAVIS_TAG=<Release Tag>.81c2369       # Full tag to create
TRAVIS_TAGGER=<github-token>     # GitHub personal access token
TRAVIS_JOB_NUMBER=${BUILD_NUMBER} # Jenkins build number
TRAVIS_JOB_WEB_URL=${BUILD_URL}  # Jenkins build URL
```

**Build Steps**:
```bash
# 1. Ensure we're in the cicd repository directory
cd cicd

# 2. Execute tag creation script
bash travis/push-git-tag.sh <repository-name>

# Examples:
# bash travis/push-git-tag.sh aci-containers
# bash travis/push-git-tag.sh opflex
# bash travis/push-git-tag.sh acc-provision
# bash travis/push-git-tag.sh acc-provision-operator
```

**Post-Build Actions**:
- Archive console logs
- Send notifications on failure

#### How to Set Up a New Jenkins Job

1. **Create Freestyle Project** in Jenkins
2. **Configure Source Code Management**:
   - Git repository: `https://github.com/noironetworks/cicd`
3. **Configure Build Triggers**:
   - GitHub hook trigger for GITScm polling
4. **Add Environment Variables**:
   - `RELEASE_TAG`
   - `TRAVIS_TAG`
   - `TRAVIS_TAGGER` (use Jenkins credentials binding)
5. **Add Build Step** (Execute Shell):
   ```bash
   bash travis/push-git-tag.sh <repository-name>
   ```
6. **Test** with a dry-run or test repository

---

### Stage 1: Pre-Build Trigger (Jenkins)

**Duration**: ~2-5 minutes  
**Trigger**: Pull Request merged to master/main branch

**Jenkins Configuration**:
- **Job Type**: Freestyle Jobs (defined in Jenkins UI, not in source code)
- **One job per repository**: aci-containers, opflex, acc-provision, acc-provision-operator
- **Tagging Script**: `cicd/travis/push-git-tag.sh`

**Process**:
1. GitHub webhook notifies Jenkins of merged PR
2. Jenkins freestyle job is triggered for the repository
3. Jenkins clones the `cicd` repository to get shared scripts
4. Jenkins executes `cicd/travis/push-git-tag.sh <repo-name>` with:
   - `$RELEASE_TAG` - Current release version (e.g., <Release Tag>)
   - `$TRAVIS_TAG` - Full tag to create (e.g., <Release Tag>.81c2369)
   - `$TRAVIS_TAGGER` - GitHub token for authentication
5. Script creates annotated git tag with message:
   ```
   "ACI Release {RELEASE_TAG} Created by Travis Job {JOB_NUMBER} {JOB_URL}"
   ```
6. Tag is force-pushed to GitHub repository
7. GitHub notifies Travis CI via webhook
8. Travis CI build is triggered automatically

**Tag Creation Details** (`push-git-tag.sh`):
```bash
#!/bin/bash
REPO=$1  # Repository name (e.g., "aci-containers")
git config --local user.name "travis-tagger"
git config --local user.email "sumitnaiksatam+travis-tagger@gmail.com"
git remote add travis-tagger https://travis-tagger:$TRAVIS_TAGGER@github.com/noironetworks/$REPO.git
TAG_MESSAGE="ACI Release ${RELEASE_TAG} Created by Travis Job ${TRAVIS_JOB_NUMBER} ${TRAVIS_JOB_WEB_URL}"
git tag -f -a ${TRAVIS_TAG} -m "${TAG_MESSAGE}"
git push travis-tagger -f --tags
```

**Example Tags**:
- `<Release Tag>` - Release tag
- `<Release Tag>.81c2369` - Release + upstream ID
- `<Release Tag>rc1` - Release candidate
- `<Release Tag>.z` - Z-stream (continuous release)

**Important Notes**:
- Tags are created with `-f` (force) flag, allowing tag updates
- Multiple repositories can be tagged in parallel by separate Jenkins jobs
- Travis CI detects these tags and filters them in `check-git-tag.sh` to prevent duplicate builds

### Stage 2: Build Validation (Travis CI)

**Duration**: ~1-2 minutes  
**Component**: `cicd/travis/check-git-tag.sh`

**Validation Steps**:
1. **Tag Format Check**: Validates tag matches expected prefix (`<Release Tag>.*`)
2. **Duplicate Prevention**: Checks if tag was created by Travis (prevents loops)
3. **Environment Setup**: Exports build variables

**Exit Codes**:
- `0` - Proceed with build
- `140` - Skip build (tag created by Travis or doesn't match)
- `Non-zero` - Error, terminate build

### Stage 3: Container Building (Travis CI)

**Duration**: ~40-80 minutes (depends on repository)

#### Build Matrix by Repository

| Repository | Duration | Parallel Builds | Dependencies |
|------------|----------|-----------------|--------------|
| opflex | 15-20 min | 1-3 containers | None (foundation) |
| aci-containers | 25-40 min | 8 containers | opflex-build-base |
| acc-provision-operator | 10-15 min | 1 container | acc-provision package |
| acc-provision | 15-20 min | Python package | aci-containers artifacts |

### Stage 4: Post-Build Actions

**Duration**: ~2-5 minutes

**Actions**:
1. **Registry Push**: Multi-registry push (quay.io/noirolabs, quay.io/noiro, docker.io/noiro)
2. **Status Update**: Update cicd-status repository with build metadata
3. **Version Coordination**: Update downstream dependencies
4. **Artifact Publishing**: Push to PyPI (acc-provision only)

---

## Repository Details

### 1. opflex Repository

**Purpose**: OpFlex protocol implementation and agent  
**Language**: C++  
**Build Tool**: CMake + Make + Maven (for Genie)

#### Containers Built

1. **opflex-build-base** (Special Build)
   - **Purpose**: Build environment with all dependencies
   - **Trigger**: Tag contains "opflex-build-base"
   - **Base Image**: registry.access.redhat.com/ubi9/ubi-minimal
   - **Size**: ~1.2 GB
   - **Contents**: GCC, Boost, CMake, Maven, all dev libraries

2. **opflex-build** (Development Image)
   - **Purpose**: Intermediate build artifacts
   - **Dependencies**: opflex-build-base
   - **Contents**: Compiled libraries, headers

3. **opflex** (Production Image)
   - **Purpose**: Runtime agent
   - **Base Image**: UBI 9 minimal
   - **Size**: ~300 MB
   - **Key Binaries**:
     - `opflex_agent` - Main agent process
     - `mcast_daemon` - Multicast daemon
     - `gbp_inspect` - Group-based policy inspector
     - `opflex_server` - OpFlex server

#### Local Build Instructions

```bash
# Clone repository
cd opflex

# Option 1: Build using Travis script (recommended)
export DOCKER_HUB_ID=myregistry
export DOCKER_TAG=local-test
./docker/travis/build-opflex-travis.sh $DOCKER_HUB_ID $DOCKER_TAG

# Option 2: Manual build
# First build the base image
docker build -t myregistry/opflex-build-base:local \
  -f docker/travis/Dockerfile-opflex-build-base .

# Generate model code
cd genie
mvn compile exec:java
cd ..

# Build opflex-build
docker build -t myregistry/opflex-build:local \
  -f docker/travis/Dockerfile-opflex-build .

# Build final opflex image
docker build -t myregistry/opflex:local \
  -f docker/travis/Dockerfile-opflex .
```

**Build Environment Variables**:
- `OPFLEX_BRANCH`: Source branch (default: kmr2-5.2.7)
- `DOCKER_HUB_ID`: Registry prefix
- `DOCKER_TAG`: Image tag
- `UPSTREAM_ID`: Upstream commit ID (default: 81c2369)

---

### 2. aci-containers Repository

**Purpose**: Main CNI implementation, controllers, and operators  
**Language**: Go (1.25.0)  
**Build Tool**: Make + Go build

#### Containers Built

1. **aci-containers-controller**
   - **Purpose**: Main Kubernetes controller
   - **Binary**: `dist-static/aci-containers-controller`
   - **Additional Tools**: kubectl, istioctl
   - **Config**: `/usr/local/etc/aci-containers/controller.conf`

2. **aci-containers-host**
   - **Purpose**: Host agent (without OVS CNI)
   - **Binary**: `dist-static/aci-containers-host-agent`
   - **Target**: `without-ovscni` (multi-stage build)

3. **aci-containers-host-ovscni**
   - **Purpose**: Host agent with OVS CNI support
   - **Binary**: Same as above + CNI plugins
   - **Target**: `with-ovscni` (multi-stage build)

4. **aci-containers-operator**
   - **Purpose**: Kubernetes operator for ACI CNI
   - **Binary**: `dist-static/aci-containers-operator`

5. **aci-containers-webhook**
   - **Purpose**: Admission webhook for validation
   - **Binary**: `dist-static/aci-containers-webhook`
   - **Launcher**: `launch-webhook.sh`

6. **aci-containers-certmanager**
   - **Purpose**: Certificate management
   - **Binary**: `dist-static/aci-containers-certmanager`
   - **Launcher**: `launch-certmanager.sh`

7. **cnideploy**
   - **Purpose**: CNI deployment tool
   - **Note**: Minimal image for deployment tasks

8. **openvswitch**
   - **Purpose**: Open vSwitch for networking
   - **Build**: Two-stage (base + final)
   - **Binaries**: ovs-vswitchd, ovsdb-server, ovs-vsctl, ovsresync
   - **Launcher**: `launch-ovs.sh`

#### Local Build Instructions

```bash
# Clone repository
cd aci-containers

# Install Go dependencies
make goinstall

# Build all static binaries
make all-static

# This creates binaries in dist-static/:
# - aci-containers-host-agent
# - aci-containers-host-agent-ovscni
# - opflex-agent-cni
# - netop-cni
# - aci-containers-controller
# - ovsresync
# - gbpserver
# - aci-containers-operator
# - aci-containers-webhook
# - aci-containers-certmanager

# Build individual container (example: controller)
docker build -t myregistry/aci-containers-controller:local \
  -f docker/travis/Dockerfile-controller .

# Build OpenVSwitch
./docker/travis/build-openvswitch-travis.sh myregistry local-test

# Build all containers using the main script
export DOCKER_BUILDKIT=1
export IMAGE_BUILD_REGISTRY=myregistry
export IMAGE_TAG=local-test
export UPSTREAM_ID=81c2369

# Copy iptables from base image
docker/copy_iptables.sh quay.io/noirolabs/opflex-build-base:<Release Tag>.81c2369.z dist-static

# Build each container
docker build -t $IMAGE_BUILD_REGISTRY/cnideploy:$IMAGE_TAG \
  -f docker/travis/Dockerfile-cnideploy .

docker build -t $IMAGE_BUILD_REGISTRY/aci-containers-controller:$IMAGE_TAG \
  -f docker/travis/Dockerfile-controller .

docker build --target without-ovscni \
  -t $IMAGE_BUILD_REGISTRY/aci-containers-host:$IMAGE_TAG \
  -f docker/travis/Dockerfile-host .

docker build --target with-ovscni \
  -t $IMAGE_BUILD_REGISTRY/aci-containers-host-ovscni:$IMAGE_TAG \
  -f docker/travis/Dockerfile-host .

docker build -t $IMAGE_BUILD_REGISTRY/aci-containers-operator:$IMAGE_TAG \
  -f docker/travis/Dockerfile-operator .

docker build -t $IMAGE_BUILD_REGISTRY/aci-containers-webhook:$IMAGE_TAG \
  -f docker/travis/Dockerfile-webhook .

docker build -t $IMAGE_BUILD_REGISTRY/aci-containers-certmanager:$IMAGE_TAG \
  -f docker/travis/Dockerfile-certmanager .
```

**Makefile Targets**:
```bash
make all            # Build all binaries
make all-static     # Build static binaries (for containers)
make check          # Run tests with coverage
make goinstall      # Install Go dependencies
make clean          # Clean build artifacts
make vendor         # Update vendor dependencies
```

---

### 3. acc-provision Repository

**Purpose**: ACI provisioning tool (Python CLI)  
**Language**: Python 3.9  
**Build Tool**: setuptools

#### Artifacts Produced

1. **Python Package** (PyPI)
   - Package name: `acc_provision`
   - Current version: 6.1.1.5 (updated during build)
   - Includes: `acikubectl` binary from aci-containers

#### Local Build Instructions

```bash
# Clone repository
cd acc-provision/provision

# Install in development mode
pip install -e .

# Or build distribution
python3 setup.py sdist bdist_wheel

# Build package with specific version
VERSION=<Release Tag>
sed -i "s/6.1.1.5/${VERSION}/" setup.py
python3 setup.py sdist bdist_wheel

# Test installation
pip install dist/acc_provision-${VERSION}.tar.gz

# Run tool
acc-provision --help
```

**Build Process in CI**:
1. Clone aci-containers repository at same tag
2. Build `acikubectl` binary from aci-containers
3. Copy `acikubectl` to `provision/bin/`
4. Update version in setup.py to match tag
5. Query Quay.io for latest container image tags
6. Update `acc_provision/versions.yaml` with latest tags
7. Build Python package
8. Push to PyPI (production) or test.pypi.org (dev)

**Version Synchronization**: acc-provision queries container registries to find the latest available versions:
```bash
# Example: Find latest acc-provision-operator tag
curl -L -s 'https://quay.io/api/v1/repository/noiro/acc-provision-operator/tag/' | \
  jq -r '.tags[] | select(.name==$ENV.UPSTREAM_IMAGE_Z_TAG) | .manifest_digest'
```

---

### 4. acc-provision-operator Repository

**Purpose**: Kubernetes operator for acc-provision  
**Language**: Ansible + Python  
**Build Tool**: Docker + Operator SDK

#### Container Built

1. **acc-provision-operator**
   - **Base Image**: Ansible Operator base
   - **Purpose**: Watches AccProvisionInput CRD, generates ACI CNI deployment
   - **Includes**: acc-provision Python package (cloned during build)

#### Local Build Instructions

```bash
# Clone repository
cd acc-provision-operator

# Build container
docker build -t myregistry/acc-provision-operator:local .

# Or use Makefile
make docker-build IMG=myregistry/acc-provision-operator:local

# Test with molecule
pip3 install docker molecule openshift jmespath
make test
```

**Build Args**:
- `ACC_PROVISION_BRANCH`: Branch of acc-provision to include (default from Dockerfile)

---

### 5. cicd Repository

**Purpose**: Shared build scripts and utilities  
**Language**: Bash + Python  
**Key Scripts**:

| Script | Purpose |
|--------|---------|
| `check-git-tag.sh` | Validates git tag format and prevents loops |
| `globals.sh` | Global configuration (release tags, registries) |
| `build-push-aci-containers-images.sh` | Builds aci-containers containers |
| `build-push-opflex-images.sh` | Builds opflex containers |
| `build-push-acc-provision-operator-image.sh` | Builds operator container |
| `build-acc-provision.sh` | Builds acc-provision package |
| `push-images.sh` | Pushes images to multiple registries |
| `push-to-cicd-status.sh` | Updates build status repository |
| `push-to-pypi.sh` | Publishes Python package to PyPI |

**Configuration in globals.sh**:
```bash
RELEASE_TAG="<Release Tag>"
UPSTREAM_ID=81c2369
QUAY_REGISTRY=quay.io/noirolabs
QUAY_NOIRO_REGISTRY=quay.io/noiro
DOCKER_REGISTRY=docker.io/noiro
```

---

## Build Timeline & Dependencies

### Dependency Graph

```
                        TIME →
                        
T=0min     T=20min          T=40min             T=60min            T=80min
  │          │                │                   │                  │
  │          │                │                   │                  │
┌─▼──────────▼─┐              │                   │                  │
│   opflex     │              │                   │                   │
│ build-base   │              │                   │                  │
└──────┬───────┘              │                   │                  │
       │                      │                   │                  │
       │ (Image: opflex-build-base:<Release Tag>.81c2369)                 │
       │                      │                   │                  │
   ┌───▼────────────────┐     │                   │                  │
   │  opflex, opflex-   │     │                   │                  │
   │  build containers  │     │                   │                  │
   └───────┬────────────┘     │                   │                  │
           │                  │                   │                  │
           │ (Images: opflex:<Release Tag>.81c2369, opflex-build:<Release Tag>.81c2369)
           │                  │                   │                  │
           │    ┌─────────────▼───────────────┐   │                  │
           │    │  aci-containers (8 images)  │   │                  │
           │    │  - controller                │   │                  │
           │    │  - host / host-ovscni       │   │                  │
           └───▶│  - operator                 │   │                  │
                │  - webhook / certmanager    │   │                  │
                │  - cnideploy                │   │                  │
                │  - openvswitch              │   │                  │
                └──────┬──────────────────────┘   │                  │
                       │                          │                  │
                       │ (All images tagged: <Release Tag>.81c2369)       │
                       │                          │                  │
         ┌─────────────┴────────────┐             │                  │
         │                          │             │                  │
    ┌────▼──────────────┐   ┌───────▼──────────┐ │                  │
    │ acc-provision-    │   │  acc-provision   │ │                  │
    │    operator       │   │   (Python pkg)   │ │                  │
    └───────────────────┘   └──────────────────┘ │                  │
                                                  │                  │
                                    ┌─────────────▼────────────────┐ │
                                    │  cicd-status repo updated    │ │
                                    │  (Build metadata, links)     │ │
                                    └──────────────────────────────┘ │
                                                                     │
                                                   ┌─────────────────▼─┐
                                                   │  Build Complete   │
                                                   └───────────────────┘
```

### Critical Path Analysis

**Sequential Dependencies**:
1. `opflex-build-base` → `opflex` → `aci-containers` → `acc-provision`
2. `aci-containers` → `acc-provision-operator`

**Parallel Opportunities**:
- Once opflex images are complete, both aci-containers AND other independent builds can proceed
- acc-provision-operator and acc-provision can build in parallel (both depend on aci-containers)

**Bottleneck**: aci-containers build (8 containers, ~25-40 minutes)

---

## Container Images Reference

### Production Image Specifications

| Image | Size | Base | Language | Entry Point | Config |
|-------|------|------|----------|-------------|--------|
| opflex-build-base | ~1.2 GB | UBI9 minimal | C++ | - | Build only |
| opflex | ~300 MB | UBI9 minimal | C++ | opflex_agent | /usr/local/etc/opflex-agent-ovs/base-conf.d/ |
| aci-containers-controller | ~450 MB | UBI9 | Go | aci-containers-controller | /usr/local/etc/aci-containers/controller.conf |
| aci-containers-host | ~200 MB | UBI9 minimal | Go | aci-containers-host-agent | /usr/local/etc/aci-containers/host-agent.conf |
| aci-containers-host-ovscni | ~220 MB | UBI9 minimal | Go | aci-containers-host-agent | /usr/local/etc/aci-containers/host-agent.conf |
| aci-containers-operator | ~400 MB | UBI9 | Go | aci-containers-operator | /usr/local/etc/aci-containers/operator.conf |
| aci-containers-webhook | ~180 MB | UBI9 | Go | launch-webhook.sh | - |
| aci-containers-certmanager | ~180 MB | UBI9 | Go | launch-certmanager.sh | - |
| cnideploy | ~150 MB | UBI9 | - | - | Deployment only |
| openvswitch | ~250 MB | UBI9 | C | launch-ovs.sh | - |
| acc-provision-operator | ~500 MB | Ansible Operator | Ansible/Python | ansible-operator | watches.yaml |

### Image Tagging Strategy

**Tag Format**: `{RELEASE}.{UPSTREAM_ID}[.{SUFFIX}]`

**Examples**:
- `<Release Tag>.81c2369` - Standard release tag
- `<Release Tag>.81c2369.110425.12345` - With date and build number
- `<Release Tag>.81c2369.z` - Z-stream (latest stable)
- `<Release Tag>.81c2369.rc1` - Release candidate

**Registry Distribution**:
```
quay.io/noirolabs/{image}:{tag}      ← Primary (built by Travis)
quay.io/noiro/{image}:{tag}          ← Mirror with extended tags
docker.io/noiro/{image}:{tag}        ← Public mirror
```

---

## Local Development Builds

### Prerequisites

**Required Tools**:
- Docker (with buildx support)
- Go 1.25.0 (for aci-containers)
- Python 3.9+ (for acc-provision)
- Maven (for opflex Genie)
- Git

**Optional Tools**:
- kind/minikube (for local Kubernetes testing)
- make
- jq (for API queries)

### Quick Start: Build All Containers Locally

```bash
#!/bin/bash
# Complete local build script

set -e

REGISTRY="localhost:5000"  # Or your local registry
TAG="dev-$(date +%Y%m%d)"

# 1. Build opflex
echo "Building opflex..."
cd /path/to/opflex
./docker/travis/build-opflex-travis.sh $REGISTRY $TAG

# 2. Build aci-containers
echo "Building aci-containers..."
cd /path/to/aci-containers

# Copy iptables from opflex-build-base
docker/copy_iptables.sh $REGISTRY/opflex-build-base:$TAG dist-static

# Build binaries
make all-static

# Build containers
export DOCKER_BUILDKIT=1
docker build -t $REGISTRY/cnideploy:$TAG -f docker/travis/Dockerfile-cnideploy .
docker build -t $REGISTRY/aci-containers-controller:$TAG -f docker/travis/Dockerfile-controller .
docker build --target without-ovscni -t $REGISTRY/aci-containers-host:$TAG -f docker/travis/Dockerfile-host .
docker build --target with-ovscni -t $REGISTRY/aci-containers-host-ovscni:$TAG -f docker/travis/Dockerfile-host .
docker build -t $REGISTRY/aci-containers-operator:$TAG -f docker/travis/Dockerfile-operator .
docker build -t $REGISTRY/aci-containers-webhook:$TAG -f docker/travis/Dockerfile-webhook .
docker build -t $REGISTRY/aci-containers-certmanager:$TAG -f docker/travis/Dockerfile-certmanager .

# Build OpenVSwitch
./docker/travis/build-openvswitch-travis.sh $REGISTRY $TAG

# 3. Build acc-provision-operator
echo "Building acc-provision-operator..."
cd /path/to/acc-provision-operator
docker build -t $REGISTRY/acc-provision-operator:$TAG .

# 4. Build acc-provision package
echo "Building acc-provision..."
cd /path/to/acc-provision/provision
pip install -e .

echo "All builds complete!"
echo "Images tagged with: $TAG"
```

### Testing Locally Built Images

**Option 1: Docker Compose**
```yaml
# docker-compose-local.yml
version: '3.8'
services:
  controller:
    image: localhost:5000/aci-containers-controller:dev-20251104
    volumes:
      - ./controller.conf:/usr/local/etc/aci-containers/controller.conf
    ports:
      - "8091:8091"
```

**Option 2: Kind Cluster**
```bash
# Create kind cluster with local registry
./scripts/create-kind-with-registry.sh

# Load images into kind
kind load docker-image localhost:5000/aci-containers-controller:dev-20251104

# Deploy using acc-provision
acc-provision -c input.yaml -f kubernetes-1.29 -o aci_deployment.yaml \
  --version-token dev-20251104

kubectl apply -f aci_deployment.yaml
```

### Debugging Build Issues

**Enable verbose output**:
```bash
export DOCKER_BUILDKIT=1
export BUILDKIT_PROGRESS=plain
docker build --progress=plain -t myimage:test .
```

**Check intermediate layers**:
```bash
# Build without removing intermediate containers
docker build --rm=false -t myimage:test .

# List all containers including stopped
docker ps -a

# Commit intermediate state
docker commit <container-id> debug-image:test
docker run -it debug-image:test /bin/bash
```

**Common Issues**:

| Issue | Cause | Solution |
|-------|-------|----------|
| `opflex-build-base not found` | Missing base image | Build opflex first with special tag |
| `Go build fails` | Go version mismatch | Use Go 1.25.0 exactly |
| `iptables copy fails` | Base image not available | Pull from quay.io or build locally |
| `Maven fails in genie` | Java version | Use Java 11 |
| `OVS build hangs` | Docker daemon busy | Increase Docker resources |

---

## Troubleshooting

### CI/CD Pipeline Issues

#### Build Not Triggered
**Symptoms**: PR merged but no Travis build started

**Checklist**:
1. **Check Jenkins job status** - Did the freestyle job run successfully?
2. **Check Jenkins console output** - Look for `push-git-tag.sh` execution
3. **Verify Jenkins has access to cicd repository** - Script must be available
4. **Check environment variables in Jenkins**:
   - `$RELEASE_TAG` - Must be set (e.g., <Release Tag>)
   - `$TRAVIS_TAG` - Must be set (e.g., <Release Tag>.81c2369)
   - `$TRAVIS_TAGGER` - GitHub token must be valid
5. **Check GitHub** - Is the tag visible? (`git ls-remote --tags`)
6. **Check Travis dashboard** - Any webhook failures?

**Common Causes**:
- Jenkins job disabled or not configured
- `$TRAVIS_TAGGER` token expired or invalid
- cicd repository not accessible to Jenkins
- Tag format doesn't match expectations in globals.sh
- GitHub webhook to Travis CI not configured
- Network issues between Jenkins and GitHub

**Debug Commands**:
```bash
# From Jenkins job console, verify script execution:
# Should see output like:
git tag -f -a <Release Tag>.81c2369 -m "ACI Release <Release Tag> Created by Travis Job ..."
git push travis-tagger -f --tags

# Verify tag was created:
git ls-remote --tags https://github.com/noironetworks/<repo>.git | grep <Release Tag>

# Check tag message:
git show -s --format=%B <Release Tag>.81c2369
```

#### Build Fails at Tag Validation
**Symptoms**: Travis job exits with code 140 or fails at check-git-tag.sh

**Debug**:
```bash
# Check tag format
git describe --tags
# Should match: <Release Tag>.*

# Check tag message
git show -s --format=%B <tag>
# Should NOT contain "Created by Travis Job"

# Check tag prefix in globals.sh
grep RELEASE_TAG cicd/travis/globals.sh
```

#### Container Build Fails
**Symptoms**: Docker build exits with error

**Debug Steps**:
```bash
# 1. Check base image availability
docker pull registry.access.redhat.com/ubi9/ubi:latest

# 2. Check upstream images
docker pull quay.io/noirolabs/opflex-build-base:<Release Tag>.81c2369.z

# 3. Check disk space
df -h
docker system df

# 4. Enable debug mode
export DOCKER_BUILDKIT=1
export BUILDKIT_PROGRESS=plain
```

#### Registry Push Fails
**Symptoms**: Build succeeds but push fails

**Common Issues**:
- Authentication expired (check `$DOCKER_PASSWORD`, `$DOCKER_USERNAME`)
- Rate limiting (wait and retry)
- Network issues (check Travis network status)
- Registry storage full (contact registry admin)

**Verify Manually**:
```bash
# Test registry access
docker login quay.io/noirolabs
docker push quay.io/noirolabs/test:check

# Check rate limits
curl -I https://quay.io/v2/
```

### Local Development Issues

#### Go Build Fails
```bash
# Verify Go version
go version  # Should be 1.25.0

# Clean module cache
go clean -modcache

# Re-download dependencies
go mod download
go mod verify

# Check for replace directives
grep replace go.mod
```

#### Python Package Build Fails
```bash
# Check Python version
python3 --version  # Should be 3.9+

# Verify setup.py
python3 setup.py check

# Install build dependencies
pip install setuptools wheel twine

# Clean build artifacts
rm -rf build/ dist/ *.egg-info/
```

#### Maven Build Fails (Genie)
```bash
# Check Java version
java -version  # Should be Java 11

# Clean Maven cache
mvn clean

# Update dependencies
mvn dependency:resolve

# Build with debug
mvn -X compile exec:java
```

---

## Appendix

### A. Environment Variables Reference

| Variable | Default | Purpose | Used By |
|----------|---------|---------|---------|
| `RELEASE_TAG` | <Release Tag> | Current release version | All |
| `UPSTREAM_ID` | 81c2369 | Upstream commit ID | All |
| `DOCKER_HUB_ID` | - | Registry prefix | Build scripts |
| `DOCKER_TAG` | - | Image tag | Build scripts |
| `DOCKER_BUILDKIT` | 1 | Enable BuildKit | Docker |
| `TRAVIS_TAG` | - | Git tag that triggered build | Travis CI / Jenkins |
| `TRAVIS_BUILD_NUMBER` | - | Travis build ID | Travis CI |
| `TRAVIS_REPO_SLUG` | - | Repository name | Travis CI |
| `TRAVIS_JOB_NUMBER` | - | Jenkins job number (for tag message) | Jenkins |
| `TRAVIS_JOB_WEB_URL` | - | Jenkins job URL (for tag message) | Jenkins |
| `TRAVIS_TAGGER` | - | GitHub token for tag creation | Jenkins |
| `QUAY_REGISTRY` | quay.io/noirolabs | Primary registry | Push scripts |
| `DOCKER_USERNAME` | - | Registry auth username | Travis CI |
| `DOCKER_PASSWORD` | - | Registry auth password | Travis CI |

### B. File Locations Reference

| Purpose | Path | Repository |
|---------|------|------------|
| Travis config | `.travis.yml` | Each repo |
| Container definitions | `docker/travis/Dockerfile-*` | aci-containers, opflex |
| Build scripts | `docker/travis/build-*.sh` | aci-containers, opflex |
| Global config | `cicd/travis/globals.sh` | cicd |
| Tag validation | `cicd/travis/check-git-tag.sh` | cicd |
| Tag creation | `cicd/travis/push-git-tag.sh` | cicd |
| Push logic | `cicd/travis/push-images.sh` | cicd |
| Status updates | `cicd/travis/push-to-cicd-status.sh` | cicd |
| Version mapping | `provision/acc_provision/versions.yaml` | acc-provision |
| Python package | `provision/setup.py` | acc-provision |
| Operator manifest | `acc-provision-operator/watches.yaml` | acc-provision-operator |

### C. Registry & Repository URLs

**Container Registries**:
- Primary: https://quay.io/organization/noirolabs
- Mirror: https://quay.io/organization/noiro
- Public: https://hub.docker.com/u/noiro

**Source Repositories**:
- aci-containers: https://github.com/noironetworks/aci-containers
- opflex: https://github.com/noironetworks/opflex
- acc-provision: https://github.com/noironetworks/acc-provision
- acc-provision-operator: https://github.com/noironetworks/acc-provision-operator
- cicd: https://github.com/noironetworks/cicd

**Status & Documentation**:
- Build Status: https://github.com/noironetworks/cicd-status
- PyPI Package: https://pypi.org/project/acc-provision/

**Jenkins Configuration**:
- Jenkins freestyle jobs are configured in Jenkins UI (not in source repositories)
- Each repository has a corresponding Jenkins job that:
  - Monitors GitHub for merged PRs
  - Clones the `cicd` repository
  - Executes `cicd/travis/push-git-tag.sh <repo-name>`
  - Requires environment variables: `$RELEASE_TAG`, `$TRAVIS_TAG`, `$TRAVIS_TAGGER`


**For Build Issues**:
1. Check Travis CI build logs
2. Check Jenkins job console
3. Review cicd-status repository for recent changes
4. Verify mismatch between update-cve and travis builds

**For Container Issues**:
1. Check container logs: `docker logs <container>`
2. Inspect image: `docker inspect <image>:<tag>`
3. Verify configuration files
4. Check Kubernetes events: `kubectl get events`

### E. Release Checklist

**Pre-Release**:


**Release**:
- [ ] Create release via acc-provision pypi
- [ ] Jenkins creates git tags on all repositories
- [ ] Monitor Travis builds for all repositories
- [ ] Verify all images pushed to registries
- [ ] Verify acc-provision published to PyPI
- [ ] Check cicd-status updated

**Post-Release**:
- [ ] Test deployment with new image
- [ ] Update `RELEASE_TAG` in `cicd/travis/globals.sh`
- [ ] Update cicd release
- [ ] Update version in `acc-provision`



---
