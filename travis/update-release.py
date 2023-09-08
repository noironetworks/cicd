from datetime import datetime
import os
import sys
import yaml
import pytz
import shutil
import subprocess


def get_image_sha(image_name_and_tag):
    try:
        # Run the 'docker image inspect' command as a subprocess
        result = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{index (split (index .RepoDigests 0) "@sha256:") 1}}', image_name_and_tag], universal_newlines=True)

        # 'result' now contains the output of the command, which includes the SHA digest
        sha_digest = result.strip()  # Remove any leading/trailing whitespaces or newlines

        return sha_digest
    except subprocess.CalledProcessError as e:
        # Handle any errors that occur during the subprocess execution
        print("Error:", e)
        return ""

def count_severity(filepath):
    filepath = "/tmp/" + GIT_LOCAL_DIR + "/docs/" + filepath
    print("count_severity file: " + filepath)
    if os.path.exists(filepath):
        with open(filepath, "r") as file:
            data = file.read()

        lines = data.strip().split('\n')

        severity_list = []

        # Loop through each line (ignoring the first line) and extract the SEVERITY value
        for line in lines[1:]:
            columns = line.split()
            severity = columns[-1]
            severity_list.append(severity)

        # Count occurrences of "Citical", "High", "Medium, "Low", "Unknown"
        critical_count = severity_list.count("Critical")
        high_count = severity_list.count("High")
        medium_count = severity_list.count("Medium")
        low_count = severity_list.count("Low")
        unknown_count = severity_list.count("Unknown")

        result = [
            {
                "C": critical_count,
                "H": high_count,
                "M": medium_count,
                "L": low_count,
                "U": unknown_count
            }
        ]
        print("count_severity result: " + str(result))
        # Return the results
        return result

    return []

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

def get_container_images_data(r_stream):
    for r in r_stream:
        if r["release_name"].endswith(".z"):
            z_container_images = r["container_images"]
    c_imagges = []
    print("TRAVIS_TAG_WITH_UPSTREAM_ID: " + TRAVIS_TAG_WITH_UPSTREAM_ID)
    for image in z_container_images:
        # setup container images
        quaySha = get_image_sha("quay.io/noiro/" + image["name"] + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID)
        # lookup image sha
        dockerSha = get_image_sha("noiro/" + image["name"] + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID)
        # copy dir /z/image_name to /r/image_name
        copyfile("/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/" + RELEASE_TAG + "/z/" + image["name"], "/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/" + RELEASE_TAG + "/r/" + image["name"])
        image_update = {
            "name": image["name"],
            "commit": image["commit"],
            "quay": [
                {
                "tag": TRAVIS_TAG_WITH_UPSTREAM_ID,
                "sha": quaySha,
                "link": "https://quay.io/noiro/" + image["name"] + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID
                },
            ],
            "docker": [
                {
                "tag": TRAVIS_TAG_WITH_UPSTREAM_ID,
                "sha": dockerSha,
                "link": "https://hub.docker.com/layers/noiro/" + image["name"] + "/" + TRAVIS_TAG_WITH_UPSTREAM_ID + "/images/sha256-" + get_image_sha("noiro/" + image["name"] + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID) + "?context=explore"
                },
            ],
            "base-image": [
                {
                "sha": image["base-image"][0]["sha"],
                "cve": "release_artifacts/" + RELEASE_TAG + "/r/" + image["name"] + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                "severity": count_severity("release_artifacts/" + RELEASE_TAG + "/r/" + image["name"] + "/" + RELEASE_TAG + "-" + "cve-base.txt")
                },
            ],
            "sbom": "release_artifacts/" + RELEASE_TAG + "/r/" + image["name"] + "/" + RELEASE_TAG + "-" + "sbom.txt",
            "cve": "release_artifacts/" + RELEASE_TAG + "/r/" + image["name"] + "/" + RELEASE_TAG + "-" + "cve.txt",
            "build-logs": "release_artifacts/" + RELEASE_TAG + "/r/" + image["name"] + "/" + RELEASE_TAG + "-" + "buildlog.txt",
            "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "severity": count_severity("release_artifacts/" + RELEASE_TAG + "/r/" + image["name"] + "/" + RELEASE_TAG + "-" + "cve.txt")
        }
        c_imagges.append(image_update)
    return c_imagges


    

