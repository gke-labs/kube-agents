package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"k8s.io/apimachinery/pkg/api/meta"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// a2aTestAgentWithBusReplicas is a2aTestAgent with spec.harness.tuning.busReplicas set.
func a2aTestAgentWithBusReplicas(n int) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(n)}}
	return agent
}

// The bus is a three-server cluster by default and one server on request, and
// every piece of the cluster shape (the route listener, the spread, the
// readiness path that lets OrderedReady form a new cluster, the rollout pause,
// the lame-duck hook, the routes Service, the budget) comes and goes with it.
func TestTheBusRendersAClusterByDefaultAndOneServerOnRequest(t *testing.T) {
	for _, tc := range []struct {
		name          string
		agent         *agentv1alpha1.PlatformAgent
		wantReplicas  int
		wantStreams   int
		wantCluster   bool
		wantReadiness string
	}{
		{"default", a2aTestAgent(), 3, 3, true, "/healthz?js-enabled-only=true"},
		{"busReplicas 3", a2aTestAgentWithBusReplicas(3), 3, 3, true, "/healthz?js-enabled-only=true"},
		{"busReplicas 1", a2aTestAgentWithBusReplicas(1), 1, 1, false, "/healthz"},
		// Past the CRD's enum: the default, never a two-server cluster.
		{"busReplicas 2 past the schema", a2aTestAgentWithBusReplicas(2), 3, 3, true, "/healthz?js-enabled-only=true"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := tc.agent
			if got := a2aNATSReplicas(agent); got != tc.wantReplicas {
				t.Fatalf("a2aNATSReplicas = %d, want %d", got, tc.wantReplicas)
			}
			if got := a2aStreamReplicas(agent); got != tc.wantStreams {
				t.Errorf("a2aStreamReplicas = %d, want %d", got, tc.wantStreams)
			}

			sts := buildA2ANATSStatefulSet(agent, "h")
			if got := int(*sts.Spec.Replicas); got != tc.wantReplicas {
				t.Errorf("StatefulSet replicas = %d, want %d", got, tc.wantReplicas)
			}
			// Immutable on a StatefulSet; changing either would mean
			// recreating every existing install's bus.
			if sts.Spec.ServiceName != a2aNATSName(agent) {
				t.Errorf("serviceName = %q, want the client Service %q", sts.Spec.ServiceName, a2aNATSName(agent))
			}
			if sts.Spec.PodManagementPolicy != "" {
				t.Errorf("podManagementPolicy = %q, want unset (OrderedReady)", sts.Spec.PodManagementPolicy)
			}
			pod := sts.Spec.Template.Spec
			c := pod.Containers[0]
			if got := c.ReadinessProbe.HTTPGet.Path; got != tc.wantReadiness {
				t.Errorf("readiness path = %q, want %q", got, tc.wantReadiness)
			}
			hasRoutePort := slices.ContainsFunc(c.Ports, func(p corev1.ContainerPort) bool { return p.ContainerPort == a2aNATSRoutePort })
			if hasRoutePort != tc.wantCluster {
				t.Errorf("route port declared = %v, want %v", hasRoutePort, tc.wantCluster)
			}
			if got := len(pod.TopologySpreadConstraints) > 0; got != tc.wantCluster {
				t.Errorf("topology spread present = %v, want %v", got, tc.wantCluster)
			}
			if tc.wantCluster {
				// The hostname spread is the HA: DoNotSchedule with a
				// minimum of three domains, so a third server waits for a
				// third node rather than doubling up on one.
				if !slices.ContainsFunc(pod.TopologySpreadConstraints, func(c corev1.TopologySpreadConstraint) bool {
					return c.TopologyKey == corev1.LabelHostname && c.WhenUnsatisfiable == corev1.DoNotSchedule &&
						c.MinDomains != nil && *c.MinDomains == 3 && c.MaxSkew == 1
				}) {
					t.Errorf("no hostname spread with DoNotSchedule and minDomains 3: %+v", pod.TopologySpreadConstraints)
				}
			}
			if p := sts.Spec.PersistentVolumeClaimRetentionPolicy; p == nil ||
				p.WhenScaled != appsv1.DeletePersistentVolumeClaimRetentionPolicyType || p.WhenDeleted != appsv1.RetainPersistentVolumeClaimRetentionPolicyType {
				t.Errorf("claim retention = %+v, want delete on scale-down and retain on StatefulSet deletion", p)
			}
			if got := sts.Spec.MinReadySeconds > 0; got != tc.wantCluster {
				t.Errorf("minReadySeconds > 0 = %v, want %v", got, tc.wantCluster)
			}
			if !slices.ContainsFunc(c.Env, func(e corev1.EnvVar) bool {
				return e.Name == "POD_NAME" && e.ValueFrom != nil && e.ValueFrom.FieldRef != nil && e.ValueFrom.FieldRef.FieldPath == "metadata.name"
			}) {
				t.Error("POD_NAME is not the pod's name; server_name reads it")
			}
			if c.Lifecycle == nil || c.Lifecycle.PreStop == nil || c.Lifecycle.PreStop.Exec == nil ||
				!slices.Equal(c.Lifecycle.PreStop.Exec.Command, []string{"nats-server", "--signal", "ldm=" + a2aNATSPidFile}) {
				t.Errorf("preStop is not the lame-duck signal: %+v", c.Lifecycle)
			}
			if pod.TerminationGracePeriodSeconds == nil || *pod.TerminationGracePeriodSeconds <= a2aNATSLameDuckSeconds {
				t.Errorf("termination grace %v does not leave room for a %ds lame duck", pod.TerminationGracePeriodSeconds, a2aNATSLameDuckSeconds)
			}

			conf := renderA2ANATSConf(agent, func(k string) string { return "pw-" + k }, &a2aCalloutKeys{IssuerPublic: "A", XKeyPublic: "X"})
			if got := a2aClusterBlockRe.MatchString(conf); got != tc.wantCluster {
				t.Errorf("cluster block present = %v, want %v", got, tc.wantCluster)
			}
			for _, line := range []string{"server_name: $POD_NAME", `pid_file: "` + a2aNATSPidFile + `"`, `lame_duck_duration: "30s"`} {
				if !strings.Contains(conf, line) {
					t.Errorf("nats.conf lacks %q", line)
				}
			}

			job := buildA2AProvisionJob(agent)
			if !slices.ContainsFunc(job.Spec.Template.Spec.Containers[0].Env, func(e corev1.EnvVar) bool {
				return e.Name == a2aStreamReplicasEnv && e.Value == strconv.Itoa(tc.wantStreams)
			}) {
				t.Errorf("provision Job does not set %s=%d", a2aStreamReplicasEnv, tc.wantStreams)
			}
		})
	}
}

// The routes Service is headless and publishes not-ready addresses: a server
// of a new cluster reports ready only after it has met a peer, so a Service
// of ready addresses alone would keep the first two servers from finding each
// other. The budget allows one eviction, never zero.
func TestTheRoutesServiceAndBudgetFitAClusterThatIsStillForming(t *testing.T) {
	agent := a2aTestAgent()
	svc := buildA2ANATSRoutesService(agent)
	if svc.Spec.ClusterIP != corev1.ClusterIPNone {
		t.Errorf("routes Service ClusterIP = %q, want None", svc.Spec.ClusterIP)
	}
	if !svc.Spec.PublishNotReadyAddresses {
		t.Error("routes Service does not publish not-ready addresses")
	}
	if len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != a2aNATSRoutePort {
		t.Errorf("routes Service ports = %+v, want 6222 only", svc.Spec.Ports)
	}
	if svc.Spec.Selector["app"] != a2aNATSName(agent) {
		t.Errorf("routes Service selects %v, want the bus's pods", svc.Spec.Selector)
	}

	pdb := buildA2ANATSPDB(agent)
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("bus budget maxUnavailable = %v, want 1", pdb.Spec.MaxUnavailable)
	}
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("bus budget sets minAvailable %v as well", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.Selector.MatchLabels["app"] != a2aNATSName(agent) {
		t.Errorf("bus budget selects %v, want the bus's pods", pdb.Spec.Selector.MatchLabels)
	}
}

