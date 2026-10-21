/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"slices"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TLS on the bus. The operator mints a CA and a serving certificate for the
// NATS Service and keeps them in two Secrets; the NATS pod mounts the serving
// pair, and every client trusts the CA through a ConfigMap that carries only
// its public half. Nothing here depends on cert-manager, which the chart does
// not install: an install that has it, or that wants to bring its own
// certificate, can later fill the same two Secrets without any client changing.
//
// The CA is long-lived and the serving certificate is not, so a renewal never
// changes what clients trust. A renewal does change what the server must
// present, and nats-server reads its certificate only at boot or on a reload
// signal. Until a reloader sidecar exists, the serving certificate's
// fingerprint rides a2aConfigRolloutHash, so a renewal restarts the bus once,
// the same window any other config change costs.
const (
	// The keys in the serving Secret, which the NATS pod mounts as a
	// directory. The Kubernetes TLS Secret keys, so a cert-manager
	// Certificate or a hand-made `kubernetes.io/tls` Secret fits as is.
	a2aNATSTLSCertKey = corev1.TLSCertKey
	a2aNATSTLSKeyKey  = corev1.TLSPrivateKeyKey
	a2aNATSTLSCAKey   = "ca.crt"

	// a2aNATSCACertKey and a2aNATSCAKeyKey are the CA Secret's two keys.
	a2aNATSCACertKey = "ca.crt"
	a2aNATSCAKeyKey  = "ca.key"

	// a2aNATSTLSMountDir is where the NATS container mounts the serving
	// Secret; nats.conf names the files under it. A directory mount, never a
	// subPath, so the kubelet updates the files in place.
	a2aNATSTLSMountDir = "/etc/nats-tls"
	// a2aNATSTLSVolume is the NATS pod's volume for the serving Secret.
	a2aNATSTLSVolume = "tls"

	// a2aNATSCABundleSuffix names the ConfigMap clients mount the CA from:
	// the public half alone, so a client never mounts a Secret to get it.
	a2aNATSCABundleSuffix = "-ca-bundle"
	// a2aNATSCABundleMountDir is where every client mounts the bundle, and
	// a2aNATSCAFileEnv names the file for it.
	a2aNATSCABundleMountDir = "/etc/a2a-nats-ca"
	a2aNATSCABundleVolume   = "a2a-nats-ca"
	a2aNATSCAFileEnv        = "NATS_CA_FILE"

	// The CA lives ten years and the serving certificate one. The reconcile
	// reissues either once fewer than a2aNATSTLSRenewBefore remain, so a
	// serving certificate is replaced about eleven months in.
	a2aNATSCALifetime     = 10 * 365 * 24 * time.Hour
	a2aNATSTLSLifetime    = 365 * 24 * time.Hour
	a2aNATSTLSRenewBefore = 30 * 24 * time.Hour
	// a2aNATSTLSBackdate covers clock skew between the operator and the
	// pods that verify the certificate.
	a2aNATSTLSBackdate = 5 * time.Minute

	a2aNATSCACommonName = "kube-agents a2a bus CA"
	// a2aNATSSerialBits bounds a certificate serial number.
	a2aNATSSerialBits = 128
	// a2aNATSFingerprintLength is how much of a certificate's SHA-256 the
	// rollout hash and the session stamp carry.
	a2aNATSFingerprintLength = 16
	// a2aNATSClusterDomain is the cluster DNS suffix of the last SAN. The
	// shorter names are what every client actually dials.
	a2aNATSClusterDomain = "cluster.local"
)

// a2aNATSTLSNow is the clock certificates are issued and judged against.
var a2aNATSTLSNow = time.Now

// a2aNATSTLS is what the rest of the render needs from the certificates: the
// CA's public PEM for the bundle, and the fingerprints the rollout hash and the
// session stamp key on.
type a2aNATSTLS struct {
	CAPEM              []byte
	CAFingerprint      string
	ServingFingerprint string
}

// a2aNATSCABundleName is the ConfigMap clients mount the CA from.
func a2aNATSCABundleName(agent *agentv1alpha1.PlatformAgent) string {
	return a2aNATSName(agent) + a2aNATSCABundleSuffix
}

// a2aNATSCAFile is the path a client reads the CA from.
func a2aNATSCAFile() string { return a2aNATSCABundleMountDir + "/" + a2aNATSCACertKey }

