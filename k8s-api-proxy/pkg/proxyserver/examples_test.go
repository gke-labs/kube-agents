package proxyserver

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/util/intstr"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	examplesDir            = "../../examples"
	egressGatewayDir       = examplesDir + "/egress-gateway"
	egressGatewayManifest  = egressGatewayDir + "/proxy.yaml"
	egressGatewayRule      = egressGatewayDir + "/egress-rule.yaml"
	egressGatewayConfig    = egressGatewayDir + "/sandbox-kubeconfig.yaml"
	egressGatewayReadme    = egressGatewayDir + "/README.md"
	localBridgeReadme      = examplesDir + "/local-bridge/README.md"
	moduleReadme           = "../../README.md"
	exampleJWTFlagPrefix   = "jwt-"
	exampleManifestSuffix  = ".yaml"
	exampleDecodeBuffer    = 4096
	exampleProxyRepository = "/kube-proxy"
	exampleImageDigestSep  = "@"
	exampleImageTagSep     = ":"
	exampleGoSuffix        = ".go"
	exampleGoTestSuffix    = "_test.go"
	exampleKindService     = "Service"
	examplePlaceholder     = "placeholder"
	exampleLoopbackPrefix  = "127.0.0.1:"
	exampleFence           = "```"
	exampleFlagValueSep    = "="
	exampleShellEmptyValue = `""`
	exampleFlagPrefix      = "--"
	exampleArgsEntryPrefix = "- --"
	examplePathSeparator   = "/"
	exampleProbeScheme     = "HTTPS"
	exampleKindPolicy      = "NetworkPolicy"
	exampleSecretCertFile  = "tls.crt"
	exampleSecretKeyFile   = "tls.key"
	kubeTokenFileFlag      = "kube-token-file"
	kubeCAFileFlag         = "kube-ca-file"
	// The fenced flag blocks each example README holds.
	egressGatewayReadmeBlocks = 3
	localBridgeReadmeBlocks   = 2
	// The module README's command blocks: one for each certificate mode.
	moduleReadmeBlocks = 2
)

// binaryEnvPrefixes are the prefixes of the variables the binary owns; proxyEnvVars lists
// the ones it reads.
var binaryEnvPrefixes = []string{"KUBE_PROXY_", "CREDENTIAL_PROXY_"}

// exampleFlagLine matches a line that holds one flag: an entry of a container's args, or a
// line of a shell command with or without its continuation backslash.
var exampleFlagLine = regexp.MustCompile(`^\s*(?:- )?(--[a-z][a-z0-9-]*(?:=\S*)?)(?:\s+\\)?\s*$`)

// exampleEnvLiteral matches a Go string literal that is the name of a variable under one of
// the binary's prefixes.
var exampleEnvLiteral = regexp.MustCompile(`"((?:KUBE_PROXY|CREDENTIAL_PROXY)_[A-Z0-9_]+)"`)

type exampleProbe struct {
	HTTPGet struct {
		Path   string             `json:"path"`
		Port   intstr.IntOrString `json:"port"`
		Scheme string             `json:"scheme"`
	} `json:"httpGet"`
}

