package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// renderPersonaMain is `worker-adapter render-persona --harness H --in DIR
// --out DIR`: the persona's rendering for the harness, sealed read-only. The
// relay target comes from CREDENTIAL_PROXY_URL only so that the render
// accepts the MCP servers the persona grants; the rendering itself carries no
// MCP configuration. The persona's integrity is
// required as lib.EnvPersonaIntegrity says, as for the worker.
func renderPersonaMain(args []string, getenv func(string) string) int {
	fs := flag.NewFlagSet(workeradapter.RenderPersonaCommand, flag.ContinueOnError)
	harness := fs.String("harness", "", "the harness to render for")
	in := fs.String("in", "", "the persona directory")
	out := fs.String("out", "", "the directory to render into (empty or absent)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *harness == "" || *in == "" || *out == "" {
		fmt.Fprintln(os.Stderr, "usage: worker-adapter render-persona --harness H --in DIR --out DIR")
		return 2
	}
	require, err := workeradapter.ParsePersonaIntegrity(getenv(lib.EnvPersonaIntegrity))
	if err != nil {
		fmt.Fprintln(os.Stderr, "render-persona:", err)
		return 2
	}
	relay := workeradapter.ProfileRelay(selfPath(), getenv)
	if err := workeradapter.RenderPersonaTo(*in, *out, persona.Harness(*harness), relay, require); err != nil {
		fmt.Fprintln(os.Stderr, "render-persona:", err)
		return 1
	}
	return 0
}
