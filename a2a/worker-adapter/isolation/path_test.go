package isolation

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestSearchedFirstStopsAtTheTrustedDir(t *testing.T) {
	got, found := SearchedFirst([]string{"/opt/h/bin"}, "/opt/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin")
	want := []string{"/opt/h/bin", "/opt/tools/bin", "/usr/local/sbin"}
	if !found || !slices.Equal(got, want) {
		t.Fatalf("got %q (found %v), want %q", got, found, want)
	}
	if got, found := SearchedFirst(nil, "/usr/local/bin/:/usr/bin"); !found || len(got) != 0 {
		t.Errorf("a trailing slash on the trusted dir: %q %v", got, found)
	}
	if got, _ := SearchedFirst(nil, "::/usr/local/bin"); len(got) != 2 {
		t.Errorf("empty entries dropped: %q", got)
	}
	// Spelled through a link and "..", it is not the trusted dir.
	if got, found := SearchedFirst(nil, "/opt/link/../usr/local/bin:/usr/local/bin"); !found || len(got) != 1 {
		t.Errorf("a spelled-out path matched the trusted dir: %q %v", got, found)
	}
	if _, found := SearchedFirst(nil, "/usr/bin:/bin"); found {
		t.Error("a PATH without the trusted dir reported it found")
	}
}

func TestCheckSearchPath(t *testing.T) {
	ro := tempDir(t)
	must(t, os.Chmod(ro, 0o555))
	roParentMissing := filepath.Join(ro, "bin") // missing, parent read-only
	rw := tempDir(t)
	rwParentMissing := filepath.Join(rw, "bin") // missing, parent writable

	if err := CheckSearchPath([]string{ro, roParentMissing}, otherUID, otherGID); err != nil {
		t.Fatalf("read-only dirs: %v", err)
	}
	if err := CheckSearchPath([]string{ro}, os.Getuid(), os.Getgid()); err == nil {
		t.Fatal("a dir agy owns passed; its owner can chmod it")
	}
	for name, c := range map[string]struct {
		dirs []string
		want string
	}{
		"a writable dir":                   {[]string{ro, rw}, "uid"},
		"a missing dir agy could create":   {[]string{rwParentMissing}, "uid"},
		"the working directory, empty":     {[]string{""}, "relative"},
		"the working directory, relative":  {[]string{"bin"}, "relative"},
		"an other-writable dir for others": {[]string{otherWritable(t)}, "is writable"},
	} {
		t.Run(name, func(t *testing.T) {
			uid, gid := os.Getuid(), os.Getgid()
			if name == "an other-writable dir for others" {
				uid, gid = otherUID, otherGID
			}
			err := CheckSearchPath(c.dirs, uid, gid)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("err = %v; want one saying %q", err, c.want)
			}
		})
	}
}

func otherWritable(t *testing.T) string {
	t.Helper()
	d := tempDir(t)
	must(t, os.Chmod(d, 0o777))
	return d
}

func TestCheckSearchPathChecksAncestorsLinksAndEntries(t *testing.T) {
	// A read-only dir under a parent others can write: the parent can
	// rename it away.
	open := tempDir(t)
	inner := filepath.Join(open, "bin")
	must(t, os.Mkdir(inner, 0o555))
	must(t, os.Chmod(open, 0o777))
	if err := CheckSearchPath([]string{inner}, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "writable") {
		t.Errorf("a writable ancestor: %v", err)
	}

	// A sticky, world-writable parent is fine for a child uid does not own.
	sticky := tempDir(t)
	child := filepath.Join(sticky, "bin")
	must(t, os.Mkdir(child, 0o555))
	must(t, os.Chmod(sticky, os.ModeSticky|0o777))
	if err := CheckSearchPath([]string{child}, otherUID, otherGID); err != nil {
		t.Errorf("a sticky parent over another user's dir: %v", err)
	}
	// ...but not where uid could create the missing entry.
	if err := CheckSearchPath([]string{filepath.Join(sticky, "missing")}, otherUID, otherGID); err == nil {
		t.Error("a missing entry in a sticky dir passed")
	}

	// Entries: a writable program, and a link to one.
	bin := tempDir(t)
	write(t, filepath.Join(bin, "kubectl"), "#!/bin/sh\n")
	must(t, os.Chmod(filepath.Join(bin, "kubectl"), 0o757))
	must(t, os.Chmod(bin, 0o555))
	if err := CheckSearchPath([]string{bin}, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "kubectl") {
		t.Errorf("a writable program inside: %v", err)
	}
	target := tempDir(t)
	write(t, filepath.Join(target, "gcloud"), "#!/bin/sh\n")
	must(t, os.Chmod(target, 0o777))
	links := tempDir(t)
	must(t, os.Symlink(filepath.Join(target, "gcloud"), filepath.Join(links, "gcloud")))
	must(t, os.Chmod(links, 0o555))
	if err := CheckSearchPath([]string{links}, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "writable") {
		t.Errorf("a link into a writable dir: %v", err)
	}

	// A symlinked PATH entry is checked through its target.
	realDir := tempDir(t)
	must(t, os.Chmod(realDir, 0o777))
	via := filepath.Join(tempDir(t), "via")
	must(t, os.Symlink(realDir, via))
	if err := CheckSearchPath([]string{via}, otherUID, otherGID); err == nil {
		t.Error("a symlinked entry whose target is writable passed")
	}
}

