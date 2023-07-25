#!/bin/bash
set -x

# Get the current branch name
current_branch=$(git rev-parse --abbrev-ref HEAD)

# Set the desired commit count and threshold count
desired_commit_count=100
threshold_count=200
GIT_REPO="https://github.com/noironetworks/cicd_status.git"
GIT_LOCAL_DIR="cicd_status"
GIT_BRANCH="main"
GIT_EMAIL=""
GIT_TOKEN=""
GIT_USER=""

gitclonerepo() {
    cd /tmp/

    git clone "${GIT_REPO/:\/\//://$GIT_USER:$GIT_TOKEN@}" "${GIT_LOCAL_DIR}" 
    cd "${GIT_LOCAL_DIR%/*}" || exit
    git remote set-url origin "${GIT_REPO/:\/\//://$GIT_USER:$GIT_TOKEN@}" 
    git checkout "${GIT_BRANCH}"
}


addartifacts() {
    cd /tmp/"${GIT_LOCAL_DIR%/*}/docs/" || exit
    #release-name
    #image-name
    #tags - quay/docker
    #sbom - push path to sbom files in artifacts - genrate via third-party tool
    #cve  -
    #build-logs - /home/travis/build/<github-username>/<repository-name>/build
    #put it in release.yaml
}

gitAddCommitPush() {
    cd /tmp/"${GIT_LOCAL_DIR%/*}" || exit
    git config --local user.email "${GIT_EMAIL}"
    git config --local user.name "${GIT_USER}"
    git pull --rebase origin ${GIT_BRANCH}
# Check if the current branch has more commits than the desired count
    commit_count=$(git rev-list --count HEAD)
    if [ $commit_count -gt $threshold_count ]; then
      echo "Commit count exceeds $threshold_count. Squashing commits..."
      commit=$(git rev-list HEAD | tail -n $(expr $commit_count - $desired_commit_count) | head -n 1)
      intialbranch=$(git rev-list --reverse HEAD | head -n 1)

      # Include new files to be merged
      git stash save --include-untracked

      # Checkout the parent of the starting commit
      git checkout "$intialbranch"

      # Merge the 100 commits into its parent, keeping the commit message of the parent
      git merge --squash "$commit"
      git add .
      git commit --amend --no-edit

      # Store the current commit
      newcommit=$(git rev-parse HEAD)

      # Rebase the starting branch onto the new commit
      git checkout "${GIT_BRANCH}"

      # Rebasing the commits
      git rebase --onto "$newcommit" "$commit" "${GIT_BRANCH}"

      # Push the changes to the remote branch
      git push origin ${GIT_BRANCH} --force

      # Stash apply new files
      git stash apply

    else
      git add .
      git commit -a -m "Update at time:$( date '+%F_%H:%M:%S' )"
      git push origin ${GIT_BRANCH}
    fi
    cd -
}


gitclonerepo 

addartifacts

gitAddCommitPush
