package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"strconv"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	kubeProxyPort             int32 = 8443
	kubeProxyMetricsPort      int32 = 8444
	kubeProxyMetricsPortName        = "kube-metrics"            // #nosec G101 -- Container port name, not a credential
	kubeProxyMetricsPortEnv         = "KUBE_PROXY_METRICS_PORT" // #nosec G101 -- Environment variable name, not a credential
	kubeProxyImageEnvVar            = "KUBE_PROXY_IMAGE"
	kubeProxyRepositoryName         = "kube-proxy"
	kubeProxyTLSSecretSuffix        = "-kube-proxy-tls" // #nosec G101 -- Secret name suffix, not a credential
	kubeProxyTLSVolumeName          = "kube-proxy-tls"  // #nosec G101 -- Volume name, not a credential
	kubeProxyTLSMountPath           = "/var/run/secrets/kubeagents/kube-proxy-tls"
	kubeProxyCAVolumeName           = "kube-proxy-ca"
	kubeProxyCAMountPath            = "/var/run/secrets/kubeagents/kube-proxy-ca"
	kubeProxyCACertFile             = kubeProxyCAMountPath + "/ca.crt"
	kubeProxyCPURequest             = "100m"
	kubeProxyMemoryRequest          = "256Mi"
	kubeProxyCPULimit               = "1"
	kubeProxyMemoryLimit            = "512Mi"
	kubeProxyTLSHashAnnotation      = "kubeagents.x-k8s.io/tls-cert-hash"
	kubeProxyTLSClockSkew           = 5 * time.Minute
	kubeProxyTLSValidity            = 10 * 365 * 24 * time.Hour
	kubeProxySecretDefaultMode      = int32(0440)
	kubeAPITokenExpirationSeconds   = int64(3600)
	kubeAPITokenVolumeName          = "event-watcher-ksa-token"
	kubeRootCAConfigMapName         = "kube-root-ca.crt"
	kubeProxyProbeInitialDelay      = int32(2)
	kubeProxyProbePeriod            = int32(10)
	kubeProxyProbeTimeout           = int32(5)
	kubeProxyProbeFailureThreshold  = int32(3)
)

func kubeProxyName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-kube-proxy"
}

func kubeProxyTLSSecretName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + kubeProxyTLSSecretSuffix
}

func kubeProxySelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		"app":                           kubeProxyName(agent),
		"kubeagents.x-k8s.io/component": "kube-proxy",
	}
}

func kubeProxyURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("https://%s.%s.svc.cluster.local:%d",
		kubeProxyName(agent), agent.Namespace, kubeProxyPort)
}

func resolveKubeProxyImage() string {
	if override := os.Getenv(kubeProxyImageEnvVar); override != "" {
		return override
	}
	if operatorImage := os.Getenv(operatorImageEnvVar); operatorImage != "" {
		return deriveImageFromOperator(operatorImage, kubeProxyRepositoryName)
	}
	if agentImage := os.Getenv(platformAgentImageEnvVar); agentImage != "" {
		return deriveImageFromOperator(agentImage, kubeProxyRepositoryName)
	}
	return "ghcr.io/gke-labs/kube-agents/" + kubeProxyRepositoryName + ":" + DefaultPlatformAgentVersion
}

func buildKubeProxyService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace},
		Spec: corev1.ServiceSpec{
			Selector: kubeProxySelector(agent),
			Ports: []corev1.ServicePort{
				{
					Name:       "https",
					Port:       kubeProxyPort,
					TargetPort: intstr.FromString("https"),
					Protocol:   corev1.ProtocolTCP,
				},
				{
					Name:       kubeProxyMetricsPortName,
					Port:       kubeProxyMetricsPort,
					TargetPort: intstr.FromString(kubeProxyMetricsPortName),
					Protocol:   corev1.ProtocolTCP,
				},
			},
		},
	}
	withCommonLabels(svc, agent)
	return svc
}

