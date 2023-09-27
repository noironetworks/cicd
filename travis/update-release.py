from datetime import datetime
import os
import re
import sys
import shutil
import subprocess
import yaml
import pytz

# Constants
GIT_LOCAL_DIR = "cicd-status"
RELEASE_TAG = os.environ.get("RELEASE_TAG")
Z_RELEASE_TAG = RELEASE_TAG + ".z"
TRAVIS_TAG= os.environ.get("TRAVIS_TAG")
TRAVIS_TAG_WITH_UPSTREAM_ID = TRAVIS_TAG + "." + os.environ.get("UPSTREAM_ID")
RC_REGEX = RELEASE_TAG + "rc" + r"[0-9]+"
IS_RC_RELEASE = bool(re.match(RC_REGEX, TRAVIS_TAG))
RC_NUM = TRAVIS_TAG.replace(RELEASE_TAG + "rc", "", 1)
RC_RELEASE_TAG = RELEASE_TAG + ".rc" + RC_NUM
RC_IMAGE_TAG = RELEASE_TAG + "." + os.environ.get("UPSTREAM_ID") + ".rc" + RC_NUM
DIR = "/z/"

# Get the timezone for Pacific Time
pacific_time = pytz.timezone('US/Pacific')


def pull_image_and_get_sha(image_name_and_tag):
    try:
        subprocess.check_output(['docker', 'pull', image_name_and_tag], universal_newlines=True)      
    except subprocess.CalledProcessError as e:
        print("Error:", e)
        return "error"
    return get_repo_digest_sha(image_name_and_tag)

def get_repo_digest_sha(image_name_and_tag):
    try:
        result = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{index (split (index .RepoDigests 0) "@sha256:") 1}}', image_name_and_tag], universal_newlines=True)
    except subprocess.CalledProcessError as e:
        # Handle any errors that occur during the subprocess execution
        print("Error:", e)
        return "error"
    return result.strip()

def count_severity(filepath):
    filepath = "/tmp/" + GIT_LOCAL_DIR + "/docs/" + filepath
    
    severity_list = []

    if os.path.exists(filepath):
        with open(filepath, "r") as file:
            data = file.read()

        lines = data.strip().split('\n')

        # Accepted severity values
        accepted_severities = ["Critical", "High", "Medium", "Low", "Unknown"]

        try:
            # Loop through each line (ignoring the first line) and extract the SEVERITY value
            for line in lines[1:]:
                columns = line.split()
                severity = columns[-1]

                if severity not in accepted_severities:
                    print(f"Unexpected severity value found: {severity}")
                    break

                severity_list.append(severity)

        except IndexError:
            print(f"IndexError encountered for line: {str(IndexError)}")
    
    result = [
        {
            "C": severity_list.count("Critical"),
            "H": severity_list.count("High"),
            "M": severity_list.count("Medium"),
            "L": severity_list.count("Low"),
            "U": severity_list.count("Unknown")
        }
    ]
    # Return the results
    return result


def copyfile(src, dst):
    # Check if dst exists, if not, create it
    if not os.path.exists(dst):
        os.makedirs(dst)

    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, False, None)
        else:
            shutil.copy2(s, d)

def get_container_images_data(r_stream,tag):
    z_container_images = {}
    c_images = []
    for r in r_stream:
        if r["release_name"].endswith(".z"):
            z_container_images = r["container_images"]
            break

    for image in z_container_images:
        # lookup image sha's
        quaySha = pull_image_and_get_sha("quay.io/noiro/" + image["name"] + ":" + tag)
        # lookup image sha
        dockerSha = pull_image_and_get_sha("noiro/" + image["name"] + ":" + tag)

        copyfile("/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/" + RELEASE_TAG + "/z/" + image["name"], "/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/" + RELEASE_TAG + DIR + image["name"])
        image_update = {
            "name": image["name"],
            "commit": image["commit"],
            "quay": [
                {
                "tag": tag,
                "sha": quaySha,
                "link": "https://quay.io/noiro/" + image["name"] + ":" + tag
                },
            ],
            "docker": [
                {
                "tag": tag,
                "sha": dockerSha,
                "link": "https://hub.docker.com/layers/noiro/" + image["name"] + "/" + tag + "/images/sha256-" + dockerSha + "?context=explore"
                },
            ],
            "base-image": [
                {
                "sha": image["base-image"][0]["sha"],
                "cve": "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                "severity": count_severity("release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "cve-base.txt")
                },
            ],
            "sbom": "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "sbom.txt",
            "cve": "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "cve.txt",
            "build-logs": "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "buildlog.txt",
            "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "severity": count_severity("release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "cve.txt")
        }
        c_images.append(image_update)

    return c_images

