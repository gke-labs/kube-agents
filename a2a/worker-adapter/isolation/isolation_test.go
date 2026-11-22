package isolation

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func table(t *testing.T) Table {
	t.Helper()
	tb, err := ForVersion("1.5.13\n")
	if err != nil {
		t.Fatal(err)
	}
	return tb
}

func write(t *testing.T, p, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func exists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

// tempDir is t.TempDir with the owner write bit restored on every directory
// before cleanup, since placeholders are read-only.
func tempDir(t *testing.T) string {
	d := t.TempDir()
	t.Cleanup(func() {
		_ = filepath.WalkDir(d, func(p string, e fs.DirEntry, err error) error {
			if err == nil && e.IsDir() {
				_ = os.Chmod(p, 0o755)
			}
			return nil
		})
	})
	return d
}

func TestForVersion(t *testing.T) {
	for _, in := range []string{"1.5.13", "1.5.13\n", "agy 1.5.13\n", "\n  1.5.13  \nextra"} {
		tb, err := ForVersion(in)
		if err != nil || tb.Version != "1.5.13" {
			t.Errorf("ForVersion(%q) = %q, %v; want 1.5.13", in, tb.Version, err)
		}
	}
	for _, in := range []string{"", "\n", "1.5.14", "1.5.13-rc1", "1.5", "agy"} {
		_, err := ForVersion(in)
		if !errors.Is(err, ErrUnknownVersion) {
			t.Errorf("ForVersion(%q) err = %v; want ErrUnknownVersion", in, err)
		}
	}
}

func TestForVersionNamesTheVersion(t *testing.T) {
	_, err := ForVersion("1.6.0")
	if err == nil || !strings.Contains(err.Error(), `"1.6.0"`) {
		t.Fatalf("err = %v; want it to name the version", err)
	}
}

// Every 1.5.13 entry that loaded or was read in the probes is in the table.
func TestTable1513Entries(t *testing.T) {
	got := map[string]Kind{}
	for _, e := range table(t).Entries {
		got[e.Name] = e.Kind
	}
	want := map[string]Kind{
		".agents": Dir, ".agent": Dir, "_agents": Dir, "_agent": Dir, ".gemini": Dir,
		"AGENTS.md": File, "GEMINI.md": File,
		".codeium.yaml": File, ".codeium.yml": File, "codeium.yaml": File, "codeium.yml": File,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("1.5.13 entries = %v; want %v", got, want)
	}
}

func TestScrubRemovesEntriesAndKeepsOtherFiles(t *testing.T) {
	ws := tempDir(t)
	for _, e := range table(t).Entries {
		p := filepath.Join(ws, e.Name)
		if e.Kind == Dir {
			write(t, filepath.Join(p, "hooks.json"), "{}")
		} else {
			write(t, p, "rule")
		}
	}
	write(t, filepath.Join(ws, "main.go"), "package main")
	write(t, filepath.Join(ws, "sub", "AGENTS.md"), "nested") // not loaded from the root
	write(t, filepath.Join(ws, "notes", "agents.md"), "x")

	removed, err := table(t).Scrub(ws)
	if err != nil {
		t.Fatal(err)
	}
	if len(removed) != len(table(t).Entries) {
		t.Errorf("removed %d entries %v; want %d", len(removed), removed, len(table(t).Entries))
	}
	for _, e := range table(t).Entries {
		if exists(filepath.Join(ws, e.Name)) {
			t.Errorf("%s survived the scrub", e.Name)
		}
	}
	for _, keep := range []string{"main.go", "sub/AGENTS.md", "notes/agents.md"} {
		if !exists(filepath.Join(ws, keep)) {
			t.Errorf("scrub removed %s", keep)
		}
	}
}

func TestScrubRemovesSymlinksNotTheirTargets(t *testing.T) {
	ws, outside := tempDir(t), tempDir(t)
	write(t, filepath.Join(outside, "r.md"), "rule")
	write(t, filepath.Join(outside, "dot", "hooks.json"), "{}")
	must(t, os.Symlink(filepath.Join(outside, "r.md"), filepath.Join(ws, "AGENTS.md")))
	must(t, os.Symlink(filepath.Join(outside, "dot"), filepath.Join(ws, ".agents")))
	must(t, os.Symlink(filepath.Join(ws, "missing"), filepath.Join(ws, "GEMINI.md"))) // dangling

	removed, err := table(t).Scrub(ws)
	if err != nil {
		t.Fatal(err)
	}
	if len(removed) != 3 {
		t.Errorf("removed %v; want the 3 links", removed)
	}
	for _, n := range []string{"AGENTS.md", ".agents", "GEMINI.md"} {
		if exists(filepath.Join(ws, n)) {
			t.Errorf("link %s survived", n)
		}
	}
	for _, n := range []string{"r.md", "dot/hooks.json"} {
		if !exists(filepath.Join(outside, n)) {
			t.Errorf("scrub followed a link and removed %s", n)
		}
	}
}

func TestScrubRemovesReadOnlyTrees(t *testing.T) {
	ws := tempDir(t)
	write(t, filepath.Join(ws, ".agents", "rules", "r.md"), "x")
	must(t, os.Chmod(filepath.Join(ws, ".agents", "rules"), 0o555))
	must(t, os.Chmod(filepath.Join(ws, ".agents"), 0o555))
	if _, err := table(t).Scrub(ws); err != nil {
		t.Fatal(err)
	}
	if exists(filepath.Join(ws, ".agents")) {
		t.Fatal("read-only .agents survived")
	}
}

func TestPlaceThenScrubKeepsPlaceholders(t *testing.T) {
	ws := tempDir(t)
	tb := table(t)
	must(t, tb.Place(ws))
	for _, e := range tb.Entries {
		p := filepath.Join(ws, e.Name)
		fi, err := os.Lstat(p)
		if !e.Placeholder {
			if err == nil {
				t.Errorf("placeholder made for %s, which allows none", e.Name)
			}
			continue
		}
		if err != nil {
			t.Fatalf("no placeholder for %s: %v", e.Name, err)
		}
		if fi.Mode().Perm()&0o222 != 0 {
			t.Errorf("placeholder %s is writable: %v", e.Name, fi.Mode())
		}
		if (e.Kind == File) != fi.IsDir() {
			t.Errorf("placeholder %s has the wrong kind: %v", e.Name, fi.Mode())
		}
	}
	removed, err := tb.Scrub(ws)
	if err != nil || len(removed) != 0 {
		t.Fatalf("scrub after Place removed %v, err %v; want nothing", removed, err)
	}
	must(t, tb.Place(ws)) // idempotent
}

// An entry that looks like a placeholder but is not inert is removed.
func TestScrubRemovesNonInertLookalikes(t *testing.T) {
	cases := map[string]func(t *testing.T, p string){
		"writable empty dir for a file entry": func(t *testing.T, p string) { must(t, os.Mkdir(p, 0o755)) },
		"read-only dir with content": func(t *testing.T, p string) {
			write(t, filepath.Join(p, "x"), "y")
			must(t, os.Chmod(p, 0o555))
		},
		"read-only non-empty file for a file entry": func(t *testing.T, p string) {
			write(t, p, "rule")
			must(t, os.Chmod(p, 0o444))
		},
	}
	for name, mk := range cases {
		t.Run(name, func(t *testing.T) {
			ws := tempDir(t)
			mk(t, filepath.Join(ws, "AGENTS.md"))
			removed, err := table(t).Scrub(ws)
			if err != nil {
				t.Fatal(err)
			}
			if len(removed) != 1 || exists(filepath.Join(ws, "AGENTS.md")) {
				t.Fatalf("removed %v; want AGENTS.md gone", removed)
			}
		})
	}
	t.Run("non-empty read-only file for a dir entry", func(t *testing.T) {
		ws := tempDir(t)
		write(t, filepath.Join(ws, ".agents"), "{}")
		must(t, os.Chmod(filepath.Join(ws, ".agents"), 0o444))
		if removed, _ := table(t).Scrub(ws); len(removed) != 1 {
			t.Fatalf("removed %v; want .agents", removed)
		}
	})
	t.Run("placeholder shape for an entry without placeholders", func(t *testing.T) {
		ws := tempDir(t)
		must(t, os.Mkdir(filepath.Join(ws, "codeium.yaml"), 0o555))
		if removed, _ := table(t).Scrub(ws); len(removed) != 1 {
			t.Fatalf("removed %v; want codeium.yaml", removed)
		}
	})
}

func TestPrepareTurnScrubsWorkspaceAndEveryConversationDir(t *testing.T) {
	ws, home := tempDir(t), tempDir(t)
	brain := filepath.Join(home, BrainDir)
	write(t, filepath.Join(ws, "AGENTS.md"), "rule")
	write(t, filepath.Join(brain, "c1", "AGENTS.md"), "rule")
	write(t, filepath.Join(brain, "c1", ".agents", "hooks.json"), "{}")
	write(t, filepath.Join(brain, "c1", "plan.md"), "artifact")
	write(t, filepath.Join(brain, "c1", ".system_generated", "logs", "transcript.jsonl"), "{}")
	write(t, filepath.Join(brain, "c2", "_agent", "AGENTS.md"), "rule")
	write(t, filepath.Join(brain, "stray-file"), "x")
	outside := tempDir(t)
	write(t, filepath.Join(outside, "AGENTS.md"), "rule")
	must(t, os.Symlink(outside, filepath.Join(brain, "c3")))

	removed, err := table(t).PrepareTurn(ws, home)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		filepath.Join(ws, "AGENTS.md"),
		filepath.Join(brain, "c1", ".agents"),
		filepath.Join(brain, "c1", "AGENTS.md"),
		filepath.Join(brain, "c2", "_agent"),
		filepath.Join(brain, "c3"),
	}
	sort.Strings(removed)
	sort.Strings(want)
	if !reflect.DeepEqual(removed, want) {
		t.Errorf("removed\n %v\nwant\n %v", removed, want)
	}
	for _, keep := range []string{"c1/plan.md", "c1/.system_generated/logs/transcript.jsonl", "stray-file"} {
		if !exists(filepath.Join(brain, keep)) {
			t.Errorf("PrepareTurn removed artifact %s", keep)
		}
	}
	if !exists(filepath.Join(outside, "AGENTS.md")) {
		t.Error("PrepareTurn followed the brain symlink")
	}
	// The workspace now holds placeholders; brain directories get none.
	if fi, err := os.Lstat(filepath.Join(ws, "AGENTS.md")); err != nil || !fi.IsDir() {
		t.Errorf("workspace AGENTS.md placeholder missing: %v", err)
	}
	if exists(filepath.Join(brain, "c1", "AGENTS.md")) {
		t.Error("placeholder made in a conversation directory")
	}
}