func kubeProxyOTelEnvVars(agent *agentv1alpha1.PlatformAgent, endpoint string, disabled bool) []corev1.EnvVar {
	envs := otelTelemetryEnvVars("platform", agent.Name, agent.Namespace, endpoint, disabled)
	for i := range envs {
		if envs[i].Name == "OTEL_SERVICE_NAME" {
			envs[i].Value = kubeProxyName(agent)
		}
	}
	return envs
}

func buildKubeProxyDeployment(agent *agentv1alpha1.PlatformAgent, otlpEndpoint string, otlpDisabled bool, tlsCertHash string) *appsv1.Deployment {
	saName := agentServiceAccountName(agent)
	podLabels := commonLabels(agent)
	for k, v := range kubeProxySelector(agent) {
		podLabels[k] = v
	}

	var podAnnotations map[string]string
	if tlsCertHash != "" {
		podAnnotations = map[string]string{
			kubeProxyTLSHashAnnotation: tlsCertHash,
		}
	}

	var affinity *corev1.Affinity
	var nodeSelector map[string]string
	var tolerations []corev1.Toleration
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		affinity = agent.Spec.Deployment.Availability.Affinity
		nodeSelector = agent.Spec.Deployment.Availability.NodeSelector
		tolerations = agent.Spec.Deployment.Availability.Tolerations
	}

	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}

	defaultCluster := inClusterContextName
	if h := agent.Spec.Harness; h != nil && h.ProjectID != "" && h.Location != "" && h.ClusterName != "" && h.Location != kindLocation {
		defaultCluster = fmt.Sprintf("gke_%s_%s_%s", h.ProjectID, h.Location, h.ClusterName)
	}

	envVars := []corev1.EnvVar{
		{Name: "KUBE_PROXY_LISTEN_ADDR", Value: fmt.Sprintf(":%d", kubeProxyPort)},
		{Name: kubeProxyMetricsPortEnv, Value: strconv.Itoa(int(kubeProxyMetricsPort))},
		{Name: "KUBE_PROXY_TLS_CERT_FILE", Value: kubeProxyTLSMountPath + "/tls.crt"},
		{Name: "KUBE_PROXY_TLS_KEY_FILE", Value: kubeProxyTLSMountPath + "/tls.key"},
		{Name: "KUBE_PROXY_MODE", Value: "read-only-dry-run"},
		{Name: "KUBE_PROXY_REDACT_SECRETS", Value: "true"},
		{Name: "KUBE_PROXY_DEFAULT_CLUSTER", Value: defaultCluster},
		{Name: "CREDENTIAL_PROXY_AUDIENCE", Value: credentialProxyAudience},
		{Name: "CREDENTIAL_PROXY_ALLOWED_CALLERS", Value: allowedBrokerCallers(agent)},
		{Name: "CREDENTIAL_PROXY_KUBE_CA_FILE", Value: kubeAPIAccessMountPath + "/ca.crt"},
		{Name: "CREDENTIAL_PROXY_KUBE_TOKEN_FILE", Value: kubeAPIAccessMountPath + "/token"},
	}
	envVars = append(envVars, kubeProxyOTelEnvVars(agent, otlpEndpoint, otlpDisabled)...)

	volumeMounts := []corev1.VolumeMount{
		{Name: kubeProxyTLSVolumeName, MountPath: kubeProxyTLSMountPath, ReadOnly: true},
		{Name: kubeAPITokenVolumeName, MountPath: kubeAPIAccessMountPath, ReadOnly: true},
	}

	volumes := []corev1.Volume{
		{
			Name: kubeProxyTLSVolumeName,
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName:  kubeProxyTLSSecretName(agent),
					DefaultMode: ptr.To(kubeProxySecretDefaultMode),
				},
			},
		},
		{
			Name: kubeAPITokenVolumeName,
			VolumeSource: corev1.VolumeSource{
				Projected: &corev1.ProjectedVolumeSource{
					DefaultMode: ptr.To(kubeProxySecretDefaultMode),
					Sources: []corev1.VolumeProjection{
						{
							ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
								ExpirationSeconds: ptr.To(kubeAPITokenExpirationSeconds),
								Path:              "token",
							},
						},
						{
							ConfigMap: &corev1.ConfigMapProjection{
								LocalObjectReference: corev1.LocalObjectReference{Name: kubeRootCAConfigMapName},
								Items:                []corev1.KeyToPath{{Key: "ca.crt", Path: "ca.crt"}},
							},
						},
					},
				},
			},
		},
	}

	if credentialProxyFederation(agent) != nil {
		envVars = append(envVars, buildCredentialProxyFederationEnv(agent)...)
		volumeMounts = append(volumeMounts,
			corev1.VolumeMount{Name: credentialProxyWIFTokenVolume, MountPath: credentialProxyWIFTokenPath, ReadOnly: true},
		)
		volumes = append(volumes, buildCredentialProxyFederationVolume(agent)...)
	}

	container := corev1.Container{
		Name:            "kube-proxy",
		Image:           resolveKubeProxyImage(),
		ImagePullPolicy: pullPolicy,
		Env:             envVars,
		Ports: []corev1.ContainerPort{
			{
				Name:          "https",
				ContainerPort: kubeProxyPort,
				Protocol:      corev1.ProtocolTCP,
			},
			{
				Name:          kubeProxyMetricsPortName,
				ContainerPort: kubeProxyMetricsPort,
				Protocol:      corev1.ProtocolTCP,
			},
		},
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				HTTPGet: &corev1.HTTPGetAction{
					Path:   "/_kube_proxy/healthz",
					Port:   intstr.FromString("https"),
					Scheme: corev1.URISchemeHTTPS,
				},
			},
			InitialDelaySeconds: kubeProxyProbeInitialDelay,
			PeriodSeconds:       kubeProxyProbePeriod,
			TimeoutSeconds:      kubeProxyProbeTimeout,
			FailureThreshold:    kubeProxyProbeFailureThreshold,
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse(kubeProxyCPURequest),
				corev1.ResourceMemory: resource.MustParse(kubeProxyMemoryRequest),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse(kubeProxyCPULimit),
				corev1.ResourceMemory: resource.MustParse(kubeProxyMemoryLimit),
			},
		},
		VolumeMounts: volumeMounts,
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}

	dep := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: kubeProxySelector(agent)},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: podAnnotations,
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:           saName,
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        ptr.To(agentFSGroup),
						RunAsUser:      ptr.To(sandboxUID),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					ImagePullSecrets: resolveImagePullSecrets(agent.Spec.Deployment),
					Affinity:         affinity,
					NodeSelector:     nodeSelector,
					Tolerations:      tolerations,
					Containers:       []corev1.Container{container},
					Volumes:          volumes,
				},
			},
		},
	}
	withCommonLabels(dep, agent)
	dep.Labels["app"] = kubeProxyName(agent)
	return dep
}

func buildKubeProxyNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	callers := []networkingv1.NetworkPolicyPeer{
		{PodSelector: &metav1.LabelSelector{MatchLabels: shellSandboxSelector(agent)}},
	}
	np := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: kubeProxySelector(agent)},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{
				{
					From: callers,
					Ports: []networkingv1.NetworkPolicyPort{{
						Protocol: &tcp,
						Port:     ptr.To(intstr.FromInt32(kubeProxyPort)),
					}},
				},
				{
					From: []networkingv1.NetworkPolicyPeer{{
						NamespaceSelector: &metav1.LabelSelector{
							MatchLabels: map[string]string{labelMetadataName: gmpNamespace},
						},
					}},
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(kubeProxyMetricsPort)},
				},
			},
		},
	}
	withCommonLabels(np, agent)
	return np
}

func generateKubeProxyTLSBundle(serviceName, namespace string) (caPEM, certPEM, keyPEM []byte, err error) {
	now := time.Now().Add(-kubeProxyTLSClockSkew)
	notAfter := now.Add(kubeProxyTLSValidity)

	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}
	caTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: serviceName + "-ca"},
		NotBefore:             now,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, err
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		return nil, nil, nil, err
	}

	srvKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}
	dnsNames := []string{
		serviceName,
		fmt.Sprintf("%s.%s", serviceName, namespace),
		fmt.Sprintf("%s.%s.svc", serviceName, namespace),
		fmt.Sprintf("%s.%s.svc.cluster.local", serviceName, namespace),
	}
	srvTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: serviceName},
		NotBefore:    now,
		NotAfter:     notAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     dnsNames,
	}
	srvDER, err := x509.CreateCertificate(rand.Reader, srvTemplate, caCert, &srvKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, err
	}
	srvKeyDER, err := x509.MarshalECPrivateKey(srvKey)
	if err != nil {
		return nil, nil, nil, err
	}

	caPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	certPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srvDER})
	keyPEM = pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: srvKeyDER})
	return caPEM, certPEM, keyPEM, nil
}