// 6222 is admitted from the bus's own servers and from nothing else, whatever
// the server count: the rule does not follow the count, so no pass that reads
// a count other than the one running (a refused spec edit that sets
// busReplicas renders the fence without converting the bus) can cut a running
// cluster's routes. Its scope is exactly one port, TCP 6222, and exactly one
// peer, the bus's own pods in the agent's namespace.
func TestTheBusFenceAdmitsRoutesFromItsOwnServersOnly(t *testing.T) {
	desiredOne := a2aTestAgentWithBusReplicas(1)
	desiredOne.Status.BusReplicas = 3 // a running cluster the spec would convert
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"default":                       a2aTestAgent(),
		"busReplicas 1":                 a2aTestAgentWithBusReplicas(1),
		"busReplicas 3":                 a2aTestAgentWithBusReplicas(3),
		"spec 1 over a running cluster": desiredOne,
	} {
		t.Run(name, func(t *testing.T) {
			np := buildA2ANATSNetworkPolicy(agent)
			var rules []networkingv1.NetworkPolicyIngressRule
			for _, rule := range np.Spec.Ingress {
				for _, p := range rule.Ports {
					if p.Port != nil && p.Port.IntValue() == a2aNATSRoutePort {
						rules = append(rules, rule)
					}
				}
			}
			if len(rules) != 1 {
				t.Fatalf("the fence has %d rules naming 6222, want exactly one", len(rules))
			}
			rule := rules[0]
			if len(rule.Ports) != 1 || rule.Ports[0].Protocol == nil || *rule.Ports[0].Protocol != corev1.ProtocolTCP || rule.Ports[0].EndPort != nil {
				t.Errorf("the route rule admits more than TCP 6222: %+v", rule.Ports)
			}
			if len(rule.From) != 1 || rule.From[0].PodSelector == nil || rule.From[0].NamespaceSelector != nil || rule.From[0].IPBlock != nil ||
				len(rule.From[0].PodSelector.MatchLabels) != 1 || rule.From[0].PodSelector.MatchLabels["app"] != a2aNATSName(agent) ||
				len(rule.From[0].PodSelector.MatchExpressions) != 0 {
				t.Errorf("the route rule's peers are not exactly the bus's own pods: %+v", rule.From)
			}
		})
	}
}

// Routes authenticate by certificate alone: the cluster block has a tls block
// naming the bus CA (nats-server requires and verifies a client certificate on
// every route once there is one), and renders no route user or password, in
// the block or in the route URL.
func TestTheRoutesAuthenticateByCertificateOnly(t *testing.T) {
	conf := renderA2ANATSConf(a2aTestAgent(), func(k string) string { return "pw-" + k }, &a2aCalloutKeys{IssuerPublic: "A", XKeyPublic: "X"})
	block := a2aClusterBlockRe.FindString(conf)
	for _, want := range []string{"tls {", "verify: true", `ca_file: "` + a2aNATSTLSMountDir + "/" + a2aNATSTLSCAKey + `"`, `cert_file: "` + a2aNATSTLSMountDir + "/" + a2aNATSTLSCertKey + `"`} {
		if !strings.Contains(block, want) {
			t.Errorf("the cluster block lacks %q:\n%s", want, block)
		}
	}
	for _, unwanted := range []string{"authorization", "user:", "password", "pw-"} {
		if strings.Contains(block, unwanted) {
			t.Errorf("the cluster block carries %q; routes authenticate by certificate:\n%s", unwanted, block)
		}
	}
	if regexp.MustCompile(`nats-route://[^"]*@`).MatchString(block) {
		t.Errorf("the route URL carries credentials:\n%s", block)
	}
}

// The serving certificate names the routes Service and allows client auth, and
// one issued before either is reissued rather than kept: a server presenting
// it on a route would be refused by its peer.
func TestTheServingCertificateCoversTheRoutesAndClientAuth(t *testing.T) {
	agent := a2aTestAgent()
	names := a2aNATSDNSNames(agent)
	for _, want := range []string{
		a2aNATSRoutesName(agent) + "." + agent.Namespace + ".svc." + a2aNATSClusterDomain,
		a2aNATSName(agent) + "." + agent.Namespace + ".svc." + a2aNATSClusterDomain,
	} {
		if !slices.Contains(names, want) {
			t.Errorf("serving SANs lack %q: %v", want, names)
		}
	}
	// The same SANs for one server or three, so changing busReplicas does
	// not reissue the certificate and roll the bus a second time.
	if !slices.Equal(a2aNATSDNSNames(a2aTestAgentWithBusReplicas(1)), names) {
		t.Error("the serving SANs depend on busReplicas")
	}

	now := time.Now()
	caCert, caKey, caPEM, _, err := generateA2ANATSCA(now)
	if err != nil {
		t.Fatal(err)
	}
	_, certPEM, keyPEM, err := generateA2ANATSServing(caCert, caKey, names, now)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := parseA2ACert(certPEM)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(cert.ExtKeyUsage, []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth}) {
		t.Errorf("serving certificate EKU = %v, want ServerAuth and ClientAuth", cert.ExtKeyUsage)
	}
	secret := &corev1.Secret{Data: map[string][]byte{a2aNATSTLSCertKey: certPEM, a2aNATSTLSKeyKey: keyPEM, a2aNATSTLSCAKey: caPEM}}
	if _, ok := readA2ANATSServing(secret, caCert, caPEM, names, now); !ok {
		t.Fatal("a freshly issued certificate is not reused")
	}

	// The same certificate with ServerAuth alone, as every bus before
	// cluster routes was issued: reissued.
	old := a2aServerAuthOnlyCert(t, caCert, caKey, names, now)
	secret.Data[a2aNATSTLSCertKey] = old.certPEM
	secret.Data[a2aNATSTLSKeyKey] = old.keyPEM
	if _, ok := readA2ANATSServing(secret, caCert, caPEM, names, now); ok {
		t.Error("a ServerAuth-only certificate is reused; its server would be refused on every route")
	}
}

// Every server's data claim is reaped on deletion, not only ordinal 0's, and
// the instance-label guard still holds for each.
func TestHandleDeletionReapsEveryServersJetStreamPVC(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Finalizers = []string{platformAgentFinalizer}
	now := metav1.Now()
	agent.DeletionTimestamp = &now
	labels := buildA2ANATSStatefulSet(a2aTestAgent(), "h").Spec.VolumeClaimTemplates[0].Labels
	objs := []*corev1.PersistentVolumeClaim{}
	for ordinal := range a2aNATSMaxReplicas {
		objs = append(objs, &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
			Name: a2aNATSDataClaimName(agent, ordinal), Namespace: agent.Namespace, Labels: labels,
		}})
	}
	squatter := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: objs[1].Name, Namespace: agent.Namespace}}
	builder := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, objs[0], squatter, objs[2])
	cl := builder.Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if _, err := r.handleDeletion(context.Background(), agent); err != nil {
		t.Fatalf("handleDeletion failed: %v", err)
	}
	for _, i := range []int{0, 2} {
		err := cl.Get(context.Background(), types.NamespacedName{Name: objs[i].Name, Namespace: agent.Namespace}, &corev1.PersistentVolumeClaim{})
		if !errors.IsNotFound(err) {
			t.Errorf("server %d's labelled claim survived deletion (err=%v)", i, err)
		}
	}
	if err := cl.Get(context.Background(), types.NamespacedName{Name: squatter.Name, Namespace: agent.Namespace}, &corev1.PersistentVolumeClaim{}); err != nil {
		t.Errorf("an unlabelled squatter on ordinal 1's name was deleted (err=%v)", err)
	}
}