func TestPrepareTurnWithoutBrainDir(t *testing.T) {
	ws, home := tempDir(t), tempDir(t)
	if _, err := table(t).PrepareTurn(ws, home); err != nil {
		t.Fatal(err)
	}
}

func TestPrepareTurnFailsClosed(t *testing.T) {
	ws, home := tempDir(t), tempDir(t)
	file := filepath.Join(ws, "f")
	write(t, file, "")
	for name, c := range map[string][2]string{
		"empty workspace":     {"", home},
		"empty home":          {ws, ""},
		"relative workspace":  {"ws", home},
		"relative home":       {ws, "home"},
		"missing workspace":   {filepath.Join(ws, "nope"), home},
		"workspace is a file": {file, home},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := table(t).PrepareTurn(c[0], c[1]); err == nil {
				t.Fatal("PrepareTurn succeeded; want an error")
			}
		})
	}
}

func TestPrepareTurnFailsOnAnUnreadableBrainDir(t *testing.T) {
	ws, home := tempDir(t), tempDir(t)
	brain := filepath.Join(home, BrainDir)
	must(t, os.MkdirAll(brain, 0o755))
	write(t, filepath.Join(brain, "c1", "AGENTS.md"), "rule")
	must(t, os.Chmod(brain, 0o000))
	if _, err := os.ReadDir(brain); err == nil {
		t.Skip("running as a user that ignores permissions")
	}
	t.Cleanup(func() { _ = os.Chmod(brain, 0o755) })
	if _, err := table(t).PrepareTurn(ws, home); err == nil {
		t.Fatal("PrepareTurn succeeded; want an error")
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