def check_rollback_artifacts(r_stream,tag):
    z_container_images = {}
    rollback = False
    c_images = []

    for r in r_stream:
        if r["release_name"].endswith(".z"):
            z_container_images = r["container_images"]
            break

    for image in z_container_images:
        # lookup image sha
        quaySha = pull_image_and_get_sha("quay.io/noiro/" + image["name"] + ":" + tag)
        print(quaySha, image["quay"][0]["sha"])
        # Check if dockersha pulled matches with z tag sha if not do a rollback with dockersha to commit id
        if quaySha != image["quay"][0]["sha"]:
            # do rollback and update release.yaml with the dockersha you recieved
            print("Mismatch happened")
            rollback = True
            break

    if rollback:
        for image in z_container_images:
            # lookup image sha's
            print("Rolling back ",image["name"])
            quaySha = pull_image_and_get_sha("quay.io/noiro/" + image["name"] + ":" + tag)
            # lookup image sha
            dockerSha = pull_image_and_get_sha("noiro/" + image["name"] + ":" + tag)

            current_directory = os.getcwd()

            # Change the working directory temporarily
            os.chdir("/tmp/" + GIT_LOCAL_DIR)

            # Command 1: git stash
            subprocess.run(["git", "stash"], universal_newlines=True)

            # Command 2: git log --grep=xyz --format=%H
            git_log_command = ["git", "log", "--grep=" + quaySha, "--format=%H"]
            commit_hash = subprocess.check_output(git_log_command, universal_newlines=True).strip()

            # Command 3: git checkout abcd
            subprocess.run(["git", "checkout", commit_hash], universal_newlines=True)

            # Command 4: copy some files to a destination (e.g., using shutil or any other method)
            copyfile("docs/release_artifacts/" + RELEASE_TAG + "/z/" + image["name"],
                            "/tmp/z/"+image["name"])

            # Command 5: git checkout main
            subprocess.run(["git","checkout", "main"], universal_newlines=True)

            # Command 6: git stash pop
            subprocess.run(["git","stash", "pop"], universal_newlines=True)

            copyfile("/tmp/z/" + image["name"], "docs/release_artifacts/" + RELEASE_TAG + DIR + image["name"])

            # Return to the original working directory
            os.chdir(current_directory)

            image_update = {
                "name": image["name"],
                "commit": image["commit"],
                "quay": [
                    {
                        "tag": tag,
                        "sha": quaySha,
                        "link": "https://quay.io/noiro/" + image["name"] + ":" + tag
                    },
                ],
                "docker": [
                    {
                        "tag": tag,
                        "sha": dockerSha,
                        "link": "https://hub.docker.com/layers/noiro/" + image[
                            "name"] + "/" + tag + "/images/sha256-" + dockerSha + "?context=explore"
                    },
                ],
                "base-image": [
                    {
                        "sha": image["base-image"][0]["sha"],
                        "cve": "release_artifacts/" + RELEASE_TAG + DIR + image[
                            "name"] + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                        "severity": count_severity("release_artifacts/" + RELEASE_TAG + DIR + image[
                            "name"] + "/" + RELEASE_TAG + "-" + "cve-base.txt")
                    },
                ],
                "sbom": "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "sbom.txt",
                "cve": "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "cve.txt",
                "build-logs": "release_artifacts/" + RELEASE_TAG + DIR + image[
                    "name"] + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "severity": count_severity(
                    "release_artifacts/" + RELEASE_TAG + DIR + image["name"] + "/" + RELEASE_TAG + "-" + "cve.txt")
            }

            c_images.append(image_update)

        return c_images, True

    return c_images, False


if IS_RC_RELEASE:
    DIR = "/rc" + RC_NUM + "/"
release_filepath = "/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/releases.yaml"

release_tag_exists = False
yaml_data = None


if not os.path.exists(release_filepath):
    with open(release_filepath, 'w'):
        pass

with open(release_filepath, "r") as file:
    try:
        yaml_data = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        print(exc)
        sys.exit(1)

    # Check if yaml_data is not None before accessing its keys
    if yaml_data is None:
        yaml_data = {"releases": []}

    if "releases" not in yaml_data:
        yaml_data["releases"] = []

for release in yaml_data["releases"]:
    if release.get("release_tag") == RELEASE_TAG:
        release_tag_exists = True
        break      

if not release_tag_exists:
    new_release_tag = {
            "release_tag": RELEASE_TAG,
            "release_streams": [
                {
                    "release_name": Z_RELEASE_TAG,
                    "last_updated": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "container_images": [],
                    "acc_provision": []
                },
                {
                    "release_name": RELEASE_TAG,
                    "last_updated": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "container_images": [],
                    "acc_provision": [],
                    "released": False
                }
            ]
        }
    yaml_data["releases"].append(new_release_tag)

