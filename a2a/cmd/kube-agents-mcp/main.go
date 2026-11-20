// Command kube-agents-mcp is the workstation-side Model Context Protocol (MCP)
// stdio bridge for kube-agents: it serves MCP over stdio and forwards tool
// calls to the in-cluster a2a-mcp server, which fronts the kube-agents A2A v2
// (next) JetStream message bus.
//
// Run with --version to print the version, or without arguments as a stdio MCP
// server. See mcpbridge.RunCLI for the supported flags.
package main

import (
	"fmt"
	"os"
	"runtime/debug"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
	"github.com/gke-labs/kube-agents/a2a/mcpbridge/kube"
)

const (
	binaryName = "kube-agents-mcp"
	// devVersion is what a build without the release's -ldflags reports when Go
	// recorded no module version either.
	devVersion = "dev"
	// develModuleVersion is the module version Go records for a build of the
	// main module from a working tree with no version control information.
	develModuleVersion = "(devel)"
)

// version is the release (X.Y.Z) a release archive was built for, stamped by
// scripts/release/build_release_binaries.sh with -ldflags "-X main.version=...".
var version = devVersion

// readBuildInfo is debug.ReadBuildInfo, replaced in tests.
var readBuildInfo = debug.ReadBuildInfo

// resolvedVersion is the stamped release, or for a build without one the module
// version Go recorded (the version or pseudo-version `go install ...@<ref>`
// resolved), or devVersion when it recorded none.
func resolvedVersion() string {
	if version != devVersion {
		return version
	}
	if info, ok := readBuildInfo(); ok && info.Main.Version != "" && info.Main.Version != develModuleVersion {
		return info.Main.Version
	}
	return devVersion
}

// versionString is what --version prints.
func versionString() string {
	return binaryName + " " + resolvedVersion()
}

func main() {
	opts := mcpbridge.ServerOptions{Cluster: kube.New(), VersionText: versionString()}
	if err := mcpbridge.RunCLIWithOptions(os.Args[1:], opts); err != nil {
		fmt.Fprintf(os.Stderr, "kube-agents-mcp: %v\n", err)
		os.Exit(1)
	}
}
