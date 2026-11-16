package lib

import (
	"crypto/tls"
	"os"

	"github.com/nats-io/nats.go"
)

// EnvNATSCAFile names the PEM file of the CA a client trusts the bus's serving
// certificate by. The operator mounts the bus's CA bundle into every client it
// renders and sets this to the file; the gateway passes both on to the session
// pods it spawns. Unset, a client trusts the system roots, which do not include
// the operator's CA, so against the rendered bus it fails verification and
// connects to nothing: TLS is required of every client there.
const EnvNATSCAFile = "NATS_CA_FILE"

// EnvNATSTLSServerName overrides the name a client checks the bus certificate
// against. Nothing the operator renders sets it: in a pod the URL's host is
// the Service name the certificate carries. It is for tooling that reaches
// the bus through a port-forward, where the URL's host is the loopback
// address and the certificate deliberately names no such thing.
const EnvNATSTLSServerName = "NATS_TLS_SERVER_NAME"

// TLSOptions is the bus trust every client takes: the CA from EnvNATSCAFile
// when it is set, nothing otherwise. lib.Connect applies it before the caller's
// options; a component that dials with nats.Connect directly appends it to its
// own. The file is read on each handshake, so a reconnect picks up a CA
// bundle the kubelet has updated in place.
func TLSOptions() []nats.Option {
	path := os.Getenv(EnvNATSCAFile)
	if path == "" {
		return nil
	}
	var opts []nats.Option
	if name := os.Getenv(EnvNATSTLSServerName); name != "" {
		// Before RootCAs, which keeps a TLS config it finds already set.
		opts = append(opts, nats.Secure(&tls.Config{ServerName: name, MinVersion: tls.VersionTLS12}))
	}
	return append(opts, nats.RootCAs(path))
}