for release_idx, release in enumerate(yaml_data["releases"]):
    if release["release_tag"] == RELEASE_TAG:
        # Check if all required command-line arguments are provided
        if "acc-provision" != os.environ.get("TRAVIS_REPO_SLUG").split("/")[1] :

            if len(sys.argv) != 9:
                print("Usage: python update-release.py IMAGE_BUILD_REGISTRY IMAGE IMAGE_BUILD_TAG OTHER_IMAGE_TAGS IMAGE_SHA IMAGE_Z_TAG TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER BASE_IMAGE")
                sys.exit(1)

            # Get the command-line arguments
            IMAGE_BUILD_REGISTRY = sys.argv[1]
            IMAGE = sys.argv[2]
            IMAGE_BUILD_TAG = sys.argv[3]
            OTHER_IMAGE_TAGS = sys.argv[4]
            IMAGE_SHA = sys.argv[5]
            IMAGE_Z_TAG = sys.argv[6]
            TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER = sys.argv[7]
            BASE_IMAGE = sys.argv[8]

            image_update = {
                                "name": IMAGE,
                                "commit": [{"link": "https://github.com/"+ os.environ.get("TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"), "sha":os.environ.get("TRAVIS_COMMIT")}],
                                "quay": [
                                    {"tag": IMAGE_Z_TAG, "sha": pull_image_and_get_sha(
                                        "quay.io/noiro/" + IMAGE + ":" + IMAGE_Z_TAG),
                                    "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + IMAGE_Z_TAG},
                                    {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": pull_image_and_get_sha(
                                        "quay.io/noiro/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER),
                                    "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER},
                                ],
                                "docker": [
                                    {"tag": IMAGE_Z_TAG, "sha": pull_image_and_get_sha(
                                        "noiro/" + IMAGE + ":" + IMAGE_Z_TAG),
                                    "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + IMAGE_Z_TAG +
                                            "/images/sha256-" + pull_image_and_get_sha(
                                        "noiro/" + IMAGE + ":" + IMAGE_Z_TAG) + "?context=explore"},
                                    {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": pull_image_and_get_sha(
                                        "noiro/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER),
                                    "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER +
                                            "/images/sha256-" + pull_image_and_get_sha(
                                        "noiro/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER) + "?context=explore"},
                                ],
                                "base-image": [
                                    {"sha": get_repo_digest_sha(BASE_IMAGE),
                                    "cve": "release_artifacts/" + RELEASE_TAG + DIR + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                                    "severity": count_severity("release_artifacts/" + RELEASE_TAG + DIR + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt")
                                    },
                                ],
                                "sbom": "release_artifacts/" + RELEASE_TAG + DIR + IMAGE + "/" + RELEASE_TAG + "-" + "sbom.txt",
                                "cve": "release_artifacts/" + RELEASE_TAG + DIR + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt",
                                "build-logs": "release_artifacts/" + RELEASE_TAG + DIR + IMAGE + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                                "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                                "severity": count_severity("release_artifacts/" + RELEASE_TAG + DIR + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt")
                            }

            for release_stream_idx, release_stream in enumerate(release["release_streams"]):
                
                if release_stream["release_name"] == Z_RELEASE_TAG:
                    yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["last_updated"] = datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z")
                    if len(yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"]) == 0:
                        yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"].append(image_update)
                    else:
                        image_exists = False
                        for image_idx, image in enumerate(release_stream["container_images"]):
                            if image["name"] == IMAGE:
                                image_exists = True
                                yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"][image_idx] = image_update
                                break
                        if not image_exists:
                            yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"].append(image_update)
        # acc-provision
        else:
            if len(sys.argv) != 4:
                print("Usage: python update-release.py PYPI_REGISTRY TAG_NAME IS_RELEASE")
                sys.exit(1)

            PYPI_REGISTRY = sys.argv[1]
            TAG_NAME = sys.argv[2]
            IS_RELEASE = sys.argv[3]
            
            search_stream = Z_RELEASE_TAG
            if IS_RELEASE == "true":
                search_stream = RELEASE_TAG
                DIR = "/r/"
            elif IS_RC_RELEASE:
                search_stream = RC_RELEASE_TAG
                RC_RELEASE_EXISTS = False
                for release_stream in release["release_streams"]:
                    if release_stream["release_name"] == search_stream:
                        RC_RELEASE_EXISTS = True
                        break
                if not RC_RELEASE_EXISTS:
                    release_stream = {
                        "release_name": search_stream,
                        "last_updated": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "container_images": [],
                        "acc_provision": [],
                    }
                    yaml_data["releases"][release_idx]["release_streams"].append(release_stream)
            acc_provision_update = [
                        {
                            "link": PYPI_REGISTRY,
                            "tag": TAG_NAME,
                            "commit": [{"link": "https://github.com/" + os.environ.get(
                                "TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"),
                                        "sha": os.environ.get("TRAVIS_COMMIT")}],
                            "build-logs": "release_artifacts/" + RELEASE_TAG + DIR + "acc-provision" + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                            "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        }
                    ]
            for release_stream_idx, release_stream in enumerate(release["release_streams"]):
                if release_stream["release_name"] == search_stream:
                    yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["last_updated"] = datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z")
                    yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["acc_provision"] = acc_provision_update
                    TG=''
                    if IS_RELEASE == "true":
                        yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["released"] = True
                        TG=TRAVIS_TAG_WITH_UPSTREAM_ID
                    elif IS_RC_RELEASE:
                        TG=RC_IMAGE_TAG
                    c_images , is_valid = check_rollback_artifacts(release["release_streams"],TG)
                    if is_valid:
                        yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"] = c_images
                    else:
                        yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"] = get_container_images_data(release["release_streams"], TG)
                    break

# Write the updated YAML data back to release.yaml
with open(release_filepath, "w") as file:
    yaml.dump(yaml_data, file, default_flow_style=False)