// Every stream and bucket the provision script creates takes the configured
// replica count, never a literal. A stream added with --replicas=1 (or any
// number) fails here, so a new stream cannot quietly ship R1 on a cluster.
func TestEveryProvisionedStreamTakesTheConfiguredReplicaCount(t *testing.T) {
	script := a2aProvisionScript(a2aTestAgent())
	if m := regexp.MustCompile(`--replicas[= ]+[0-9]`).FindAllString(script, -1); len(m) != 0 {
		t.Errorf("the provision script writes a literal replica count: %v", m)
	}
	adds := regexp.MustCompile(`\$NATS (stream|kv) add `).FindAllStringIndex(script, -1)
	if len(adds) == 0 {
		t.Fatal("the provision script adds no stream or bucket; the check below would be vacuous")
	}
	for _, loc := range adds {
		// A stream add continues over backslashed lines; read to the end
		// of the command.
		cmd := script[loc[0]:]
		for i := 0; i < len(cmd); i++ {
			if cmd[i] == '\n' && (i == 0 || cmd[i-1] != '\\') {
				cmd = cmd[:i]
				break
			}
		}
		if !strings.Contains(cmd, `--replicas="${`+a2aStreamReplicasEnv+`}"`) {
			t.Errorf("this add does not take --replicas=\"${%s}\":\n%s", a2aStreamReplicasEnv, cmd)
		}
	}
	// And every provisioned stream is one the script adds.
	for _, s := range a2aProvisionedStreams {
		name := strings.TrimPrefix(s, "KV_")
		if !regexp.MustCompile(`\$NATS (stream|kv) add ` + regexp.QuoteMeta(name) + `\b`).MatchString(script) {
			t.Errorf("a2aProvisionedStreams names %q, which the provision script does not add", s)
		}
	}
}

