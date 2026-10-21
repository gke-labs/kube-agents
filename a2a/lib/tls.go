package lib

import (
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

// TLSOptions is the bus trust every client takes: the CA from EnvNATSCAFile
// when it is set, nothing otherwise. lib.Connect applies it before the caller's
// options; a component that dials with nats.Connect directly appends it to its
// own. The file is read on each connect, so a rebuilt connection picks up a CA
// bundle the kubelet has updated in place.
func TLSOptions() []nats.Option {
	path := os.Getenv(EnvNATSCAFile)
	if path == "" {
		return nil
	}
	return []nats.Option{nats.RootCAs(path)}
}