// a2aNATSDNSNames are the serving certificate's SANs: the NATS Service's
// in-cluster names and nothing else. No localhost and no external host,
// because nothing reaches the bus from outside the cluster; a port-forwarded
// check sets the TLS server name to one of these instead.
func a2aNATSDNSNames(agent *agentv1alpha1.PlatformAgent) []string {
	svc := a2aNATSName(agent)
	ns := agent.Namespace
	return []string{svc, svc + "." + ns, svc + "." + ns + ".svc", svc + "." + ns + ".svc." + a2aNATSClusterDomain}
}

// ensureA2ANATSTLS keeps the CA, the serving certificate and the bundle
// current, issuing whichever is missing, unreadable or inside its renewal
// window, and returns what the render needs from them.
//
// A new CA always means a new serving certificate, since the old one no longer
// verifies. The CA Secret is replaced rather than repaired: a key without its
// certificate, or the reverse, is not something to complete by halves.
func (r *PlatformAgentReconciler) ensureA2ANATSTLS(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*a2aNATSTLS, error) {
	now := a2aNATSTLSNow()

	caCert, caKey, caPEM, err := r.ensureA2ANATSCA(ctx, agent, now)
	if err != nil {
		return nil, err
	}
	servingDER, err := r.ensureA2ANATSServing(ctx, agent, caCert, caKey, caPEM, now)
	if err != nil {
		return nil, err
	}

	bundle := &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aNATSCABundleName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "nats-ca-bundle"),
		},
		Data: map[string]string{a2aNATSCACertKey: string(caPEM)},
	}
	if err := ctrl.SetControllerReference(agent, bundle, r.Scheme); err != nil {
		return nil, err
	}
	if err := r.applyManaged(ctx, agent, bundle); err != nil {
		return nil, fmt.Errorf("failed to apply the A2A NATS CA bundle: %w", err)
	}

	return &a2aNATSTLS{
		CAPEM:              caPEM,
		CAFingerprint:      a2aCertFingerprint(caCert.Raw),
		ServingFingerprint: a2aCertFingerprint(servingDER),
	}, nil
}

// ensureA2ANATSCA returns the current CA, issuing a new one when the Secret is
// missing, unreadable or near expiry.
func (r *PlatformAgentReconciler) ensureA2ANATSCA(ctx context.Context, agent *agentv1alpha1.PlatformAgent, now time.Time) (*x509.Certificate, *ecdsa.PrivateKey, []byte, error) {
	name := types.NamespacedName{Name: agentv1alpha1.A2ANATSCASecretName(agent.Name), Namespace: agent.Namespace}
	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, name, existing)
	if err != nil && !apierrors.IsNotFound(err) {
		return nil, nil, nil, err
	}
	if err == nil {
		if cert, key, ok := readA2ANATSCA(existing, now); ok {
			return cert, key, existing.Data[a2aNATSCACertKey], nil
		}
	}

	cert, key, certPEM, keyPEM, gerr := generateA2ANATSCA(now)
	if gerr != nil {
		return nil, nil, nil, gerr
	}
	data := map[string][]byte{a2aNATSCACertKey: certPEM, a2aNATSCAKeyKey: keyPEM}
	if err == nil {
		existing.Data = data
		if err := r.Update(ctx, existing); err != nil {
			return nil, nil, nil, err
		}
		return cert, key, certPEM, nil
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: name.Name, Namespace: name.Namespace, Labels: a2aLabels(agent, "nats-ca")},
		Data:       data,
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return nil, nil, nil, err
	}
	if err := r.Create(ctx, secret); err != nil {
		return nil, nil, nil, err
	}
	return cert, key, certPEM, nil
}

// ensureA2ANATSServing returns the serving certificate's DER, issuing a new one
// when the Secret is missing or unreadable, no longer verifies against the CA,
// names other hosts, carries a different CA, or is inside its renewal window.
func (r *PlatformAgentReconciler) ensureA2ANATSServing(ctx context.Context, agent *agentv1alpha1.PlatformAgent, caCert *x509.Certificate, caKey *ecdsa.PrivateKey, caPEM []byte, now time.Time) ([]byte, error) {
	name := types.NamespacedName{Name: agentv1alpha1.A2ANATSTLSSecretName(agent.Name), Namespace: agent.Namespace}
	dnsNames := a2aNATSDNSNames(agent)
	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, name, existing)
	if err != nil && !apierrors.IsNotFound(err) {
		return nil, err
	}
	if err == nil {
		if der, ok := readA2ANATSServing(existing, caCert, caPEM, dnsNames, now); ok {
			return der, nil
		}
	}

	der, certPEM, keyPEM, gerr := generateA2ANATSServing(caCert, caKey, dnsNames, now)
	if gerr != nil {
		return nil, gerr
	}
	data := map[string][]byte{a2aNATSTLSCertKey: certPEM, a2aNATSTLSKeyKey: keyPEM, a2aNATSTLSCAKey: caPEM}
	if err == nil {
		existing.Data = data
		if err := r.Update(ctx, existing); err != nil {
			return nil, err
		}
		return der, nil
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: name.Name, Namespace: name.Namespace, Labels: a2aLabels(agent, "nats-tls")},
		Type:       corev1.SecretTypeTLS,
		Data:       data,
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return nil, err
	}
	if err := r.Create(ctx, secret); err != nil {
		return nil, err
	}
	return der, nil
}