// The rendered cluster block forms a cluster: three embedded servers, started
// from the operator's own nats.conf with only what a test process cannot
// honour changed (server names, ports, the store, the pid file, and the route
// host, which is the routes Service in a pod and localhost here), meet over
// routes whose TLS is verified both ways, and elect a JetStream meta leader.
//
// The peers each server learns by gossip arrive as IPs, which the test
// certificate does not name. That they connect at all is nats-server dialling
// them with the TLS name saved from the explicit route (saveRouteTLSName), the
// behaviour the render's SANs rely on. A fourth server whose certificate comes
// from another CA does not join: routes authenticate by certificate.
func TestTheRenderedClusterFormsOverVerifiedRoutes(t *testing.T) {
	agent := a2aTestAgent()
	creds := a2aFullCreds("c", "1")
	conf := string(buildA2ANATSConfigSecret(agent, creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	if !a2aClusterBlockRe.MatchString(conf) {
		t.Fatal("the default render has no cluster block")
	}
	routeHost := a2aNATSRoutesName(agent) + "." + agent.Namespace + ".svc:" + strconv.Itoa(a2aNATSRoutePort)
	if !strings.Contains(conf, routeHost) {
		t.Fatalf("the cluster block does not route to %s", routeHost)
	}

	ports := a2aFreePorts(t, 4)
	// tlsDir, when set, is a serving pair from another CA: a server that can
	// reach 6222 but does not hold the bus's serving key.
	start := func(i int, tlsDir string) *natsserver.Server {
		t.Helper()
		c := strings.Replace(conf, "server_name: $POD_NAME", "server_name: a2a-test-nats-"+strconv.Itoa(i), 1)
		c = strings.Replace(c, `pid_file: "`+a2aNATSPidFile+`"`, `pid_file: "`+filepath.Join(t.TempDir(), "pid")+`"`, 1)
		c = strings.Replace(c, routeHost, "localhost:"+strconv.Itoa(ports[0]), 1)
		c = strings.Replace(c, "port: "+strconv.Itoa(a2aNATSRoutePort), "port: "+strconv.Itoa(ports[i]), 1)
		if tlsDir != "" {
			c = strings.ReplaceAll(c, a2aNATSTLSMountDir+"/", tlsDir+"/")
		} else {
			c = a2aWithTestTLS(t, c)
		}
		path := filepath.Join(t.TempDir(), "nats.conf")
		if err := os.WriteFile(path, []byte(c), 0o600); err != nil {
			t.Fatal(err)
		}
		opts, err := natsserver.ProcessConfigFile(path)
		if err != nil {
			t.Fatalf("server %d: the rendered nats.conf does not parse: %v", i, err)
		}
		opts.Port, opts.HTTPPort, opts.Websocket.Port = -1, -1, -1
		opts.Host, opts.Cluster.Host = "127.0.0.1", "127.0.0.1"
		opts.StoreDir = t.TempDir()
		opts.NoLog, opts.NoSigs = true, true
		s, err := natsserver.NewServer(opts)
		if err != nil {
			t.Fatalf("server %d: %v", i, err)
		}
		go s.Start()
		t.Cleanup(func() { s.Shutdown(); s.WaitForShutdown() })
		return s
	}

	servers := []*natsserver.Server{start(0, ""), start(1, ""), start(2, "")}
	deadline := time.Now().Add(20 * time.Second)
	for {
		formed := true
		for _, s := range servers {
			if len(a2aRoutePeers(t, s)) != 2 {
				formed = false
			}
		}
		leader := slices.ContainsFunc(servers, func(s *natsserver.Server) bool { return s.JetStreamIsLeader() })
		if formed && leader {
			break
		}
		if time.Now().After(deadline) {
			for i, s := range servers {
				t.Logf("server %d: peers %v, clustered=%v, leader=%v", i, a2aRoutePeers(t, s), s.JetStreamIsClustered(), s.JetStreamIsLeader())
			}
			t.Fatal("the rendered cluster did not form with a JetStream meta leader within 20s")
		}
		time.Sleep(100 * time.Millisecond)
	}
	for i, s := range servers {
		if !s.JetStreamIsClustered() {
			t.Errorf("server %d is not running clustered JetStream", i)
		}
		// One route per peer (pool_size -1): two, not a pool of four.
		if n := s.NumRoutes(); n != 2 {
			t.Errorf("server %d holds %d route connections, want exactly one per peer (2)", i, n)
		}
	}

	// Interest crosses every route in the APP account: a subscriber on each
	// server receives what the other two publish. A client on a server
	// missing a peer's interest would see "no responders" for every stream
	// that peer leads, which is the failure pool_size -1 exists for.
	seedPassword := string(creds.Data[a2aSeedPasswordKey])
	connect := func(s *natsserver.Server) *nats.Conn {
		t.Helper()
		nc, err := nats.Connect(s.ClientURL(), a2aTestBusTLS(t), nats.UserInfo("seed", seedPassword))
		if err != nil {
			t.Fatalf("connect as seed to %s: %v", s.Name(), err)
		}
		t.Cleanup(nc.Close)
		return nc
	}
	const subject = "a2a.topics.shared.blueprint"
	for i, sub := range servers {
		nc := connect(sub)
		ch := make(chan *nats.Msg, 4)
		if _, err := nc.ChanSubscribe(subject, ch); err != nil {
			t.Fatal(err)
		}
		if err := nc.Flush(); err != nil {
			t.Fatal(err)
		}
		deadline := time.Now().Add(5 * time.Second)
		for j, pub := range servers {
			if j == i {
				continue
			}
			pc := connect(pub)
			got := false
			for !got && time.Now().Before(deadline) {
				if err := pc.Publish(subject, []byte(fmt.Sprintf("%d->%d", j, i))); err != nil {
					t.Fatal(err)
				}
				select {
				case <-ch:
					got = true
				case <-time.After(200 * time.Millisecond):
				}
			}
			if !got {
				t.Errorf("a publish on server %d never reached a subscriber on server %d", j, i)
			}
		}
	}

	// A serving pair from a CA of its own, as anything that is not one of the
	// bus's servers would have: it names the right host and is not signed by
	// the bus CA. It trusts the bus CA, though, so its own handshake with a
	// bus server succeeds and the refusal, if any, is the bus server's check
	// of its certificate (verify: true), which is what this exercises.
	foreignCA := t.TempDir()
	busTLSDir, _ := a2aTestTLSMaterial(t)
	busCAPEM, err := os.ReadFile(filepath.Join(busTLSDir, a2aNATSTLSCAKey))
	if err != nil {
		t.Fatal(err)
	}
	caCert, caKey, _, _, err := generateA2ANATSCA(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	_, certPEM, keyPEM, err := generateA2ANATSServing(caCert, caKey, []string{a2aTestServerName}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{a2aNATSTLSCertKey: certPEM, a2aNATSTLSKeyKey: keyPEM, a2aNATSTLSCAKey: busCAPEM} {
		if err := os.WriteFile(filepath.Join(foreignCA, name), data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	intruder := start(3, foreignCA)
	time.Sleep(3 * time.Second)
	if peers := a2aRoutePeers(t, intruder); len(peers) != 0 {
		t.Errorf("a server with a certificate from another CA joined, peering with %v", peers)
	}
	for i, s := range servers {
		if peers := a2aRoutePeers(t, s); len(peers) != 2 || slices.Contains(peers, "a2a-test-nats-3") {
			t.Errorf("server %d peers with %v after the intruder tried, want its two members only", i, peers)
		}
	}
}

// a2aFreePorts reserves n distinct loopback ports and releases them for the
// caller to bind.
func a2aFreePorts(t *testing.T, n int) []int {
	t.Helper()
	var ports []int
	var ls []net.Listener
	for range n {
		l, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		ls = append(ls, l)
		ports = append(ports, l.Addr().(*net.TCPAddr).Port)
	}
	for _, l := range ls {
		_ = l.Close()
	}
	return ports
}

// a2aServerAuthOnlyCert issues the serving certificate as the bus issued it
// before cluster routes: the same names, ServerAuth alone.
func a2aServerAuthOnlyCert(t *testing.T, caCert *x509.Certificate, caKey *ecdsa.PrivateKey, names []string, now time.Time) struct{ certPEM, keyPEM []byte } {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, err := a2aSerial()
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificate(rand.Reader, &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: names[0]},
		DNSNames:     names,
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(a2aNATSTLSLifetime),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	keyPEM, err := a2aKeyPEM(key)
	if err != nil {
		t.Fatal(err)
	}
	return struct{ certPEM, keyPEM []byte }{a2aCertPEM(der), keyPEM}
}

// a2aRoutePeers is the distinct servers s holds routes to, by server name.
// NumRoutes counts connections, and nats-server pools several per peer.
func a2aRoutePeers(t *testing.T, s *natsserver.Server) []string {
	t.Helper()
	rz, err := s.Routez(nil)
	if err != nil {
		t.Fatal(err)
	}
	var peers []string
	for _, r := range rz.Routes {
		if r.RemoteName != "" && !slices.Contains(peers, r.RemoteName) {
			peers = append(peers, r.RemoteName)
		}
	}
	slices.Sort(peers)
	return peers
}

// The server count is sticky: a set field wins, an existing bus keeps what it
// runs (from its StatefulSet, or from the claims a deleted StatefulSet left),
// and only a bus that does not exist yet gets three. Converting a running bus
// says so once, because conversion empties it.
func TestTheBusServerCountIsStickyAndConversionIsAnnounced(t *testing.T) {
	ctx := context.Background()
	claim := func(agent *agentv1alpha1.PlatformAgent, ordinal int, labelled bool) *corev1.PersistentVolumeClaim {
		c := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSDataClaimName(agent, ordinal), Namespace: agent.Namespace}}
		if labelled {
			c.Labels = buildA2ANATSStatefulSet(agent, "h").Spec.VolumeClaimTemplates[0].Labels
		}
		return c
	}
	sts := func(agent *agentv1alpha1.PlatformAgent, replicas int32) *appsv1.StatefulSet {
		s := buildA2ANATSStatefulSet(agent, "h")
		s.Spec.Replicas = ptr.To(replicas)
		return s
	}
	for _, tc := range []struct {
		name      string
		spec      *int
		objects   func(*agentv1alpha1.PlatformAgent) []client.Object
		want      int32
		wantEvent bool
	}{
		{"a new bus", nil, func(*agentv1alpha1.PlatformAgent) []client.Object { return nil }, 3, false},
		{"an existing single-server bus", nil, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 1)} }, 1, false},
		{"an existing cluster", nil, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 3)} }, 3, false},
		{"a deleted single-server bus's claim", nil, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{claim(a, 0, true)} }, 1, false},
		{"a deleted cluster's claims, one missing", nil, func(a *agentv1alpha1.PlatformAgent) []client.Object {
			return []client.Object{claim(a, 0, true), claim(a, 1, true)}
		}, 3, false},
		{"an unlabelled squatter claim", nil, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{claim(a, 0, false)} }, 3, false},
		{"set to 3 on a single-server bus converts it", ptr.To(3), func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 1)} }, 3, true},
		{"set to 1 on a cluster converts it", ptr.To(1), func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 3)} }, 1, true},
		{"set to what runs is no conversion", ptr.To(1), func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 1)} }, 1, false},
		// Claims 0 and 1 without 2 read as a cluster, so setting 1 over
		// them is a conversion and says so.
		{"set to 1 over a deleted cluster's two claims converts it", ptr.To(1), func(a *agentv1alpha1.PlatformAgent) []client.Object {
			return []client.Object{claim(a, 0, true), claim(a, 1, true)}
		}, 1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			if tc.spec != nil {
				agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: tc.spec}}
			}
			objs := append([]client.Object{agent}, tc.objects(agent)...)
			rec := record.NewFakeRecorder(8)
			r := &PlatformAgentReconciler{Client: fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build(), Scheme: setupScheme(), Recorder: rec, busConversionAllowed: true}
			converting, err := r.resolveA2ABusReplicas(ctx, agent)
			if err != nil {
				t.Fatal(err)
			}
			if converting != tc.wantEvent {
				t.Errorf("converting = %v, want %v", converting, tc.wantEvent)
			}
			if agent.Status.BusReplicas != tc.want {
				t.Errorf("status.busReplicas = %d, want %d", agent.Status.BusReplicas, tc.want)
			}
			if got := a2aNATSReplicas(agent); got != int(tc.want) {
				t.Errorf("a2aNATSReplicas = %d, want %d", got, tc.want)
			}
			if got := len(rec.Events) > 0; got != tc.wantEvent {
				t.Errorf("conversion Event recorded = %v, want %v", got, tc.wantEvent)
			}
			// The Event is once per conversion: a second pass that already
			// reads the new count says nothing more.
			for len(rec.Events) > 0 {
				<-rec.Events
			}
			if _, err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
				t.Fatal(err)
			}
			if len(rec.Events) != 0 {
				t.Error("the conversion Event repeated on the next pass")
			}
		})
	}
}

