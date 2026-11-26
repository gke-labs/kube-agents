// Command kube-proxy is the kube-agents build of k8s-api-proxy: the same server, with the
// task credential gate as its Authorize hook when the environment configures one.
package main

import (
	"context"
	"log"
	"log/slog"
	"os"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/proxyserver"
	"github.com/gke-labs/kube-agents/kube-proxy/taskgate"
)

// version is the commit the kube-proxy image was built from, stamped by
// deploy/kube-proxy/Dockerfile with -ldflags "-X main.version=..." from KUBE_AGENTS_VERSION,
// which every image build passes as it does for the platform-agent image. A build without
// the stamp leaves it empty, which proxyserver reports as a development build.
var version string

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	authorize, err := taskgate.FromEnv(os.Getenv, logger)
	if err != nil {
		log.Fatal(err)
	}
	opts := proxyserver.Options{
		Version:            version,
		Authorize:          authorize,
		ExtraCallerHeaders: taskgate.CallerHeaders(),
	}
	if err := proxyserver.Run(context.Background(), os.Args[1:], opts); err != nil {
		log.Fatal(err)
	}
}