type exampleContainer struct {
	Name  string   `json:"name"`
	Image string   `json:"image"`
	Args  []string `json:"args"`
	Env   []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"env"`
	Ports []struct {
		Name          string `json:"name"`
		ContainerPort int    `json:"containerPort"`
	} `json:"ports"`
	ReadinessProbe *exampleProbe `json:"readinessProbe"`
	LivenessProbe  *exampleProbe `json:"livenessProbe"`
	VolumeMounts   []struct {
		Name      string `json:"name"`
		MountPath string `json:"mountPath"`
	} `json:"volumeMounts"`
}

// port resolves a port given by number or by the name of one of the container's ports;
// 0 when the name is not one of them.
func (c exampleContainer) port(ref intstr.IntOrString) int {
	if ref.Type == intstr.Int {
		return ref.IntValue()
	}
	for _, port := range c.Ports {
		if port.Name == ref.StrVal {
			return port.ContainerPort
		}
	}
	return 0
}

// mountedFile splits an in-pod path into the volume mounted over it and the file's path
// within that volume; ok is false when no volumeMount of the container holds the path.
func (c exampleContainer) mountedFile(path string) (volume, file string, ok bool) {
	for _, mount := range c.VolumeMounts {
		if rest, found := strings.CutPrefix(path, mount.MountPath+examplePathSeparator); found {
			return mount.Name, rest, true
		}
	}
	return "", "", false
}

type exampleVolume struct {
	Name      string `json:"name"`
	Projected struct {
		Sources []struct {
			PodCertificate *struct {
				CredentialBundlePath string `json:"credentialBundlePath"`
				KeyPath              string `json:"keyPath"`
				CertificateChainPath string `json:"certificateChainPath"`
			} `json:"podCertificate"`
			ServiceAccountToken *struct {
				Path string `json:"path"`
			} `json:"serviceAccountToken"`
			ConfigMap *struct {
				Items []struct {
					Path string `json:"path"`
				} `json:"items"`
			} `json:"configMap"`
		} `json:"sources"`
	} `json:"projected"`
}

// files are the paths a projected volume's sources write.
func (v exampleVolume) files() []string {
	var files []string
	for _, source := range v.Projected.Sources {
		if cert := source.PodCertificate; cert != nil {
			files = append(files, cert.CredentialBundlePath, cert.KeyPath, cert.CertificateChainPath)
		}
		if token := source.ServiceAccountToken; token != nil {
			files = append(files, token.Path)
		}
		if configMap := source.ConfigMap; configMap != nil {
			for _, item := range configMap.Items {
				files = append(files, item.Path)
			}
		}
	}
	return slices.DeleteFunc(files, func(file string) bool { return file == "" })
}

type examplePodSpec struct {
	Containers     []exampleContainer `json:"containers"`
	InitContainers []exampleContainer `json:"initContainers"`
	Volumes        []exampleVolume    `json:"volumes"`
}

// proxyContainers returns the containers and init containers of the pod that run the
// proxy's image, whether it is pinned by tag or by digest.
func (p examplePodSpec) proxyContainers() []exampleContainer {
	var containers []exampleContainer
	for _, container := range slices.Concat(p.Containers, p.InitContainers) {
		repository, _, _ := strings.Cut(container.Image, exampleImageDigestSep)
		if tag := strings.LastIndex(repository, exampleImageTagSep); tag > strings.LastIndex(repository, examplePathSeparator) {
			repository = repository[:tag]
		}
		if strings.HasSuffix(repository, exampleProxyRepository) {
			containers = append(containers, container)
		}
	}
	return containers
}

// exampleObject holds the fields these tests read from any document in an example: a
// workload's pod, a Service's name and ports, a NetworkPolicy's ports, a kubeconfig, and
// the egress rule.
type exampleObject struct {
	Kind     string `json:"kind"`
	Metadata struct {
		Name      string `json:"name"`
		Namespace string `json:"namespace"`
	} `json:"metadata"`
	Spec struct {
		// A bare Pod's containers and volumes.
		examplePodSpec
		Template struct {
			Spec examplePodSpec `json:"spec"`
		} `json:"template"`
		JobTemplate struct {
			Spec struct {
				Template struct {
					Spec examplePodSpec `json:"spec"`
				} `json:"template"`
			} `json:"spec"`
		} `json:"jobTemplate"`
		Ports []struct {
			Port       int                `json:"port"`
			TargetPort intstr.IntOrString `json:"targetPort"`
		} `json:"ports"`
		Ingress []struct {
			Ports []struct {
				Port intstr.IntOrString `json:"port"`
			} `json:"ports"`
		} `json:"ingress"`
	} `json:"spec"`
	Clusters []struct {
		Cluster struct {
			Server string `json:"server"`
		} `json:"cluster"`
	} `json:"clusters"`
	Users []struct {
		User struct {
			Token string `json:"token"`
		} `json:"user"`
	} `json:"users"`
	EgressRule struct {
		Host   string `json:"host"`
		Port   int    `json:"port"`
		Scheme string `json:"scheme"`
		Token  struct {
			Issuer   string `json:"issuer"`
			Audience string `json:"audience"`
		} `json:"token"`
	} `json:"egressRule"`
}

// exampleObjects decodes every YAML document in path.
func exampleObjects(t *testing.T, path string) []exampleObject {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	decoder := utilyaml.NewYAMLOrJSONDecoder(f, exampleDecodeBuffer)
	var objects []exampleObject
	for {
		var object exampleObject
		if err := decoder.Decode(&object); err != nil {
			if errors.Is(err, io.EOF) {
				return objects
			}
			t.Fatalf("%s: %v", path, err)
		}
		objects = append(objects, object)
	}
}

// proxyPods returns the pods in objects that run the proxy's image: a workload's pod
// template, a CronJob's, and a bare Pod.
func proxyPods(objects []exampleObject) []examplePodSpec {
	var pods []examplePodSpec
	for _, object := range objects {
		for _, pod := range []examplePodSpec{object.Spec.examplePodSpec, object.Spec.Template.Spec, object.Spec.JobTemplate.Spec.Template.Spec} {
			if len(pod.proxyContainers()) > 0 {
				pods = append(pods, pod)
			}
		}
	}
	return pods
}

// withLocalFiles points the flags whose files the binary reads at startup at files that
// exist here, in place of an example's in-pod paths.
func withLocalFiles(t *testing.T, args []string) []string {
	t.Helper()
	caFile := newTLSUpstream(t).caFile
	local := map[string]string{
		exampleFlagPrefix + upstreamTokenFileFlag:  writeTokenFile(t, testUpstreamToken),
		exampleFlagPrefix + upstreamCAFileFlag:     caFile,
		exampleFlagPrefix + jwtCAFileFlag:          caFile,
		exampleFlagPrefix + kubeTokenFileFlag:      writeTokenFile(t, testUpstreamToken),
		exampleFlagPrefix + kubeCAFileFlag:         caFile,
		exampleFlagPrefix + mtlsClientCAFileFlag:   caFile,
		exampleFlagPrefix + headerClientCAFileFlag: caFile,
	}
	out := make([]string, 0, len(args))
	for _, arg := range args {
		name, _, _ := strings.Cut(arg, exampleFlagValueSep)
		if path, ok := local[name]; ok {
			arg = name + exampleFlagValueSep + path
		}
		out = append(out, arg)
	}
	return out
}

// buildFromArgs parses args and reads the environment as Run does, and
// builds its handler from them, which makes every check the binary makes of its
// configuration before it listens. It returns the configuration as the example wrote it,
// and the error the binary would stop with.
func buildFromArgs(t *testing.T, args []string) (proxyConfig, error) {
	t.Helper()
	parse := func(args []string) proxyConfig {
		var cfg proxyConfig
		flags := flag.NewFlagSet(binaryName, flag.ContinueOnError)
		flags.SetOutput(io.Discard)
		if err := parseFlags(flags, args, &cfg); err != nil {
			t.Fatalf("parseFlags(%q): %v", args, err)
		}
		if flags.NArg() != 0 {
			t.Fatalf("%q holds arguments that are not flags: %q", args, flags.Args())
		}
		loadEnvOnlyConfig(&cfg)
		return cfg
	}
	written := parse(args)
	cfg := parse(withLocalFiles(t, args))
	// As newServer does for Run: the example's listener is the binary's own, so a
	// certificate mode needs its serving files.
	cfg.clientCert.ownListener = true
	mode, err := k8sapiproxy.ParseMode(cfg.modeRaw)
	if err != nil {
		return written, err
	}
	logger := slog.New(slog.DiscardHandler)
	_, err = buildHandler(context.Background(), cfg, mode, logger, k8sapiproxy.NewMetrics(), nil)
	return written, err
}

// startFromArgs is buildFromArgs for arguments the binary must start with.
func startFromArgs(t *testing.T, args []string) proxyConfig {
	t.Helper()
	cfg, err := buildFromArgs(t, args)
	if err != nil {
		t.Fatalf("the binary does not start with %q: %v", args, err)
	}
	return cfg
}

// buildContainer sets the container's variables and builds the handler from its args,
// after refusing a variable under the binary's prefixes that the binary does not read.
func buildContainer(t *testing.T, container exampleContainer) (proxyConfig, error) {
	t.Helper()
	clearHostEnv(t)
	for _, variable := range container.Env {
		owned := slices.ContainsFunc(binaryEnvPrefixes, func(prefix string) bool {
			return strings.HasPrefix(variable.Name, prefix)
		})
		if owned && !slices.Contains(proxyEnvVars, variable.Name) {
			t.Errorf("container %s sets %s, which the binary does not read", container.Name, variable.Name)
		}
		t.Setenv(variable.Name, variable.Value)
	}
	return buildFromArgs(t, container.Args)
}

// startContainer is buildContainer for a container the binary must start in.
func startContainer(t *testing.T, container exampleContainer) proxyConfig {
	t.Helper()
	cfg, err := buildContainer(t, container)
	if err != nil {
		t.Fatalf("the binary does not start in container %s: %v", container.Name, err)
	}
	return cfg
}

// exampleBlock is a fenced block of a README: its text, and the flags on its lines.
type exampleBlock struct {
	text  string
	flags []string
}

// flagBlocks returns each fenced block in the Markdown file at path that holds a flag.
func flagBlocks(t *testing.T, path string) []exampleBlock {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	var blocks []exampleBlock
	var block exampleBlock
	fenced := false
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(strings.TrimSpace(line), exampleFence) {
			if fenced && len(block.flags) > 0 {
				blocks = append(blocks, block)
			}
			fenced, block = !fenced, exampleBlock{}
			continue
		}
		if !fenced {
			continue
		}
		block.text += line + "\n"
		match := exampleFlagLine.FindStringSubmatch(line)
		if match == nil {
			// A flag line the pattern misses would go unchecked.
			trimmed := strings.TrimSpace(line)
			if strings.HasPrefix(trimmed, exampleFlagPrefix) || strings.HasPrefix(trimmed, exampleArgsEntryPrefix) {
				t.Errorf("%s: %q looks like a flag but does not match %s", path, line, exampleFlagLine)
			}
			continue
		}
		// A shell's empty string is an empty value to the binary.
		block.flags = append(block.flags, strings.ReplaceAll(match[1], exampleShellEmptyValue, ""))
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return blocks
}

// flagName is the name of the flag in arg, without its dashes and value.
func flagName(arg string) string {
	name, _, _ := strings.Cut(strings.TrimPrefix(arg, exampleFlagPrefix), exampleFlagValueSep)
	return name
}

// flagNames are the names of the flags in args, sorted.
func flagNames(args []string) []string {
	names := make([]string, 0, len(args))
	for _, arg := range args {
		names = append(names, flagName(arg))
	}
	slices.Sort(names)
	return names
}

// replaceFlags returns args without the flags named in drop and without those add sets,
// followed by add: the edit a README describes to a manifest's args.
func replaceFlags(args, drop, add []string) []string {
	drop = append(slices.Clone(drop), flagNames(add)...)
	var out []string
	for _, arg := range args {
		if !slices.Contains(drop, flagName(arg)) {
			out = append(out, arg)
		}
	}
	return append(out, add...)
}

// egressGatewayPod returns the documents of the egress gateway manifest, and the pod and
// the one container in it that run the proxy.
func egressGatewayPod(t *testing.T) ([]exampleObject, examplePodSpec, exampleContainer) {
	t.Helper()
	objects := exampleObjects(t, egressGatewayManifest)
	pods := proxyPods(objects)
	if len(pods) != 1 || len(pods[0].proxyContainers()) != 1 {
		t.Fatalf("%s: want one workload with one container running the proxy, got %d workloads", egressGatewayManifest, len(pods))
	}
	return objects, pods[0], pods[0].proxyContainers()[0]
}

// checkMountedFiles fails for a flag in args whose value is an in-pod path that no
// volumeMount of the container holds, whose mount names a volume the pod lacks, or that
// names a file its projected volume does not write. A volume that is not projected is
// checked for the mount only.
func checkMountedFiles(t *testing.T, args []string, pod examplePodSpec, container exampleContainer) {
	t.Helper()
	for _, arg := range args {
		_, path, _ := strings.Cut(arg, exampleFlagValueSep)
		if !strings.HasPrefix(path, examplePathSeparator) {
			continue
		}
		volumeName, file, ok := container.mountedFile(path)
		if !ok {
			t.Errorf("%s names a path under no volumeMount of container %s", arg, container.Name)
			continue
		}
		index := slices.IndexFunc(pod.Volumes, func(volume exampleVolume) bool { return volume.Name == volumeName })
		if index < 0 {
			t.Errorf("%s is under a volumeMount of volume %q, which the pod does not define", arg, volumeName)
			continue
		}
		volume := pod.Volumes[index]
		if len(volume.Projected.Sources) > 0 && !slices.Contains(volume.files(), file) {
			t.Errorf("%s: volume %s writes %q, not %q", arg, volumeName, volume.files(), file)
		}
	}
}

// Every manifest under examples/ that runs the proxy's image passes it flags and variables
// the binary has, in a combination it starts with.
func TestExampleManifestsStartTheBinary(t *testing.T) {
	started := 0
	err := filepath.WalkDir(examplesDir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || !strings.HasSuffix(path, exampleManifestSuffix) {
			return err
		}
		for _, pod := range proxyPods(exampleObjects(t, path)) {
			for _, container := range pod.proxyContainers() {
				started++
				t.Run(path+"/"+container.Name, func(t *testing.T) { startContainer(t, container) })
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if started == 0 {
		t.Fatalf("no manifest under %s runs the image %q", examplesDir, exampleProxyRepository)
	}
}

// The egress gateway example serves TLS from one bundle, separates tenants by the token's
// partition, and names the Service the same way in the kubeconfig, the gateway rule and
// the token's audience.
func TestEgressGatewayExampleIsConsistent(t *testing.T) {
	objects, _, container := egressGatewayPod(t)
	cfg := startContainer(t, container)

	if cfg.tlsCertFile == "" || cfg.tlsCertFile != cfg.tlsKeyFile {
		t.Errorf("--tls-cert-file=%q and --tls-key-file=%q, want one bundle file in both", cfg.tlsCertFile, cfg.tlsKeyFile)
	}
	if cfg.authMode != authModeJWT || cfg.jwtPartitionClaim == "" || cfg.partitionByCaller != partitionByPartition {
		t.Errorf("--auth-mode=%q --jwt-partition-claim=%q --partition-by-caller=%q, want jwt callers partitioned by a claim",
			cfg.authMode, cfg.jwtPartitionClaim, cfg.partitionByCaller)
	}

	var host string
	var port int
	for _, object := range objects {
		if object.Kind == exampleKindService && len(object.Spec.Ports) == 1 {
			host = object.Metadata.Name + "." + object.Metadata.Namespace + ".svc"
			port = object.Spec.Ports[0].Port
		}
	}
	if host == "" {
		t.Fatalf("%s holds no Service with one port", egressGatewayManifest)
	}
	if want := fmt.Sprintf(":%d", port); !strings.HasSuffix(cfg.listenAddr, want) {
		t.Errorf("--listen-addr=%q, want the Service's port %s", cfg.listenAddr, want)
	}

	kubeconfigs := exampleObjects(t, egressGatewayConfig)
	if len(kubeconfigs) != 1 || len(kubeconfigs[0].Clusters) != 1 || len(kubeconfigs[0].Users) != 1 {
		t.Fatalf("%s: want one kubeconfig with one cluster and one user", egressGatewayConfig)
	}
	if got, want := kubeconfigs[0].Clusters[0].Cluster.Server, fmt.Sprintf("https://%s:%d", host, port); got != want {
		t.Errorf("kubeconfig server = %q, want %q", got, want)
	}
	if got := kubeconfigs[0].Users[0].User.Token; got != examplePlaceholder {
		t.Errorf("kubeconfig token = %q, want %q", got, examplePlaceholder)
	}

	rules := exampleObjects(t, egressGatewayRule)
	if len(rules) != 1 {
		t.Fatalf("%s holds %d documents, want 1", egressGatewayRule, len(rules))
	}
	rule := rules[0].EgressRule
	if rule.Host != host || rule.Port != port || rule.Scheme != "https" {
		t.Errorf("egress rule targets %s://%s:%d, want https://%s:%d", rule.Scheme, rule.Host, rule.Port, host, port)
	}
	if rule.Token.Issuer != cfg.jwtIssuer {
		t.Errorf("egress rule issuer = %q, want --jwt-issuer %q", rule.Token.Issuer, cfg.jwtIssuer)
	}
	if !slices.Contains(splitList(cfg.jwtAudiences), rule.Token.Audience) {
		t.Errorf("egress rule audience = %q, not in --jwt-audiences %q", rule.Token.Audience, cfg.jwtAudiences)
	}
	if rule.Token.Audience != host {
		t.Errorf("egress rule audience = %q, want the Service name %q", rule.Token.Audience, host)
	}
}

// The egress gateway manifest mounts every file its flags name, probes the binary's health
// path, and uses one port for the listener, the container, the probes, the Service's
// target and the NetworkPolicy.
func TestEgressGatewayManifestWiring(t *testing.T) {
	objects, pod, container := egressGatewayPod(t)
	cfg := startContainer(t, container)
	checkMountedFiles(t, container.Args, pod, container)

	_, rawPort, err := net.SplitHostPort(cfg.listenAddr)
	if err != nil {
		t.Fatalf("--listen-addr=%q: %v", cfg.listenAddr, err)
	}
	listenPort, err := strconv.Atoi(rawPort)
	if err != nil {
		t.Fatalf("--listen-addr=%q: %v", cfg.listenAddr, err)
	}
	if len(container.Ports) != 1 || container.Ports[0].ContainerPort != listenPort {
		t.Errorf("container ports = %+v, want the one port --listen-addr names, %d", container.Ports, listenPort)
	}

	for name, probe := range map[string]*exampleProbe{"readinessProbe": container.ReadinessProbe, "livenessProbe": container.LivenessProbe} {
		if probe == nil {
			t.Errorf("container %s has no %s", container.Name, name)
			continue
		}
		if probe.HTTPGet.Path != k8sapiproxy.HealthzPath || probe.HTTPGet.Scheme != exampleProbeScheme {
			t.Errorf("%s gets %s %s, want %s %s", name, probe.HTTPGet.Scheme, probe.HTTPGet.Path, exampleProbeScheme, k8sapiproxy.HealthzPath)
		}
		if got := container.port(probe.HTTPGet.Port); got != listenPort {
			t.Errorf("%s port resolves to %d, want %d", name, got, listenPort)
		}
	}

	services, policyPorts := 0, 0
	for _, object := range objects {
		switch object.Kind {
		case exampleKindService:
			for _, port := range object.Spec.Ports {
				services++
				if got := container.port(port.TargetPort); got != listenPort {
					t.Errorf("Service targetPort resolves to %d, want %d", got, listenPort)
				}
			}
		case exampleKindPolicy:
			for _, rule := range object.Spec.Ingress {
				for _, port := range rule.Ports {
					policyPorts++
					if got := container.port(port.Port); got != listenPort {
						t.Errorf("NetworkPolicy admits port %d, want %d", got, listenPort)
					}
				}
			}
		}
	}
	if services == 0 || policyPorts == 0 {
		t.Errorf("%s holds %d Service ports and %d NetworkPolicy ports, want at least one of each", egressGatewayManifest, services, policyPorts)
	}
}

// The three edits the egress gateway README describes to the manifest's args start the
// binary: a TLS Secret in place of the certificate bundle, a front proxy's identity headers
// in place of the injected token, and GKE routing in place of the static upstream, which
// reads the host cluster's token and CA from the mounted volume.
func TestEgressGatewayReadmeVariantsStartTheBinary(t *testing.T) {
	_, pod, container := egressGatewayPod(t)
	blocks := flagBlocks(t, egressGatewayReadme)
	if len(blocks) != egressGatewayReadmeBlocks {
		t.Fatalf("%s holds %d fenced blocks with flags, want %d", egressGatewayReadme, len(blocks), egressGatewayReadmeBlocks)
	}

	t.Run("TLS Secret", func(t *testing.T) {
		block := blocks[0]
		if got, want := flagNames(block.flags), []string{tlsCertFileFlag, tlsKeyFileFlag}; !slices.Equal(got, want) {
			t.Fatalf("the first block sets %q, want %q", got, want)
		}
		clearHostEnv(t)
		cfg := startFromArgs(t, replaceFlags(container.Args, nil, block.flags))
		if filepath.Base(cfg.tlsCertFile) != exampleSecretCertFile || filepath.Base(cfg.tlsKeyFile) != exampleSecretKeyFile {
			t.Errorf("--tls-cert-file=%q --tls-key-file=%q, want a TLS Secret's %s and %s", cfg.tlsCertFile, cfg.tlsKeyFile, exampleSecretCertFile, exampleSecretKeyFile)
		}

		// The snippet is a pod's containers and volumes: its own mount and volume hold
		// the two files, and the mount is where the manifest mounts the volume it replaces.
		var snippet examplePodSpec
		if err := utilyaml.NewYAMLOrJSONDecoder(strings.NewReader(block.text), exampleDecodeBuffer).Decode(&snippet); err != nil {
			t.Fatalf("the first block is not YAML: %v", err)
		}
		if len(snippet.Containers) != 1 || len(snippet.Volumes) != 1 {
			t.Fatalf("the first block holds %d containers and %d volumes, want 1 of each", len(snippet.Containers), len(snippet.Volumes))
		}
		checkMountedFiles(t, snippet.Containers[0].Args, snippet, snippet.Containers[0])
		volumeName, _, _ := snippet.Containers[0].mountedFile(cfg.tlsCertFile)
		replaced, _, ok := container.mountedFile(cfg.tlsCertFile)
		if !ok || replaced != volumeName {
			t.Errorf("the snippet mounts volume %q for %s, where the manifest mounts %q", volumeName, cfg.tlsCertFile, replaced)
		}
	})

	t.Run("front proxy headers", func(t *testing.T) {
		block := blocks[1]
		want := []string{authModeFlag, headerAllowedNamesFlag, headerAllowedPartitionsFlag, headerClientCAFileFlag, headerNameSourceFlag, headerPartitionHeadersFlag, headerUsernameHeadersFlag}
		if got := flagNames(block.flags); !slices.Equal(got, want) {
			t.Fatalf("the second block sets %q, want %q", got, want)
		}
		// The README's edit: every --jwt-* flag goes, and --auth-mode is replaced.
		var jwtFlags []string
		for _, name := range flagNames(container.Args) {
			if strings.HasPrefix(name, exampleJWTFlagPrefix) {
				jwtFlags = append(jwtFlags, name)
			}
		}
		if len(jwtFlags) == 0 {
			t.Fatal("the manifest sets no --jwt-* flag for the variant to delete")
		}
		clearHostEnv(t)
		cfg := startFromArgs(t, replaceFlags(container.Args, jwtFlags, block.flags))
		if cfg.authMode != authModeRequestHeader || cfg.partitionByCaller != partitionByPartition || cfg.clientCert.headerPartitionHeaders == "" {
			t.Errorf("--auth-mode=%q --partition-by-caller=%q --requestheader-partition-headers=%q, want front proxy callers partitioned by a header",
				cfg.authMode, cfg.partitionByCaller, cfg.clientCert.headerPartitionHeaders)
		}
		// Left out, the flags take default paths this pod does not mount.
		if cfg.tlsCertFile == "" || cfg.tlsKeyFile == "" || cfg.tlsCertFile == defaultTLSCertFile || cfg.tlsKeyFile == defaultTLSKeyFile {
			t.Errorf("--tls-cert-file=%q --tls-key-file=%q, want the serving files a client certificate needs, named by the manifest", cfg.tlsCertFile, cfg.tlsKeyFile)
		}
	})

	t.Run("GKE routing", func(t *testing.T) {
		block := blocks[2]
		if got, want := flagNames(block.flags), []string{kubeCAFileFlag, kubeTokenFileFlag}; !slices.Equal(got, want) {
			t.Fatalf("the third block sets %q, want %q", got, want)
		}
		clearHostEnv(t)
		args := replaceFlags(container.Args, []string{upstreamURLFlag, upstreamTokenFileFlag, upstreamCAFileFlag}, block.flags)
		cfg := startFromArgs(t, args)
		checkMountedFiles(t, args, pod, container)
		if cfg.upstreamURL != "" || cfg.upstreamTokenFile != "" || cfg.upstreamCAFile != "" {
			t.Errorf("the GKE variant keeps a static upstream: --upstream-url=%q", cfg.upstreamURL)
		}
		if cfg.kubeTokenFile == defaultKubeTokenFile || cfg.kubeCAFile == defaultKubeCAFile {
			t.Errorf("--kube-token-file=%q --kube-ca-file=%q, want the mounted files, not the defaults this pod does not mount", cfg.kubeTokenFile, cfg.kubeCAFile)
		}
	})
}

// The module README's two commands, one for each certificate mode, start the binary.
func TestModuleReadmeCertificateModeCommandsStartTheBinary(t *testing.T) {
	blocks := flagBlocks(t, moduleReadme)
	if len(blocks) != moduleReadmeBlocks {
		t.Fatalf("%s holds %d fenced blocks with flags, want %d", moduleReadme, len(blocks), moduleReadmeBlocks)
	}
	for i, wantMode := range []string{authModeMTLS, authModeRequestHeader} {
		t.Run(wantMode, func(t *testing.T) {
			clearHostEnv(t)
			cfg := startFromArgs(t, blocks[i].flags)
			if cfg.authMode != wantMode {
				t.Errorf("--auth-mode=%q, want %q", cfg.authMode, wantMode)
			}
			// A command that leaves the serving files out gets the flags' default paths,
			// which exist only in the kube-agents deployment.
			if cfg.tlsCertFile == "" || cfg.tlsKeyFile == "" || cfg.tlsCertFile == defaultTLSCertFile || cfg.tlsKeyFile == defaultTLSKeyFile || cfg.upstreamURL == "" {
				t.Errorf("--tls-cert-file=%q --tls-key-file=%q --upstream-url=%q, want all three given by the command", cfg.tlsCertFile, cfg.tlsKeyFile, cfg.upstreamURL)
			}
		})
	}
}

// The local bridge command, and its sidecar form, start an unauthenticated plain-HTTP
// loopback proxy to a static upstream, with streaming refused and the cache off.
func TestLocalBridgeExampleStartsTheBinary(t *testing.T) {
	blocks := flagBlocks(t, localBridgeReadme)
	if len(blocks) == 0 {
		t.Fatalf("%s holds no fenced block with flags", localBridgeReadme)
	}
	if len(blocks) != localBridgeReadmeBlocks {
		t.Errorf("%s holds %d fenced blocks with flags, want the command and the sidecar (%d)", localBridgeReadme, len(blocks), localBridgeReadmeBlocks)
	}
	for _, block := range blocks {
		args := block.flags
		t.Run(strings.Join(args, " "), func(t *testing.T) {
			clearHostEnv(t)
			cfg := startFromArgs(t, args)
			if cfg.authMode != authModeNone || !strings.HasPrefix(cfg.listenAddr, exampleLoopbackPrefix) {
				t.Errorf("--auth-mode=%q --listen-addr=%q, want none on loopback", cfg.authMode, cfg.listenAddr)
			}
			if cfg.tlsCertFile != "" || cfg.tlsKeyFile != "" {
				t.Errorf("--tls-cert-file=%q --tls-key-file=%q, want both empty", cfg.tlsCertFile, cfg.tlsKeyFile)
			}
			if !strings.HasPrefix(cfg.upstreamURL, "http://"+exampleLoopbackPrefix) {
				t.Errorf("--upstream-url=%q, want a loopback http URL", cfg.upstreamURL)
			}
			if !cfg.denyStreaming || cfg.cacheMaxBytes != 0 {
				t.Errorf("--deny-streaming=%v --cache-max-bytes=%d, want streaming refused and no cache", cfg.denyStreaming, cfg.cacheMaxBytes)
			}
		})
	}
}

// A variable the binary reads only from the environment is part of what an example starts
// with: one the example's flags do not allow stops it, as it stops the binary.
func TestExampleStartReadsEnvOnlyVariables(t *testing.T) {
	_, _, container := egressGatewayPod(t)
	container.Env = append(slices.Clone(container.Env), struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	}{Name: sessionAudienceEnv, Value: testReviewAudience})
	_, err := buildContainer(t, container)
	if err == nil || !strings.Contains(err.Error(), sessionAudienceEnv) {
		t.Fatalf("err = %v, want the binary's refusal of %s without token-review", err, sessionAudienceEnv)
	}
}

// proxyEnvVars is every KUBE_PROXY_ and CREDENTIAL_PROXY_ variable named by a string
// literal in the binary's source, and nothing else.
func TestProxyEnvVarsListsEveryVariableTheSourceNames(t *testing.T) {
	sources, err := filepath.Glob("*" + exampleGoSuffix)
	if err != nil {
		t.Fatal(err)
	}
	var named []string
	for _, source := range sources {
		if strings.HasSuffix(source, exampleGoTestSuffix) {
			continue
		}
		data, err := os.ReadFile(source)
		if err != nil {
			t.Fatal(err)
		}
		for _, match := range exampleEnvLiteral.FindAllSubmatch(data, -1) {
			named = append(named, string(match[1]))
		}
	}
	slices.Sort(named)
	named = slices.Compact(named)
	listed := slices.Clone(proxyEnvVars)
	slices.Sort(listed)
	if !slices.Equal(named, listed) {
		t.Errorf("the source names %q\nproxyEnvVars lists %q", named, listed)
	}
}