// The provision Job is not created until the bus has rolled out: every replica
// updated and Ready, on a status that has seen the applied generation. A Job
// that ran mid-conversion would find the old streams, create nothing, and
// leave the bus empty once the last server restarted into the new shape.
func TestTheProvisionJobWaitsForTheBusToRollOut(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	reportCalloutStatus(t, ctx, cl, agent, 2, 2, 2)
	jobKey := client.ObjectKey{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}

	// Two of three servers Ready: held.
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, client.ObjectKey{Name: a2aNATSName(agent), Namespace: agent.Namespace}, sts); err != nil {
		t.Fatal(err)
	}
	sts.Status = appsv1.StatefulSetStatus{ObservedGeneration: sts.Generation, Replicas: 3, ReadyReplicas: 2, UpdatedReplicas: 3, CurrentRevision: "r", UpdateRevision: "r"}
	if err := cl.Status().Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if !state.jobHeld {
		t.Error("the provision Job was not held with a server not Ready")
	}
	if err := cl.Get(ctx, jobKey, &batchv1.Job{}); !errors.IsNotFound(err) {
		t.Errorf("the provision Job exists mid-rollout (err=%v)", err)
	}

	// Every server Ready but one still on the old revision: held.
	if err := cl.Get(ctx, client.ObjectKey{Name: a2aNATSName(agent), Namespace: agent.Namespace}, sts); err != nil {
		t.Fatal(err)
	}
	sts.Status = appsv1.StatefulSetStatus{ObservedGeneration: sts.Generation, Replicas: 3, ReadyReplicas: 3, UpdatedReplicas: 3, CurrentRevision: "old", UpdateRevision: "r"}
	if err := cl.Status().Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
	if state, err = r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if !state.jobHeld {
		t.Error("the provision Job was not held with a server still on the old revision")
	}

	// Rolled out: created.
	reportBusRolledOut(t, ctx, cl, agent)
	if state, err = r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if state.jobHeld {
		t.Error("the provision Job is held on a bus that has rolled out")
	}
	if err := cl.Get(ctx, jobKey, &batchv1.Job{}); err != nil {
		t.Errorf("the provision Job was not created once the bus rolled out: %v", err)
	}
}

// A StatefulSet read that fails for any reason but NotFound fails the pass: it
// is not read as "no bus", which would give a running single-server bus three
// servers and empty it.
func TestABusReadErrorIsNotReadAsNoBus(t *testing.T) {
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*appsv1.StatefulSet); ok {
				return apierrors.NewServiceUnavailable("informer not ready")
			}
			return c.Get(ctx, key, obj, opts...)
		}}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(4)}
	if _, err := r.resolveA2ABusReplicas(context.Background(), agent); err == nil {
		t.Fatalf("a failed StatefulSet read resolved to %d servers instead of failing the pass", agent.Status.BusReplicas)
	}
}

// Lowering a cluster to one server takes the routes Service and the budget away
// rather than leaving them: a budget over one server would block its drains.
func TestASingleServerBusHasNoRoutesServiceOrBudget(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(16), busConversionAllowed: true}
	ctx := context.Background()
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	routes := client.ObjectKey{Name: a2aNATSRoutesName(agent), Namespace: agent.Namespace}
	pdb := client.ObjectKey{Name: a2aNATSName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, routes, &corev1.Service{}); err != nil {
		t.Fatalf("a new bus rendered no routes Service: %v", err)
	}
	if err := cl.Get(ctx, pdb, &policyv1.PodDisruptionBudget{}); err != nil {
		t.Fatalf("a new bus rendered no budget: %v", err)
	}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(1)}}
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, routes, &corev1.Service{}); !apierrors.IsNotFound(err) {
		t.Errorf("the routes Service survived lowering the bus to one server (err=%v)", err)
	}
	if err := cl.Get(ctx, pdb, &policyv1.PodDisruptionBudget{}); !apierrors.IsNotFound(err) {
		t.Errorf("the budget survived lowering the bus to one server (err=%v)", err)
	}
}

// The resolved count reaches the stored status even on a pass where nothing
// else changed: the writer is told the persisted value differs.
func TestTheResolvedBusCountIsWrittenToStatus(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(16)}
	ctx := context.Background()
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), agent); err != nil {
		t.Fatal(err)
	}
	// One pass to bring the conditions current, so the next is a no-op but
	// for the count.
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), agent); err != nil {
		t.Fatal(err)
	}
	persisted := agent.Status.BusReplicas
	agent.Status.BusReplicas = 1
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{busReplicasPersisted: persisted}); err != nil {
		t.Fatal(err)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	if stored.Status.BusReplicas != 1 {
		t.Errorf("stored status.busReplicas = %d, want the resolved 1", stored.Status.BusReplicas)
	}
}

// A conversion provisions the bus again, after it rolls out, in both
// directions and back: the conversion changes STREAM_REPLICAS and so the Job's
// digested name, the superseded-Job sweep removes the old shape's Job, and the
// bus gate holds the new one until the roll is done. A conversion back to an
// earlier shape gets a fresh Job, not that shape's old completed one.
func TestAConversionProvisionsTheBusAgain(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(32), busConversionAllowed: true}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	completeTheProvisionJob(t, ctx, cl, agent)
	threeServerJob := buildA2AProvisionJob(agent).Name
	if err := cl.Get(ctx, client.ObjectKey{Name: threeServerJob, Namespace: agent.Namespace}, &batchv1.Job{}); err != nil {
		t.Fatalf("no provision Job before the conversion: %v", err)
	}

	// Convert to one server: the three-server Job is swept as superseded,
	// and the one-server Job waits for the roll.
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(1)}}
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, client.ObjectKey{Name: threeServerJob, Namespace: agent.Namespace}, &batchv1.Job{}); !apierrors.IsNotFound(err) {
		t.Errorf("the completed provision Job survived the conversion pass (err=%v)", err)
	}
	if !state.jobHeld {
		t.Error("the provision Job was not held on the conversion pass, before the bus rolled out")
	}

	// And back to three, on a bus rolled out at one: the three-server Job's
	// name again, created fresh rather than found complete.
	reportBusRolledOut(t, ctx, cl, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	completeTheProvisionJob(t, ctx, cl, agent)
	agent.Spec.Harness.Tuning.BusReplicas = ptr.To(3)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	reportBusRolledOut(t, ctx, cl, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	job := &batchv1.Job{}
	if err := cl.Get(ctx, client.ObjectKey{Name: threeServerJob, Namespace: agent.Namespace}, job); err != nil {
		t.Fatalf("no provision Job after converting back to three: %v", err)
	}
	for _, c := range job.Status.Conditions {
		if c.Type == batchv1.JobComplete {
			t.Error("the provision Job after converting back is the old completed one, not a fresh run")
		}
	}
}

// A single-server bus leaves a routes Service or budget it does not own alone:
// skipped and logged, not deleted, and the pass goes on (#3871).
func TestASingleServerBusSkipsAForeignBudgetUnderItsName(t *testing.T) {
	agent := a2aTestAgentWithBusReplicas(1)
	foreign := &policyv1.PodDisruptionBudget{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent), Namespace: agent.Namespace}}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, foreign).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(16)}
	if _, err := r.reconcileA2A(context.Background(), agent); err != nil {
		t.Fatalf("a foreign budget under the bus's name failed the pass: %v", err)
	}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(foreign), &policyv1.PodDisruptionBudget{}); err != nil {
		t.Errorf("a budget this PlatformAgent does not own was deleted: %v", err)
	}
}

// The status writer counts the NATS StatefulSet ready once a quorum of its
// servers is: one of three is a bus that cannot elect a leader.
func TestTheBusCountsTowardReadyOnlyWithAQuorum(t *testing.T) {
	for _, tc := range []struct {
		replicas, ready, want int32
	}{
		{3, 0, 0}, {3, 1, 0}, {3, 2, 2}, {3, 3, 3}, {1, 0, 0}, {1, 1, 1},
	} {
		sts := &appsv1.StatefulSet{Spec: appsv1.StatefulSetSpec{Replicas: ptr.To(tc.replicas)}, Status: appsv1.StatefulSetStatus{ReadyReplicas: tc.ready}}
		if got := a2aNATSServingReplicas(sts); got != tc.want {
			t.Errorf("%d of %d servers Ready counts as %d, want %d", tc.ready, tc.replicas, got, tc.want)
		}
	}
}

