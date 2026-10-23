// persona validates a persona directory and renders it for a harness
// (a2a/lib/persona), for CI and for checking a persona locally:
//
//	persona validate --in <dir>
//	persona render --harness claude-code --in <dir> --out <dir> \
//	    [--relay-url <url> --relay-command '<cmd> <arg> ... {url} ...']
//
// render writes the harness config files under --out, which must be empty
// or absent, and prints {"args", "model", "warnings"} as JSON on stdout. The
// relay flags stand in for what the profile worker supplies in the pod.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

const (
	dirMode  = 0o755
	fileMode = 0o644
	execMode = 0o755
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: persona validate|render [flags]")
		return 2
	}
	var err error
	switch args[0] {
	case "validate":
		err = validate(args[1:], stderr)
	case "render":
		err = renderCmd(args[1:], stdout, stderr)
	default:
		fmt.Fprintf(stderr, "persona: unknown command %q (validate, render)\n", args[0])
		return 2
	}
	var usage usageError
	switch {
	case err == nil:
		return 0
	case errors.As(err, &usage):
		fmt.Fprintln(stderr, "persona:", err)
		return 2
	default:
		fmt.Fprintln(stderr, "persona:", err)
		return 1
	}
}

type usageError string

func (e usageError) Error() string { return string(e) }

func validate(args []string, stderr io.Writer) error {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	in := fs.String("in", "", "the persona directory")
	if err := fs.Parse(args); err != nil {
		return usageError(err.Error())
	}
	if *in == "" {
		return usageError("validate needs --in")
	}
	_, err := persona.Load(*in)
	return err
}

type result struct {
	Args     []string `json:"args"`
	Model    string   `json:"model,omitempty"`
	Warnings []string `json:"warnings"`
}

func renderCmd(args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("render", flag.ContinueOnError)
	fs.SetOutput(stderr)
	harness := fs.String("harness", "", "the harness to render for (claude-code)")
	in := fs.String("in", "", "the persona directory")
	out := fs.String("out", "", "the directory to write the harness config into (empty or absent)")
	relayURL := fs.String("relay-url", "", "the broker's MCP relay base URL")
	relayCmd := fs.String("relay-command", "", "the stdio proxy to the relay, space-separated, {url} for the server URL")
	if err := fs.Parse(args); err != nil {
		return usageError(err.Error())
	}
	if *harness == "" || *in == "" || *out == "" {
		return usageError("render needs --harness, --in and --out")
	}
	adapter, err := render.For(persona.Harness(*harness))
	if err != nil {
		return usageError(err.Error())
	}
	p, err := persona.Load(*in)
	if err != nil {
		return err
	}
	target := persona.Target{}
	fields := strings.Fields(*relayCmd)
	switch {
	case *relayURL != "" && len(fields) == 0:
		return usageError("--relay-url needs --relay-command")
	case *relayURL == "" && len(fields) > 0:
		return usageError("--relay-command needs --relay-url")
	}
	if *relayURL != "" {
		target.Relay = persona.Relay{URL: *relayURL, Command: fields[0], Args: fields[1:]}
	}
	output, err := adapter.Render(p, target)
	if err != nil {
		return err
	}
	if err := render.CheckOutput(output); err != nil {
		return err
	}
	if err := write(*out, output.Files); err != nil {
		return err
	}
	for _, w := range output.Warnings {
		fmt.Fprintln(stderr, "warning:", w)
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(result{Args: output.Args, Model: output.Model, Warnings: append([]string{}, output.Warnings...)})
}

// write puts files under out. It refuses a non-empty out: a stale file from
// an earlier render would load as part of this one.
func write(out string, files []persona.File) error {
	if entries, err := os.ReadDir(out); err == nil && len(entries) > 0 {
		return fmt.Errorf("--out %s is not empty", out)
	}
	if err := os.MkdirAll(out, dirMode); err != nil {
		return err
	}
	for _, f := range files {
		full := filepath.Join(out, filepath.FromSlash(f.Path))
		if err := os.MkdirAll(filepath.Dir(full), dirMode); err != nil {
			return err
		}
		mode := os.FileMode(fileMode)
		if f.Executable {
			mode = execMode
		}
		if err := os.WriteFile(full, f.Data, mode); err != nil {
			return err
		}
	}
	return nil
}