GIT_LOCAL_DIR = "cicd-status"
RELEASE_TAG = os.environ.get("RELEASE_TAG")
Z_RELEASE_TAG = RELEASE_TAG + ".z"
TRAVIS_TAG_WITH_UPSTREAM_ID = os.environ.get("TRAVIS_TAG") + "." + os.environ.get("UPSTREAM_ID")
print("TRAVIS_TAG_WITH_UPSTREAM_ID: " + TRAVIS_TAG_WITH_UPSTREAM_ID)
release_filepath = "/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/releases.yaml"

release_tag_exists = False
yaml_data = None

# Get the timezone for Pacific Time
pacific_time = pytz.timezone('US/Pacific')

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
                                    {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                    "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + IMAGE_Z_TAG},
                                    {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                    "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER},
                                ],
                                "docker": [
                                    {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                    "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + IMAGE_Z_TAG +
                                            "/images/sha256-" + get_image_sha(
                                        "noiro/" + IMAGE + ":" + IMAGE_Z_TAG) + "?context=explore"},
                                    {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                    "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER +
                                            "/images/sha256-" + get_image_sha(
                                        "noiro/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER) + "?context=explore"},
                                ],
                                "base-image": [
                                    {"sha": get_image_sha(BASE_IMAGE),
                                    "cve": "release_artifacts/" + RELEASE_TAG + "/z/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                                    "severity": count_severity("release_artifacts/" + RELEASE_TAG + "/z/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt")
                                    },
                                ],
                                "sbom": "release_artifacts/" + RELEASE_TAG + "/z/" + IMAGE + "/" + RELEASE_TAG + "-" + "sbom.txt",
                                "cve": "release_artifacts/" + RELEASE_TAG + "/z/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt",
                                "build-logs": "release_artifacts/" + RELEASE_TAG + "/z/" + IMAGE + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                                "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                                "severity": count_severity("release_artifacts/" + RELEASE_TAG + "/z/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt")
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
            
            search_stream = RELEASE_TAG
            if IS_RELEASE == "false":
                search_stream = Z_RELEASE_TAG
            print("search_stream: " + search_stream)
            print("IS_RELEASE: " + IS_RELEASE)
            acc_provision_update = []
            if IS_RELEASE == "true":
                acc_provision_update = [
                            {
                                "link": PYPI_REGISTRY,
                                "tag": TAG_NAME,
                                "commit": [{"link": "https://github.com/" + os.environ.get(
                                    "TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"),
                                            "sha": os.environ.get("TRAVIS_COMMIT")}],
                                "build-logs": "release_artifacts/" + RELEASE_TAG + "/r/" + "acc-provision" + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                                "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                            }
                        ]
            elif IS_RELEASE == "false":
                acc_provision_update = [
                            {
                                "link": PYPI_REGISTRY,
                                "tag": TAG_NAME,
                                "commit": [{"link": "https://github.com/" + os.environ.get(
                                    "TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"),
                                            "sha": os.environ.get("TRAVIS_COMMIT")}],
                                "build-logs": "release_artifacts/" + RELEASE_TAG + "/z/" + "acc-provision" + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                                "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                            }
                        ]
            for release_stream_idx, release_stream in enumerate(release["release_streams"]):
                if release_stream["release_name"] == search_stream:
                    yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["last_updated"] = datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z")
                    yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["acc_provision"] = acc_provision_update
                    if IS_RELEASE == "true":
                        yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["released"] = True
                        yaml_data["releases"][release_idx]["release_streams"][release_stream_idx]["container_images"] = get_container_images_data(release["release_streams"])


# Write the updated YAML data back to release.yaml
with open(release_filepath, "w") as file:
    yaml.dump(yaml_data, file, default_flow_style=False)