// A write whose echo comes back without status.busReplicas (the served CRD
// prunes it) stops the writers keying on the field, so the pruned write does
// not repeat on every pass; an echo that carries it clears the record.
func TestAPrunedBusReplicasStatusDoesNotLoopTheWriters(t *testing.T) {
	r := &PlatformAgentReconciler{}
	agent := a2aTestAgent()
	agent.Status.BusReplicas = 1
	if r.busReplicasStatusCurrent(agent, 0) {
		t.Fatal("a resolved count the CR does not carry reads as current before any write")
	}
	agent.Status.BusReplicas = 0 // the echo of a write of 1, pruned
	r.noteBusReplicasEcho(agent, 1)
	agent.Status.BusReplicas = 1 // the next pass resolves 1 again
	if !r.busReplicasStatusCurrent(agent, 0) {
		t.Error("after a pruned echo the writers still key on status.busReplicas and would write every pass")
	}
	r.noteBusReplicasEcho(agent, 1) // an echo that carries the field
	if r.busReplicasStatusCurrent(agent, 0) {
		t.Error("an echo that carried the field did not clear the pruned record")
	}
}

// A conversion drops the provisioned-once record, so the CR reads Provisioning
// until the new shape's provision Job completes, not Ready over an empty bus.
func TestAConversionDropsBusProvisioned(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(32), busConversionAllowed: true}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	if !busProvisioned(agent) {
		t.Fatal("precondition: BusProvisioned not set")
	}
	// The same shape: the record stands.
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if !busProvisioned(agent) {
		t.Fatal("BusProvisioned dropped on a pass with no conversion")
	}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(1)}}
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if busProvisioned(agent) {
		t.Error("BusProvisioned survived converting the bus, which empties it")
	}
}

// A flip to today clears status.busReplicas: a today CR has no bus.
func TestAFlipToTodayClearsTheBusServerCount(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	keys := sandboxKeysSecret(agent)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, keys).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d under next: %v", i+1, err)
		}
	}
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatal(err)
	}
	if fresh.Status.BusReplicas == 0 {
		t.Fatal("precondition: status.busReplicas not written under next")
	}
	fresh.Spec.Mode = ptr.To("today")
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after flip: %v", err)
	}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatal(err)
	}
	if fresh.Status.BusReplicas != 0 {
		t.Errorf("status.busReplicas = %d after the flip to today, want 0", fresh.Status.BusReplicas)
	}
}

// A NATS pod the hostname spread holds Pending is named with the scheduler's
// reason by the status pod scan, not blamed on the CR's RuntimeClass, which
// the pod does not ask for: the remedy that message gives (a gVisor node pool)
// does not add the node the spread waits for.
func TestAnUnschedulableBusServerIsNotBlamedOnTheRuntimeClass(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Availability: &agentv1alpha1.AvailabilitySpec{RuntimeClassName: ptr.To("gvisor")}}
	if len(requestedRuntimeClasses(agent)) == 0 {
		t.Fatal("precondition: the CR requests a RuntimeClass")
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent) + "-2", Namespace: agent.Namespace, Labels: map[string]string{"app": a2aNATSName(agent)}},
		Status: corev1.PodStatus{Phase: corev1.PodPending, Conditions: []corev1.PodCondition{{
			Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: "Unschedulable",
			Message: "0/2 nodes are available: 2 node(s) didn't match pod topology spread constraints.",
		}}},
	}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, pod).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	_, _, msg := r.getDeploymentStatusDetails(context.Background(), agent, true)
	if strings.Contains(msg, "RuntimeClass") || strings.Contains(msg, "gVisor") {
		t.Errorf("a bus server Pending under the spread is blamed on the RuntimeClass: %q", msg)
	}
	if !strings.Contains(msg, "topology spread") {
		t.Errorf("the scheduler's reason is not in the message: %q", msg)
	}
}

// The Ready message names a server short of the configured count, and says
// nothing when every server is Ready.
func TestTheReadyMessageNamesAMissingBusServer(t *testing.T) {
	agent := a2aTestAgent()
	sts := buildA2ANATSStatefulSet(agent, "h")
	sts.Status = appsv1.StatefulSetStatus{Replicas: 3, ReadyReplicas: 2}
	ready := func(name string) *corev1.Pod {
		return &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: map[string]string{"app": a2aNATSName(agent)}},
			Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}}}
	}
	pending := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent) + "-2", Namespace: agent.Namespace, Labels: map[string]string{"app": a2aNATSName(agent)}},
		Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Message: "0/2 nodes are available: 2 node(s) didn't match pod topology spread constraints."}}}}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(sts, ready(a2aNATSName(agent)+"-0"), ready(a2aNATSName(agent)+"-1"), pending).
		WithStatusSubresource(&appsv1.StatefulSet{}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	note := r.a2aNATSShortNote(context.Background(), agent)
	if !strings.Contains(note, "2 of 3 NATS servers") || !strings.Contains(note, a2aNATSName(agent)+"-2: 0/2 nodes are available") {
		t.Errorf("note = %q, want the count and the Pending server with the scheduler's reason", note)
	}
	if strings.Contains(note, "-0:") || strings.Contains(note, "-1:") {
		t.Errorf("note names a Ready server: %q", note)
	}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(sts), sts); err != nil {
		t.Fatal(err)
	}
	sts.Status.ReadyReplicas = 3
	if err := cl.Status().Update(context.Background(), sts); err != nil {
		t.Fatal(err)
	}
	if note := r.a2aNATSShortNote(context.Background(), agent); note != "" {
		t.Errorf("note on a fully Ready bus = %q, want none", note)
	}
}

// The pruned status.busReplicas record schedules its own reprobe and is
// forgotten with the CR.
func TestThePrunedBusReplicasRecordIsReprobedAndForgotten(t *testing.T) {
	r := &PlatformAgentReconciler{}
	agent := a2aTestAgent()
	r.prunedBusReplicasStatus.Store(client.ObjectKeyFromObject(agent), time.Now())
	if got := r.usageStatusRequeue(agent); got != usageStatusReprobeInterval {
		t.Errorf("requeue with a pruned busReplicas record = %s, want the reprobe interval %s", got, usageStatusReprobeInterval)
	}
	r.forgetUsageStatus(agent)
	if r.busReplicasStatusPruned(agent) {
		t.Error("the pruned busReplicas record outlived forgetUsageStatus")
	}
}

// A minAvailable another manager set on the live bus budget. A forced apply
// cannot remove a field it never owned, so without clearForeignPDBBudgetField
// the apply merges to both fields and is refused, and the bus step sits ahead
// of the callout, the fences and the provision Job in reconcileA2A.
func TestTheBusBudgetRecoversFromAForeignBudgetField(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	want := buildA2ANATSPDB(agent)
	live := &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: want.Name, Namespace: agent.Namespace},
		Spec:       policyv1.PodDisruptionBudgetSpec{MinAvailable: ptr.To(intstr.FromInt32(2)), Selector: want.Spec.Selector},
	}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, live).WithInterceptorFuncs(pdbSSAInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	if err := r.reconcileA2ANATSClusterObjects(ctx, agent); err != nil {
		t.Fatalf("reconcileA2ANATSClusterObjects: %v", err)
	}
	got := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(want), got); err != nil {
		t.Fatal(err)
	}
	if got.Spec.MinAvailable != nil || got.Spec.MaxUnavailable == nil || got.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("bus budget = maxUnavailable %v / minAvailable %v, want 1 / unset", got.Spec.MaxUnavailable, got.Spec.MinAvailable)
	}
}