func kubeProxyTLSSecretHash(data map[string][]byte) string {
	sum := sha256.New()
	sum.Write(data["ca.crt"])
	sum.Write([]byte{0})
	sum.Write(data["tls.crt"])
	sum.Write([]byte{0})
	sum.Write(data["tls.key"])
	return hex.EncodeToString(sum.Sum(nil))
}

func (r *PlatformAgentReconciler) ensureKubeProxyTLSSecret(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	secretName := kubeProxyTLSSecretName(agent)
	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, client.ObjectKey{Name: secretName, Namespace: agent.Namespace}, existing)
	if err == nil {
		if !metav1.IsControlledBy(existing, agent) {
			return "", fmt.Errorf("kube-proxy TLS Secret %s/%s already exists and is not owned by PlatformAgent %s", agent.Namespace, secretName, agent.Name)
		}
		if len(existing.Data["ca.crt"]) > 0 && len(existing.Data["tls.crt"]) > 0 && len(existing.Data["tls.key"]) > 0 {
			return kubeProxyTLSSecretHash(existing.Data), nil
		}
	} else if !errors.IsNotFound(err) {
		return "", fmt.Errorf("failed to get kube-proxy TLS Secret %s/%s: %w", agent.Namespace, secretName, err)
	}

	caPEM, certPEM, keyPEM, genErr := generateKubeProxyTLSBundle(kubeProxyName(agent), agent.Namespace)
	if genErr != nil {
		return "", fmt.Errorf("failed to generate kube-proxy TLS bundle: %w", genErr)
	}

	bundleData := map[string][]byte{
		"ca.crt":  caPEM,
		"tls.crt": certPEM,
		"tls.key": keyPEM,
	}
	bundleHash := kubeProxyTLSSecretHash(bundleData)

	if errors.IsNotFound(err) {
		secret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      secretName,
				Namespace: agent.Namespace,
				Labels:    commonLabels(agent),
			},
			Type: corev1.SecretTypeOpaque,
			Data: bundleData,
		}
		if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
			return "", err
		}
		return bundleHash, r.Create(ctx, secret)
	}

	existing.Data = bundleData
	return bundleHash, r.Update(ctx, existing)
}

func (r *PlatformAgentReconciler) reconcileKubeProxy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, otlpEndpoint string, otlpDisabled bool) error {
	tlsHash, err := r.ensureKubeProxyTLSSecret(ctx, agent)
	if err != nil {
		return err
	}

	objs := []client.Object{
		buildKubeProxyService(agent),
		buildKubeProxyDeployment(agent, otlpEndpoint, otlpDisabled, tlsHash),
		buildKubeProxyNetworkPolicy(agent),
	}
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on kube-proxy %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply kube-proxy %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}
