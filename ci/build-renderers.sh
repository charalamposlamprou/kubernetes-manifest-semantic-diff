#!/usr/bin/env bash
set -euo pipefail
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
cd "$stage"
curl --fail --silent --show-error --location --retry 3 -o helm.tgz https://get.helm.sh/helm-v4.1.1-linux-amd64.tar.gz
curl --fail --silent --show-error --location --retry 3 -o kustomize.tgz https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv5.8.1/kustomize_v5.8.1_linux_amd64.tar.gz
sha256sum -c <<'SUMS'
5d4c7623283e6dfb1971957f4b755468ab64917066a8567dd50464af298f4031  helm.tgz
029a7f0f4e1932c52a0476cf02a0fd855c0bb85694b82c338fc648dcb53a819d  kustomize.tgz
SUMS
mkdir image
tar -xzf helm.tgz --strip-components=1 -C image linux-amd64/helm
tar -xzf kustomize.tgz -C image kustomize
if [[ ${1:-} == --install ]]; then
  install -m 0755 image/{helm,kustomize} "$2/"
fi
cat > image/Dockerfile <<'DOCKER'
FROM scratch
COPY helm kustomize /
ENV HOME=/tmp PATH=/nonexistent HELM_NO_PLUGINS=1 HELM_PLUGINS=/disabled KUBECONFIG=/dev/null TZ=UTC
USER 65534:65534
DOCKER
docker build --platform linux/amd64 -t manifest-diff-renderers:1 image