// OrderedReady creates server 1 and its claim only once server 0 is Ready, so
// a new cluster's StatefulSet deleted before then leaves only claim 0, as a
// single server does. The recorded count tells them apart; with nothing
// recorded (a bus back from today) the lone claim reads as one server.
func TestALoneClaimKeepsTheRecordedServerCount(t *testing.T) {
	for _, tc := range []struct {
		recorded, want int32
	}{{3, 3}, {1, 1}, {0, 1}} {
		agent := a2aTestAgent()
		agent.Status.BusReplicas = tc.recorded
		claim := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
			Name: a2aNATSDataClaimName(agent, 0), Namespace: agent.Namespace,
			Labels: buildA2ANATSStatefulSet(agent, "h").Spec.VolumeClaimTemplates[0].Labels,
		}}
		r := &PlatformAgentReconciler{Client: fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, claim).Build(), Scheme: setupScheme(), Recorder: record.NewFakeRecorder(8)}
		converting, err := r.resolveA2ABusReplicas(context.Background(), agent)
		if err != nil {
			t.Fatal(err)
		}
		if agent.Status.BusReplicas != tc.want || converting {
			t.Errorf("recorded %d, lone claim 0: status.busReplicas = %d converting = %v, want %d and no conversion", tc.recorded, agent.Status.BusReplicas, converting, tc.want)
		}
	}
}

// Pods are not watched, so the short server's note follows its pod only if a
// short bus requeues on a provisioned, Ready pass; and the note skips a pod
// on its way out, which its successor replaces.
func TestAShortBusRequeuesAndItsNoteSkipsATerminatingPod(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	sts := buildA2ANATSStatefulSet(agent, "h")
	sts.Status = appsv1.StatefulSetStatus{Replicas: 3, ReadyReplicas: 2}
	leaving := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: a2aNATSName(agent) + "-2", Namespace: agent.Namespace, Labels: map[string]string{"app": a2aNATSName(agent)},
		DeletionTimestamp: ptr.To(metav1.Now()), Finalizers: []string{"test/hold"},
	}}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(sts, leaving).WithStatusSubresource(&appsv1.StatefulSet{}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	settled := a2aProvisionState{done: true}
	if !r.a2aBusNeedsRecheck(ctx, agent, settled) {
		t.Error("a provisioned bus one server short does not requeue; its Ready note would stand stale until the 15-minute re-probe")
	}
	if note := r.a2aNATSShortNote(ctx, agent); strings.Contains(note, "-2:") {
		t.Errorf("note names a terminating pod: %q", note)
	}
	sts.Status.ReadyReplicas = 3
	if err := cl.Status().Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
	if r.a2aBusNeedsRecheck(ctx, agent, settled) {
		t.Error("a provisioned, fully Ready bus requeues at 30s")
	}
}

// A converting pass that fails after the StatefulSet apply writes no status,
// so the CR keeps BusProvisioned and the old count. The retry finds the
// StatefulSet already at the new count, no conversion by the StatefulSet's
// account; the persisted count still differs, and the record is dropped.
func TestAFailedConvertingPassStillDropsBusProvisionedOnTheRetry(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	failCallout := false
	base := fakeServerSideApplyInterceptors()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if _, isDep := obj.(*appsv1.Deployment); failCallout && isDep && obj.GetName() == a2aCalloutName(agent) {
					return fmt.Errorf("injected: the apply after the bus StatefulSet fails")
				}
				return base.Patch(ctx, c, obj, patch, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(32), busConversionAllowed: true}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if agent.Status.BusReplicas != 3 || !busProvisioned(agent) {
		t.Fatalf("precondition: status.busReplicas = %d, BusProvisioned = %v; want 3 and set", agent.Status.BusReplicas, busProvisioned(agent))
	}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(1)}}

	// The converting pass, on a copy: it fails, so none of its in-memory
	// status reaches the CR the retry reads.
	failCallout = true
	if _, err := r.reconcileA2A(ctx, agent.DeepCopy()); err == nil {
		t.Fatal("precondition: the injected failure did not fail the converting pass")
	}
	failCallout = false
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil || ptr.Deref(sts.Spec.Replicas, 0) != 1 {
		t.Fatalf("precondition: the failed pass did not land the StatefulSet at 1 (err=%v replicas=%v)", err, sts.Spec.Replicas)
	}

	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if busProvisioned(agent) {
		t.Error("BusProvisioned survived the retry of a converting pass that failed after the StatefulSet apply; the CR reads Ready over an emptied bus")
	}
}

// Converting an existing bus empties it, and the migration that keeps the
// data is #4225, which lifts the gate in the same change. Until then a set
// busReplicas that disagrees with the bus is refused in both directions,
// whether the bus is read from its StatefulSet or from the claims a deleted
// one left: the count stays, a Warning says so once, and the field still
// decides for a bus that does not exist yet.
func TestAnExistingBusRefusesAConversion(t *testing.T) {
	ctx := context.Background()
	claim := func(agent *agentv1alpha1.PlatformAgent, ordinal int) *corev1.PersistentVolumeClaim {
		return &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
			Name: a2aNATSDataClaimName(agent, ordinal), Namespace: agent.Namespace,
			Labels: buildA2ANATSStatefulSet(agent, "h").Spec.VolumeClaimTemplates[0].Labels,
		}}
	}
	sts := func(agent *agentv1alpha1.PlatformAgent, replicas int32) *appsv1.StatefulSet {
		s := buildA2ANATSStatefulSet(agent, "h")
		s.Spec.Replicas = ptr.To(replicas)
		return s
	}
	for _, tc := range []struct {
		name    string
		spec    int
		objects func(*agentv1alpha1.PlatformAgent) []client.Object
		want    int32
		refused bool
	}{
		{"one to three", 3, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 1)} }, 1, true},
		{"three to one", 1, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 3)} }, 3, true},
		{"three to one over a deleted cluster's claims", 1, func(a *agentv1alpha1.PlatformAgent) []client.Object {
			return []client.Object{claim(a, 0), claim(a, 1)}
		}, 3, true},
		{"one to three over a deleted single server's claim", 3, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{claim(a, 0)} }, 1, true},
		{"the count it runs", 3, func(a *agentv1alpha1.PlatformAgent) []client.Object { return []client.Object{sts(a, 3)} }, 3, false},
		{"a new bus takes the field", 1, func(*agentv1alpha1.PlatformAgent) []client.Object { return nil }, 1, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(tc.spec)}}
			rec := record.NewFakeRecorder(8)
			r := &PlatformAgentReconciler{Client: fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(append([]client.Object{agent}, tc.objects(agent)...)...).Build(), Scheme: setupScheme(), Recorder: rec}
			converting, err := r.resolveA2ABusReplicas(ctx, agent)
			if err != nil {
				t.Fatal(err)
			}
			if converting {
				t.Error("resolveA2ABusReplicas reports a conversion with the gate shut")
			}
			if agent.Status.BusReplicas != tc.want || a2aNATSReplicas(agent) != int(tc.want) || int(*buildA2ANATSStatefulSet(agent, "h").Spec.Replicas) != int(tc.want) {
				t.Errorf("status %d, a2aNATSReplicas %d, rendered %d; want %d throughout", agent.Status.BusReplicas, a2aNATSReplicas(agent), *buildA2ANATSStatefulSet(agent, "h").Spec.Replicas, tc.want)
			}
			msg := a2aBusConversionRefusedMessage(agent)
			if tc.refused != (msg != "") {
				t.Errorf("condition message %q, want present = %v", msg, tc.refused)
			}
			var events []string
			for len(rec.Events) > 0 {
				events = append(events, <-rec.Events)
			}
			if tc.refused != (len(events) == 1 && strings.Contains(events[0], a2aBusConversionRefusedReason)) {
				t.Errorf("events %q, want one %s = %v", events, a2aBusConversionRefusedReason, tc.refused)
			}
			if tc.refused && !strings.Contains(msg, "#4225") {
				t.Errorf("the refusal does not name the migration it waits for: %q", msg)
			}
			// Said once: with the condition standing, the next pass is quiet.
			if tc.refused {
				setA2ABusConversionRefusedCondition(agent, msg, metav1.Now())
				if _, err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
					t.Fatal(err)
				}
				if len(rec.Events) != 0 {
					t.Errorf("a standing refusal is re-announced on every pass: %q", <-rec.Events)
				}
			}
		})
	}
}

