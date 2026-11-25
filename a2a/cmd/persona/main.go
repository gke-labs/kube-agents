// persona validates a persona directory and renders it for a harness
// (a2a/lib/persona), for CI and for checking a persona locally:
//
//	persona validate --in <dir> [--require-integrity]
//	persona manifest --in <dir> [--git]
//	persona render --harness claude-code --in <dir> --out <dir> \
//	    [--relay-url <url> --relay-command '<cmd> <arg> ... {url} ...']
//
// render writes the harness config files under --out, which must be empty
// or absent, and prints {"args", "model", "warnings"} as JSON on stdout. The
// relay flags stand in for what the profile worker supplies in the pod.
//
// manifest writes the persona's content manifest
// (persona.IntegrityManifestFile) at the root of --in, over every other file
// in it, replacing any manifest already there. With --git it covers only the
// files git would put in a clean checkout of --in (tracked, or untracked and
// not ignored), so an ignored cache in the directory is left out and left
// alone. A shipped persona's manifest
// is committed beside it, because a persona image's Dockerfile is only
// FROM and COPY (hack/check-image-inventory.sh) and so can't run the recipe.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: persona validate|render|manifest [flags]")
		return 2
	}
	var err error
	switch args[0] {
	case "validate":
		err = validate(args[1:], stderr)
	case "render":
		err = renderCmd(args[1:], stdout, stderr)
	case "manifest":
		err = manifestCmd(args[1:], stderr)
	default:
		fmt.Fprintf(stderr, "persona: unknown command %q (validate, render, manifest)\n", args[0])
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
	require := fs.Bool("require-integrity", false, "refuse a persona without "+persona.IntegrityManifestFile+" (one that has it is checked either way)")
	if err := fs.Parse(args); err != nil {
		return usageError(err.Error())
	}
	if *in == "" {
		return usageError("validate needs --in")
	}
	_, err := persona.LoadWithOptions(*in, persona.Options{RequireIntegrity: *require})
	return err
}

// manifestCmd writes --in's content manifest, then loads the persona with
// the manifest required, so a persona that doesn't load fails the command
// (its new manifest stays; fix the persona and run it again). With --git it
// loads a clean copy of the files the manifest covers.
func manifestCmd(args []string, stderr io.Writer) error {
	fs := flag.NewFlagSet("manifest", flag.ContinueOnError)
	fs.SetOutput(stderr)
	in := fs.String("in", "", "the persona directory")
	fromGit := fs.Bool("git", false, "cover only the files git lists for --in (tracked, or untracked and not ignored)")
	if err := fs.Parse(args); err != nil {
		return usageError(err.Error())
	}
	if *in == "" {
		return usageError("manifest needs --in")
	}
	if !*fromGit {
		if err := persona.WriteIntegrityManifest(*in); err != nil {
			return err
		}
		_, err := persona.LoadWithOptions(*in, persona.Options{RequireIntegrity: true})
		return err
	}
	rels, err := gitPersonaFiles(*in)
	if err != nil {
		return err
	}
	if err := persona.WriteIntegrityManifestOf(*in, rels); err != nil {
		return err
	}
	return loadCleanCopy(*in, append(rels, persona.IntegrityManifestFile))
}

// loadCleanCopy copies rels (paths relative to dir, slash-separated) to a
// temporary directory and loads that copy with the manifest required: what a
// clean checkout of dir holds, so an ignored local file that --git left out
// of the manifest can't fail the load. A path listed twice is copied once.
func loadCleanCopy(dir string, rels []string) error {
	tmp, err := os.MkdirTemp("", "persona-manifest-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(tmp)
	for _, rel := range rels {
		data, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(rel)))
		if err != nil {
			return err
		}
		dst := filepath.Join(tmp, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(dst, data, 0o644); err != nil {
			return err
		}
	}
	_, err = persona.LoadWithOptions(tmp, persona.Options{RequireIntegrity: true})
	return err
}

// gitListArgs lists, relative to the directory git runs in, the files a
// clean checkout of it would hold: tracked ones, and untracked ones git
// doesn't ignore (a new file not yet added), NUL-separated.
var gitListArgs = []string{"ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."}

// gitPersonaFiles is dir's files as gitListArgs lists them, sorted, less any
// that no longer exist on disk (tracked but deleted in the working tree).
func gitPersonaFiles(dir string) ([]string, error) {
	cmd := exec.Command("git", append([]string{"-C", dir}, gitListArgs...)...) // #nosec G204 -- fixed git arguments; dir is the operator's own flag
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("listing %s with git: %w", dir, err)
	}
	var rels []string
	for _, rel := range strings.Split(string(out), "\x00") {
		if rel == "" {
			continue
		}
		if _, err := os.Lstat(filepath.Join(dir, filepath.FromSlash(rel))); errors.Is(err, os.ErrNotExist) {
			continue
		}
		rels = append(rels, rel)
	}
	sort.Strings(rels)
	return rels, nil
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
	if err := render.WriteFiles(*out, output.Files); err != nil {
		return fmt.Errorf("--out %w", err)
	}
	for _, w := range output.Warnings {
		fmt.Fprintln(stderr, "warning:", w)
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(result{Args: output.Args, Model: output.Model, Warnings: append([]string{}, output.Warnings...)})
}