// TestCheckSearchPathChecksEveryLinkHop: a link whose first hop goes
// through a writable directory fails even though the chain's final target
// is safe, since that middle link can be retargeted.
func TestCheckSearchPathChecksEveryLinkHop(t *testing.T) {
	safe := tempDir(t)
	write(t, filepath.Join(safe, "kubectl"), "#!/bin/sh\n")
	must(t, os.Chmod(filepath.Join(safe, "kubectl"), 0o555))
	must(t, os.Chmod(safe, 0o555))
	middle := tempDir(t)
	must(t, os.Symlink(filepath.Join(safe, "kubectl"), filepath.Join(middle, "hop")))
	must(t, os.Chmod(middle, 0o777))
	bin := tempDir(t)
	must(t, os.Symlink(filepath.Join(middle, "hop"), filepath.Join(bin, "kubectl")))
	must(t, os.Chmod(bin, 0o555))
	if err := CheckSearchPath([]string{bin}, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), middle) {
		t.Fatalf("a hop through a writable dir: %v", err)
	}
	// The same chain with the middle directory locked passes.
	must(t, os.Chmod(middle, 0o555))
	if err := CheckSearchPath([]string{bin}, otherUID, otherGID); err != nil {
		t.Fatalf("an all read-only chain: %v", err)
	}
}

// TestCheckSearchPathEndingOnAStickyDir: the sticky exemption holds only
// for a directory something follows; a path that ends on one, however it
// is spelled, fails.
func TestCheckSearchPathEndingOnAStickyDir(t *testing.T) {
	sticky := tempDir(t)
	child := filepath.Join(sticky, "child")
	must(t, os.Mkdir(child, 0o555))
	must(t, os.Chmod(sticky, os.ModeSticky|0o777))
	for _, p := range []string{sticky, sticky + "/", sticky + "/.", child + "/..", sticky + "/./"} {
		if err := CheckSearchPath([]string{p}, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "writable") {
			t.Errorf("%q: err = %v; want the sticky dir writable", p, err)
		}
	}
	if err := CheckSearchPath([]string{child}, otherUID, otherGID); err != nil {
		t.Errorf("the child under it: %v", err)
	}
	// A link that ends on the sticky dir fails the same way.
	link := filepath.Join(tempDir(t), "l")
	must(t, os.Symlink(sticky+"/.", link))
	if err := CheckSearchPath([]string{link}, otherUID, otherGID); err == nil {
		t.Error("a link ending on the sticky dir passed")
	}
}

// TestCheckSearchPathRelativeLinksInAStickyDir: a link inside a sticky
// directory whose relative target stays in that directory keeps the
// directory's rule: ending on it fails, a missing target fails, and a chain
// ending on another user's entry passes.
func TestCheckSearchPathRelativeLinksInAStickyDir(t *testing.T) {
	sticky := tempDir(t)
	child := filepath.Join(sticky, "child")
	must(t, os.Mkdir(child, 0o555))
	must(t, os.Symlink(".", filepath.Join(sticky, "dot")))
	must(t, os.Symlink("l2", filepath.Join(sticky, "l1")))
	must(t, os.Symlink("child", filepath.Join(sticky, "l2")))
	must(t, os.Symlink("nothing", filepath.Join(sticky, "dangling")))
	must(t, os.Chmod(sticky, os.ModeSticky|0o777))
	for _, c := range []struct {
		entry string
		ok    bool
	}{
		{"dot", false},      // ends on the sticky dir itself
		{"dangling", false}, // uid could create the target there
		{"l1", true},        // l1 -> l2 -> child, none of them uid's
	} {
		err := CheckSearchPath([]string{filepath.Join(sticky, c.entry)}, otherUID, otherGID)
		if (err == nil) != c.ok {
			t.Errorf("%s: err = %v, want ok=%v", c.entry, err, c.ok)
		}
	}
}

// TestCheckSearchPathResolvesDotDotAfterALink: "safe/link/../x" is read by
// the kernel as the parent of link's target plus x, not safe/x; the check
// must follow the same path.
func TestCheckSearchPathResolvesDotDotAfterALink(t *testing.T) {
	evil := tempDir(t)
	must(t, os.Mkdir(filepath.Join(evil, "sub"), 0o555))
	must(t, os.Mkdir(filepath.Join(evil, "x"), 0o555))
	must(t, os.Chmod(evil, 0o777)) // others could swap evil/x
	safe := tempDir(t)
	must(t, os.Mkdir(filepath.Join(safe, "x"), 0o555))
	must(t, os.Symlink(filepath.Join(evil, "sub"), filepath.Join(safe, "link")))
	must(t, os.Chmod(safe, 0o555))
	spelled := safe + "/link/../x"
	if err := CheckSearchPath([]string{spelled}, otherUID, otherGID); err == nil {
		t.Fatalf("%s passed; the kernel reads %s/x, under a writable dir", spelled, evil)
	}
	if err := CheckSearchPath([]string{safe + "/x"}, otherUID, otherGID); err != nil {
		t.Fatalf("the plain safe path: %v", err)
	}
}