// The whole pass, both directions: the StatefulSet stays at the count the
// bus runs, including on the retry of a pass that failed after its
// StatefulSet apply, and BusProvisioned stands, because nothing converted.
func TestARefusedConversionRendersNothingTowardTheNewCount(t *testing.T) {
	for _, from := range []int32{1, 3} {
		to := 4 - int(from)
		agent := a2aTestAgent()
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(int(from))}}
		scheme := setupScheme()
		failCallout := false
		base := fakeServerSideApplyInterceptors()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, discordBotSecret(agent)).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
			WithInterceptorFuncs(interceptor.Funcs{Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if _, isDep := obj.(*appsv1.Deployment); failCallout && isDep && obj.GetName() == a2aCalloutName(agent) {
					return fmt.Errorf("injected: the apply after the bus StatefulSet fails")
				}
				return base.Patch(ctx, c, obj, patch, opts...)
			}}).Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(32)}
		ctx := context.Background()
		theCalloutIsServing(t, ctx, cl, r, agent)
		setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
		if _, err := r.reconcileA2A(ctx, agent); err != nil {
			t.Fatal(err)
		}
		replicas := func() int32 {
			t.Helper()
			sts := &appsv1.StatefulSet{}
			if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil {
				t.Fatal(err)
			}
			return ptr.Deref(sts.Spec.Replicas, 0)
		}
		if got := replicas(); got != from {
			t.Fatalf("precondition: the bus runs %d, want %d", got, from)
		}
		agent.Spec.Harness.Tuning.BusReplicas = ptr.To(to)
		failCallout = true
		if _, err := r.reconcileA2A(ctx, agent.DeepCopy()); err == nil {
			t.Fatal("precondition: the injected failure did not fail the pass")
		}
		if got := replicas(); got != from {
			t.Errorf("%d to %d, failed pass: the StatefulSet went to %d", from, to, got)
		}
		failCallout = false
		if _, err := r.reconcileA2A(ctx, agent); err != nil {
			t.Fatal(err)
		}
		if got := replicas(); got != from || agent.Status.BusReplicas != from {
			t.Errorf("%d to %d, retry: StatefulSet %d, status.busReplicas %d; want %d", from, to, got, agent.Status.BusReplicas, from)
		}
		if !busProvisioned(agent) {
			t.Errorf("%d to %d: BusProvisioned dropped although nothing converted", from, to)
		}
	}
}

// The refusal is a condition the status write carries and clears, beside a
// Ready that stays on the bus that runs.
func TestARefusedConversionIsAConditionThatClears(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(1)}}
	running := buildA2ANATSStatefulSet(agent, "h")
	running.Spec.Replicas = ptr.To(int32(3))
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, running).WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(8)}
	read := func() *metav1.Condition {
		t.Helper()
		live := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
			t.Fatal(err)
		}
		return meta.FindStatusCondition(live.Status.Conditions, a2aBusConversionRefusedConditionType)
	}
	if _, err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{done: true}); err != nil {
		t.Fatal(err)
	}
	if c := read(); c == nil || c.Status != metav1.ConditionTrue || c.Message != BusConversionRefusedMessage(1, 3) {
		t.Fatalf("condition after a refused 3 to 1 = %+v, want True with the shared message", c)
	}
	agent.Spec.Harness.Tuning.BusReplicas = nil
	if _, err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{done: true}); err != nil {
		t.Fatal(err)
	}
	if c := read(); c != nil {
		t.Errorf("condition after removing the field = %+v, want it gone", c)
	}
}

// The gate has one way in, and it is not a production one: in non-test files
// of this package busConversionAllowed appears only where the reconciler
// declares it and where resolveA2ABusReplicas reads it, so no flag, env var or
// constructor can turn a conversion on before #4225 brings the migration with
// it. A set, a composite-literal key, an address taken (a flag binding) and
// any further name all fail.
func TestNothingButTestsAllowsABusConversion(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	declared, total := 0, 0
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		parsed, err := parser.ParseFile(token.NewFileSet(), f, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(parsed, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.Field:
				for _, name := range n.Names {
					if name.Name == "busConversionAllowed" {
						declared++
					}
				}
			case *ast.KeyValueExpr:
				if id, ok := n.Key.(*ast.Ident); ok && id.Name == "busConversionAllowed" {
					t.Errorf("%s sets busConversionAllowed in a composite literal", f)
				}
			case *ast.AssignStmt:
				for _, lhs := range n.Lhs {
					ast.Inspect(lhs, func(ln ast.Node) bool {
						if sel, ok := ln.(*ast.SelectorExpr); ok && sel.Sel.Name == "busConversionAllowed" {
							t.Errorf("%s assigns busConversionAllowed", f)
						}
						return true
					})
				}
			case *ast.UnaryExpr:
				if n.Op == token.AND {
					if sel, ok := ast.Unparen(n.X).(*ast.SelectorExpr); ok && sel.Sel.Name == "busConversionAllowed" {
						t.Errorf("%s takes the address of busConversionAllowed", f)
					}
				}
			case *ast.Ident:
				if n.Name == "busConversionAllowed" {
					total++
				}
			}
			return true
		})
	}
	if declared != 1 || total != 2 {
		t.Fatalf("busConversionAllowed is declared %d times and named %d times in non-test files, want 1 declaration and 1 read (2 names); this check would be vacuous or the gate has a new way in", declared, total)
	}
}

// A StatefulSet scaled by hand outside 1 and 3 is still the bus last
// resolved: a single server scaled to 2 (claims -0 and -1, which read like a
// cluster short one) or to 0 (claims gone) stays one server, and a cluster
// stays three, so neither the default nor a set field converts it.
func TestABusScaledByHandKeepsItsRecordedCount(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name     string
		recorded int32
		scaled   int32
		claims   []int
		spec     *int
	}{
		{"one server scaled to 2", 1, 2, []int{0, 1}, nil},
		{"one server scaled to 0", 1, 0, nil, nil},
		{"one server scaled to 2, field asks 3", 1, 2, []int{0, 1}, ptr.To(3)},
		{"three servers scaled to 0", 3, 0, nil, nil},
		{"three servers scaled to 2, field asks 1", 3, 2, []int{0, 1}, ptr.To(1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			agent.Status.BusReplicas = tc.recorded
			if tc.spec != nil {
				agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: tc.spec}}
			}
			sts := buildA2ANATSStatefulSet(agent, "h")
			sts.Spec.Replicas = ptr.To(tc.scaled)
			objs := []client.Object{agent, sts}
			for _, o := range tc.claims {
				objs = append(objs, &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
					Name: a2aNATSDataClaimName(agent, o), Namespace: agent.Namespace,
					Labels: sts.Spec.VolumeClaimTemplates[0].Labels,
				}})
			}
			r := &PlatformAgentReconciler{Client: fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build(), Scheme: setupScheme(), Recorder: record.NewFakeRecorder(8)}
			if _, err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
				t.Fatal(err)
			}
			if agent.Status.BusReplicas != tc.recorded || a2aNATSReplicas(agent) != int(tc.recorded) {
				t.Errorf("status.busReplicas %d, rendered %d; want the recorded %d", agent.Status.BusReplicas, a2aNATSReplicas(agent), tc.recorded)
			}
		})
	}
}
