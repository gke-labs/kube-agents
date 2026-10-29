package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
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
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

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
		{"default", a2aTestAgent(), 3, 3, true, "/healthz?js-server-only=true"},
		{"busReplicas 3", a2aTestAgentWithBusReplicas(3), 3, 3, true, "/healthz?js-server-only=true"},
		{"busReplicas 1", a2aTestAgentWithBusReplicas(1), 1, 1, false, "/healthz"},
		// Past the CRD's enum: the default, never a two-server cluster.
		{"busReplicas 2 past the schema", a2aTestAgentWithBusReplicas(2), 3, 3, true, "/healthz?js-server-only=true"},
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

// 6222 is admitted from the bus's own servers and from nothing else, and only
// on a multi-server bus.
func TestTheBusFenceAdmitsRoutesFromItsOwnServersOnly(t *testing.T) {
	routeRules := func(agent *agentv1alpha1.PlatformAgent) []int {
		var idx []int
		for i, rule := range buildA2ANATSNetworkPolicy(agent).Spec.Ingress {
			for _, p := range rule.Ports {
				if p.Port != nil && p.Port.IntValue() == a2aNATSRoutePort {
					idx = append(idx, i)
				}
			}
		}
		return idx
	}
	if got := routeRules(a2aTestAgentWithBusReplicas(1)); len(got) != 0 {
		t.Errorf("a single-server bus's fence admits 6222 (rules %v)", got)
	}
	agent := a2aTestAgent()
	np := buildA2ANATSNetworkPolicy(agent)
	got := routeRules(agent)
	if len(got) != 1 {
		t.Fatalf("a cluster's fence has %d rules naming 6222, want exactly one", len(got))
	}
	rule := np.Spec.Ingress[got[0]]
	if len(rule.Ports) != 1 {
		t.Errorf("the route rule admits other ports too: %+v", rule.Ports)
	}
	if len(rule.From) != 1 || rule.From[0].PodSelector == nil || rule.From[0].NamespaceSelector != nil ||
		len(rule.From[0].PodSelector.MatchLabels) != 1 || rule.From[0].PodSelector.MatchLabels["app"] != a2aNATSName(agent) {
		t.Errorf("the route rule's peers are not exactly the bus's own pods: %+v", rule.From)
	}
}

// The route user is the servers' login to each other. It appears in the
// cluster block and nowhere else: not in an account, not in auth_users, so it
// cannot open a client connection.
func TestTheRouteUserLivesOnlyInTheClusterBlock(t *testing.T) {
	conf := renderA2ANATSConf(a2aTestAgent(), func(k string) string { return "pw-" + k }, &a2aCalloutKeys{IssuerPublic: "A", XKeyPublic: "X"})
	block := a2aClusterBlockRe.FindString(conf)
	if !strings.Contains(block, "user: "+a2aNATSRouteUser) || !strings.Contains(block, `password: "pw-`+a2aRoutePasswordKey+`"`) {
		t.Errorf("the cluster block has no route user and password:\n%s", block)
	}
	rest := a2aClusterBlockRe.ReplaceAllString(conf, "")
	if regexp.MustCompile(`user: ` + a2aNATSRouteUser + `\b`).MatchString(rest) {
		t.Error("the route user appears outside the cluster block")
	}
	if strings.Contains(rest, "pw-"+a2aRoutePasswordKey) {
		t.Error("the route password appears outside the cluster block")
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
		end := strings.Index(script[loc[0]:], "\n\n")
		if end < 0 {
			end = len(script) - loc[0]
		}
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
// behaviour the render's SANs rely on. A fourth server with the wrong route
// password does not join.
func TestTheRenderedClusterFormsOverVerifiedRoutes(t *testing.T) {
	agent := a2aTestAgent()
	creds := a2aFullCreds("c", "1")
	conf := string(buildA2ANATSConfigSecret(agent, creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	if !a2aClusterBlockRe.MatchString(conf) {
		t.Fatal("the default render has no cluster block")
	}
	routeHost := a2aNATSRoutesName(agent) + "." + agent.Namespace + ".svc." + a2aNATSClusterDomain + ":" + strconv.Itoa(a2aNATSRoutePort)
	if !strings.Contains(conf, routeHost) {
		t.Fatalf("the cluster block does not route to %s", routeHost)
	}

	ports := a2aFreePorts(t, 4)
	start := func(i int, routePassword string) *natsserver.Server {
		t.Helper()
		c := strings.Replace(conf, "server_name: $POD_NAME", "server_name: a2a-test-nats-"+strconv.Itoa(i), 1)
		c = strings.Replace(c, `pid_file: "`+a2aNATSPidFile+`"`, `pid_file: "`+filepath.Join(t.TempDir(), "pid")+`"`, 1)
		c = strings.Replace(c, routeHost, "localhost:"+strconv.Itoa(ports[0]), 1)
		c = strings.Replace(c, "port: "+strconv.Itoa(a2aNATSRoutePort), "port: "+strconv.Itoa(ports[i]), 1)
		if routePassword != "" {
			// Only the cluster block's own password: the one this server
			// would accept from a peer, and the one it dials with.
			c = strings.ReplaceAll(c, string(creds.Data[a2aRoutePasswordKey]), routePassword)
		}
		path := filepath.Join(t.TempDir(), "nats.conf")
		if err := os.WriteFile(path, []byte(a2aWithTestTLS(t, c)), 0o600); err != nil {
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
	}

	intruder := start(3, "0123456789abcdef0123456789abcdef")
	time.Sleep(3 * time.Second)
	if peers := a2aRoutePeers(t, intruder); len(peers) != 0 {
		t.Errorf("a server with the wrong route password joined, peering with %v", peers)
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
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			if tc.spec != nil {
				agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: tc.spec}}
			}
			objs := append([]client.Object{agent}, tc.objects(agent)...)
			rec := record.NewFakeRecorder(8)
			r := &PlatformAgentReconciler{Client: fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build(), Scheme: setupScheme(), Recorder: rec}
			if err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
				t.Fatal(err)
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
			if err := r.resolveA2ABusReplicas(ctx, agent); err != nil {
				t.Fatal(err)
			}
			if len(rec.Events) != 0 {
				t.Error("the conversion Event repeated on the next pass")
			}
		})
	}
}
