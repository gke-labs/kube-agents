# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 20: an image the old node pulled once and no registry serves afterwards
# Runs 20 to 20c never cached the image: the pool's default compute service account had no read on the
# repository (403 on the token fetch), and the run went on to retire the image and upgrade anyway. plant()
# now grants the node service account reader on the repository and stops unless the pod runs; before()
# restarts the pod on the old node after the image is gone, the control that shows the cache hides it.
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
REPO=us-central1-docker.pkg.dev/$PROJECT/upg-scenarios
IMAGE=$REPO/$CLUSTER/pause:3.9   # one image path per cluster: two runs in one project can neither retire nor restore each other's image
SOURCE_IMAGE=registry.k8s.io/pause:3.9
PULL_ROLE=roles/artifactregistry.reader; ROLLOUT_TIMEOUT=420s
CRANE_VERSION=v0.22.1
CRANE=${CRANE:-$(command -v crane || echo "$H/.bin/crane")}
[ -x "$CRANE" ] || { echo "crane not found; build it: GOBIN=$H/.bin GOTOOLCHAIN=auto go install github.com/google/go-containerregistry/cmd/crane@$CRANE_VERSION" >&2; exit 1; }
# (build it locally: the released darwin binary aborts on current macOS with "missing LC_UUID load command")
# Copy with crane under the caller's own token: Cloud Build's default service account cannot push to
# the repository, and no Docker daemon is needed. The login goes to a throwaway DOCKER_CONFIG, so the token
# does not stay in ~/.docker/config.json.
node_sa(){ local sa; sa=$(G container node-pools describe work-pool --cluster "$CLUSTER" --zone "$ZONE" --format='value(config.serviceAccount)')
  [ "$sa" = default ] && sa="$(G projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"; echo "$sa"; }
grant_pull(){ G artifacts repositories add-iam-policy-binding upg-scenarios --location us-central1 --member "serviceAccount:$(node_sa)" --role "$PULL_ROLE" --format='yaml(bindings)'; }
cached_images(){ K get nodes -l role=work -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.images[*].names}{"\n"}{end}' | tr ' ' '\n' | grep -e '^gke-' -e upg-scenarios; }
pod_phase(){ K -n scen get pods -l app=retired-image -o jsonpath='{.items[*].status.phase}'; }
push_image(){ local dc rc; dc=$(mktemp -d)
  gcloud auth print-access-token | DOCKER_CONFIG=$dc "$CRANE" auth login us-central1-docker.pkg.dev -u oauth2accesstoken --password-stdin >/dev/null &&
    DOCKER_CONFIG=$dc "$CRANE" copy "$SOURCE_IMAGE" "$IMAGE" && DOCKER_CONFIG=$dc "$CRANE" digest "$IMAGE"; rc=$?; rm -rf "$dc"; return $rc; }
plant(){ G artifacts repositories describe upg-scenarios --location us-central1 >/dev/null 2>&1 || G artifacts repositories create upg-scenarios --repository-format=docker --location=us-central1 --quiet; ev registry push push_image; ev registry grant-pull grant_pull; K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: retired-image}
spec:
  replicas: 1
  selector: {matchLabels: {app: retired-image}}
  template:
    metadata: {labels: {app: retired-image}}
    spec:
      nodeSelector: {role: work}
      containers: [{name: web, image: $IMAGE, imagePullPolicy: IfNotPresent, resources: {requests: {cpu: 10m, memory: 16Mi}}}]
Y
  K -n scen delete pod -l app=retired-image --ignore-not-found >/dev/null; K -n scen rollout status deploy/retired-image --timeout=$ROLLOUT_TIMEOUT
  [ "$(pod_phase)" = Running ] || { ev registry plant-failed K -n scen get events --field-selector reason=Failed -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; note registry "precondition not met: the image never ran on the old node; stopping before the retirement"; exit 1; }; }
before(){ ev registry cached cached_images; ev registry before K -n scen get pods -l app=retired-image -o wide; ev registry delete-image G artifacts docker images delete "$IMAGE" --delete-tags --quiet || { note final "precondition not met: the image was not retired; stopping before the upgrade"; exit 1; }; sleep 30; ev registry gone G artifacts docker images list "${IMAGE%:*}" --include-tags
  tags=$(G artifacts docker images list "${IMAGE%:*}" --include-tags --format='value(tags)') || { note final "precondition not met: the repository could not be listed after the delete; stopping before the upgrade"; exit 1; }
  ! grep -qw 3.9 <<<"$tags" || { note final "precondition not met: tag 3.9 is still listed after the delete; stopping before the upgrade"; exit 1; }; ev registry still-running K -n scen get pods -l app=retired-image -o wide
  note registry "control: restart the pod on the old node, which still holds the image in its cache"; K -n scen delete pod -l app=retired-image --wait=true >/dev/null
  K -n scen rollout status deploy/retired-image --timeout=120s; ev registry restarted-from-cache K -n scen get pods -l app=retired-image -o wide
  # The control gives the verdict its meaning: a pod already failing on the old node would make the after-state's ImagePullBackOff say nothing about the rebuild.
  [ "$(pod_phase)" = Running ] || { ev registry control-failed K -n scen get events --field-selector reason=Failed -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; note final "precondition not met: the restarted pod did not run from the old node's cache; stopping before the upgrade"; exit 1; }; }
break_it(){ V=$(newest_patch REGULAR 1.35); [ "$(G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)')" = "$V" ] || upgrade_master "$V"; upgrade_pool work-pool "$V" registry:scen:app=retired-image; }
after(){ ev registry after K -n scen get pods -l app=retired-image -o wide; ev registry pull-events K -n scen get events --field-selector reason=Failed -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; }