// readA2ANATSCA parses the CA Secret. It reports false for anything not fit to
// sign with: unparseable, not a CA, a key that is not the certificate's, or
// fewer than a2aNATSTLSRenewBefore left.
func readA2ANATSCA(secret *corev1.Secret, now time.Time) (*x509.Certificate, *ecdsa.PrivateKey, bool) {
	cert, err := parseA2ACert(secret.Data[a2aNATSCACertKey])
	if err != nil || !cert.IsCA || now.Add(a2aNATSTLSRenewBefore).After(cert.NotAfter) {
		return nil, nil, false
	}
	key, err := parseA2AKey(secret.Data[a2aNATSCAKeyKey])
	if err != nil || !key.PublicKey.Equal(cert.PublicKey) {
		return nil, nil, false
	}
	return cert, key, true
}

// readA2ANATSServing parses the serving Secret and returns its DER when it is
// still the certificate this render would issue in substance: signed by caCert,
// naming exactly dnsNames, paired with its key, carrying the current CA, and
// outside its renewal window.
func readA2ANATSServing(secret *corev1.Secret, caCert *x509.Certificate, caPEM []byte, dnsNames []string, now time.Time) ([]byte, bool) {
	if !bytes.Equal(secret.Data[a2aNATSTLSCAKey], caPEM) {
		return nil, false
	}
	cert, err := parseA2ACert(secret.Data[a2aNATSTLSCertKey])
	if err != nil || now.Add(a2aNATSTLSRenewBefore).After(cert.NotAfter) || cert.CheckSignatureFrom(caCert) != nil {
		return nil, false
	}
	if !slices.Equal(slices.Sorted(slices.Values(cert.DNSNames)), slices.Sorted(slices.Values(dnsNames))) {
		return nil, false
	}
	key, err := parseA2AKey(secret.Data[a2aNATSTLSKeyKey])
	if err != nil || !key.PublicKey.Equal(cert.PublicKey) {
		return nil, false
	}
	return cert.Raw, true
}

func generateA2ANATSCA(now time.Time) (*x509.Certificate, *ecdsa.PrivateKey, []byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	serial, err := a2aSerial()
	if err != nil {
		return nil, nil, nil, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: a2aNATSCACommonName},
		NotBefore:             now.Add(-a2aNATSTLSBackdate),
		NotAfter:              now.Add(a2aNATSCALifetime),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
		MaxPathLenZero:        true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	keyPEM, err := a2aKeyPEM(key)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	return cert, key, a2aCertPEM(der), keyPEM, nil
}

func generateA2ANATSServing(caCert *x509.Certificate, caKey *ecdsa.PrivateKey, dnsNames []string, now time.Time) ([]byte, []byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}
	serial, err := a2aSerial()
	if err != nil {
		return nil, nil, nil, err
	}
	notAfter := now.Add(a2aNATSTLSLifetime)
	if notAfter.After(caCert.NotAfter) {
		notAfter = caCert.NotAfter
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: dnsNames[0]},
		DNSNames:     dnsNames,
		NotBefore:    now.Add(-a2aNATSTLSBackdate),
		NotAfter:     notAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, err
	}
	keyPEM, err := a2aKeyPEM(key)
	if err != nil {
		return nil, nil, nil, err
	}
	return der, a2aCertPEM(der), keyPEM, nil
}

func a2aSerial() (*big.Int, error) {
	return rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), a2aNATSSerialBits))
}

func a2aCertPEM(der []byte) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func a2aKeyPEM(key *ecdsa.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

var errA2ANoPEM = errors.New("no PEM block")

func parseA2ACert(data []byte) (*x509.Certificate, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errA2ANoPEM
	}
	return x509.ParseCertificate(block.Bytes)
}

func parseA2AKey(data []byte) (*ecdsa.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errA2ANoPEM
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	key, ok := parsed.(*ecdsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("key is %T, not ECDSA", parsed)
	}
	return key, nil
}

// a2aCertFingerprint is a short SHA-256 of a certificate's DER: a public value,
// for an annotation.
func a2aCertFingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:])[:a2aNATSFingerprintLength]
}
