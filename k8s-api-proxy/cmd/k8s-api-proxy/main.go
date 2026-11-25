// Command k8s-api-proxy is the stock server: pkg/proxyserver with no options but its version.
package main

import (
	"context"
	"log"
	"os"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/proxyserver"
)

// version is what the binary was built for: the release (X.Y.Z) for a release archive,
// stamped by scripts/release/build_release_binaries.sh, or the commit SHA for the
// kube-proxy image, stamped by deploy/kube-proxy/Dockerfile from KUBE_AGENTS_VERSION, which
// every image build passes as it does for the platform-agent image. Both use -ldflags "-X main.version=...".
// A build without the stamp leaves it empty, which proxyserver reports as a development build.
var version string

func main() {
	if err := proxyserver.Run(context.Background(), os.Args[1:], proxyserver.Options{Version: version}); err != nil {
		log.Fatal(err)
	}
}
