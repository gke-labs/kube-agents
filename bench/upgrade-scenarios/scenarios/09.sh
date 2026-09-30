# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 9: a deprecated but still served API (Endpoints v1 on 1.33+): audit-stamped, and nothing breaks on upgrade
CHANNEL=REGULAR; START=1.34
plant(){ K -n scen apply -f - <<'Y'
apiVersion: v1
kind: ServiceAccount
metadata: {name: ep-writer}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: ep-writer}
rules: [{apiGroups: [""], resources: [endpoints], verbs: [create, patch, get]}]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: ep-writer}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: ep-writer}
subjects: [{kind: ServiceAccount, name: ep-writer, namespace: scen}]
---
apiVersion: v1
kind: Service
metadata: {name: lane}
spec: {clusterIP: None, ports: [{port: 9}]}
---
apiVersion: v1
kind: Endpoints
metadata: {name: lane}
subsets: [{addresses: [{ip: 192.0.2.10}], ports: [{port: 9}]}]
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: ep-writer}
spec:
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          serviceAccountName: ep-writer
          restartPolicy: Never
          containers:
            - name: w
              image: python:3.14-slim
              command: ["python3", "-c", "import json,ssl,os,urllib.request,time\nt=open('/var/run/secrets/kubernetes.io/serviceaccount/token').read()\nctx=ssl.create_default_context(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')\nbody=json.dumps({'metadata':{'annotations':{'last-run':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}}}).encode()\nreq=urllib.request.Request('https://'+os.environ['KUBERNETES_SERVICE_HOST']+'/api/v1/namespaces/scen/endpoints/lane',data=body,method='PATCH',headers={'Authorization':'Bearer '+t,'Content-Type':'application/merge-patch+json','User-Agent':'legacy-endpoints-writer/1.0'})\nprint(urllib.request.urlopen(req,context=ctx).status)"]
Y
K -n scen create job --from=cronjob/ep-writer first >/dev/null; sleep 40; }
before(){ ev deprecated-served before K -n scen logs job/first; sleep 30; ev deprecated-served audit G logging read "resource.type=k8s_cluster AND resource.labels.cluster_name=$CLUSTER AND protoPayload.authenticationInfo.principalEmail=\"system:serviceaccount:scen:ep-writer\"" --freshness 1h --limit 2 --format='value(timestamp,protoPayload.methodName,labels."k8s.io/deprecated",labels."k8s.io/removed-release")'; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; }
after(){ K -n scen create job --from=cronjob/ep-writer after >/dev/null; sleep 40; ev deprecated-served after K -n scen logs job/after; }
