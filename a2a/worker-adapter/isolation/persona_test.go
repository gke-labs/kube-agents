package isolation

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// otherUID and otherGID stand for agy's user when the test's own user
// renders the persona: neither owns anything the test creates.
const otherUID, otherGID = 4242, 4242

// personaDir is a rendered persona owned by the test's user: rules files
// and a skill, read-only to everyone.
func personaDir(t *testing.T) string {
	t.Helper()
	d := tempDir(t)
	write(t, filepath.Join(d, "AGENTS.md"), "be careful")
	write(t, filepath.Join(d, "GEMINI.md"), "be careful")
	write(t, filepath.Join(d, "skills", "triage", "SKILL.md"), "---\nname: triage\n---\n")
	lockDown(t, d)
	return d
}

// lockDown removes every write bit under d.
func lockDown(t *testing.T, d string) {
	t.Helper()
	_ = filepath.Walk(d, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		mode := os.FileMode(0o444)
		if fi.IsDir() {
			mode = 0o555
		}
		return os.Chmod(p, mode)
	})
}

func TestCheckPersonaDirAcceptsAReadOnlyPersona(t *testing.T) {
	if err := CheckPersonaDir(personaDir(t), otherUID, otherGID); err != nil {
		t.Fatal(err)
	}
}

func TestCheckPersonaDirFailsClosed(t *testing.T) {
	cases := map[string]struct {
		mutate   func(t *testing.T, d string)
		uid, gid int
		want     string
	}{
		// The owner can chmod, so an entry agy owns is writable even with
		// no write bit set.
		// Refused either for the dir or, where the test user owns a parent
		// in a sticky /tmp, for that parent; both name uid.
		"the dir owned by agy, no write bits": {
			mutate: func(t *testing.T, d string) {},
			uid:    os.Getuid(), gid: os.Getgid(), want: "uid",
		},
		// agy's supplementary groups are unknown here, so any group write
		// bit fails, whatever gid is passed.
		"the dir group-writable": {
			mutate: func(t *testing.T, d string) { must(t, os.Chmod(d, 0o575)) },
			uid:    otherUID, gid: otherGID, want: "writable",
		},
		"the dir writable by others": {
			mutate: func(t *testing.T, d string) { must(t, os.Chmod(d, 0o557)) },
			uid:    otherUID, gid: otherGID, want: "writable",
		},
		"a skill file writable by others": {
			mutate: func(t *testing.T, d string) {
				must(t, os.Chmod(filepath.Join(d, "skills", "triage", "SKILL.md"), 0o446))
			},
			uid: otherUID, gid: otherGID, want: "writable",
		},
		"a hooks file beside the persona": {
			mutate: func(t *testing.T, d string) {
				must(t, os.Chmod(d, 0o755))
				write(t, filepath.Join(d, "hooks.json"), "{}")
				lockDown(t, d)
			},
			uid: otherUID, gid: otherGID, want: "not a persona entry",
		},
		"a symlink under skills": {
			mutate: func(t *testing.T, d string) {
				sk := filepath.Join(d, "skills")
				must(t, os.Chmod(sk, 0o755))
				must(t, os.Symlink("/etc/passwd", filepath.Join(sk, "link")))
				must(t, os.Chmod(sk, 0o555))
			},
			uid: otherUID, gid: otherGID, want: "neither a directory nor a regular file",
		},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			d := personaDir(t)
			c.mutate(t, d)
			err := CheckPersonaDir(d, c.uid, c.gid)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("err = %v; want one saying %q", err, c.want)
			}
		})
	}
	for name, dir := range map[string]string{
		"empty":    "",
		"relative": "persona",
		"missing":  filepath.Join(tempDir(t), "nope"),
	} {
		if err := CheckPersonaDir(dir, otherUID, otherGID); err == nil {
			t.Errorf("%s dir: accepted", name)
		}
	}
	f := filepath.Join(tempDir(t), "f")
	write(t, f, "")
	if err := CheckPersonaDir(f, otherUID, otherGID); err == nil {
		t.Error("a file accepted as the persona dir")
	}
	real := personaDir(t)
	link := filepath.Join(tempDir(t), "persona")
	must(t, os.Symlink(real, link))
	// lstat follows a link written with a trailing "/" or "/.".
	for _, spelled := range []string{link, link + "/", link + "/.", link + "/./", link + "//"} {
		if err := CheckPersonaDir(spelled, otherUID, otherGID); err == nil {
			t.Errorf("a symlinked persona dir accepted as %q", spelled)
		}
	}
}

// TestCheckPersonaDirResolvesDotDotAfterALink: the persona's contents are
// checked where the kernel resolves the path, not where its text cleans to.
// safe/link points at real/sub, so safe/link/../persona is real/persona,
// whose skills tree holds a file others can write; safe/persona, where the
// text cleans to, is read-only. The check, and the load, must refuse it.
func TestCheckPersonaDirResolvesDotDotAfterALink(t *testing.T) {
	real := tempDir(t)
	must(t, os.Mkdir(filepath.Join(real, "sub"), 0o755))
	rp := filepath.Join(real, "persona")
	write(t, filepath.Join(rp, "AGENTS.md"), "x")
	write(t, filepath.Join(rp, "skills", "triage", "SKILL.md"), "evil")
	lockDown(t, real)
	must(t, os.Chmod(filepath.Join(rp, "skills", "triage", "SKILL.md"), 0o666)) // others could rewrite the skill
	safe := tempDir(t)
	sp := filepath.Join(safe, "persona")
	write(t, filepath.Join(sp, "AGENTS.md"), "x")
	write(t, filepath.Join(sp, "skills", "triage", "SKILL.md"), "good")
	must(t, os.Symlink(filepath.Join(real, "sub"), filepath.Join(safe, "link")))
	lockDown(t, safe)
	must(t, os.Chmod(filepath.Join(real, "sub"), 0o555)) // lockDown's chmod followed the link
	spelled := safe + "/link/../persona"
	if err := CheckPersonaDir(spelled, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "writable") {
		t.Fatalf("CheckPersonaDir(%s) = %v; the kernel reads %s, whose skill is writable", spelled, err, rp)
	}
	if _, err := LoadPersona(spelled, otherUID, otherGID); err == nil {
		t.Fatalf("LoadPersona(%s) loaded the unchecked tree", spelled)
	}
	if err := CheckPersonaDir(sp, otherUID, otherGID); err != nil {
		t.Fatalf("the plain safe path: %v", err)
	}
}

func TestCheckPersonaDirChecksItsAncestors(t *testing.T) {
	parent := tempDir(t)
	d := filepath.Join(parent, "persona")
	must(t, os.Mkdir(d, 0o755))
	write(t, filepath.Join(d, "AGENTS.md"), "x")
	lockDown(t, d)
	must(t, os.Chmod(parent, 0o777)) // others could rename persona away
	if err := CheckPersonaDir(d, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "writable") {
		t.Fatalf("a writable parent: %v", err)
	}
	must(t, os.Chmod(parent, 0o755))
	if err := CheckPersonaDir(d, otherUID, otherGID); err != nil {
		t.Fatalf("a read-only parent: %v", err)
	}
}

func TestLoadPersonaReadsTheCheckedTreeOnce(t *testing.T) {
	d := personaDir(t)
	files, err := LoadPersona(d, otherUID, otherGID)
	if err != nil {
		t.Fatal(err)
	}
	if string(files["AGENTS.md"]) != "be careful" || len(files["skills/triage/SKILL.md"]) == 0 || len(files) != 3 {
		t.Fatalf("loaded %v", files)
	}
	// A directory that fails the check loads nothing.
	bad := personaDir(t)
	must(t, os.Chmod(bad, 0o757))
	if _, err := LoadPersona(bad, otherUID, otherGID); err == nil {
		t.Fatal("a writable persona dir loaded")
	}
}

func TestCheckPersonaDirRequiresTheRightKinds(t *testing.T) {
	d := tempDir(t)
	must(t, os.Mkdir(filepath.Join(d, "AGENTS.md"), 0o755)) // a dir where a file belongs
	write(t, filepath.Join(d, "AGENTS.md", "hooks.json"), "{}")
	lockDown(t, d)
	if err := CheckPersonaDir(d, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "regular file") {
		t.Errorf("AGENTS.md as a directory: %v", err)
	}
	d2 := tempDir(t)
	write(t, filepath.Join(d2, "skills"), "not a dir")
	lockDown(t, d2)
	if err := CheckPersonaDir(d2, otherUID, otherGID); err == nil || !strings.Contains(err.Error(), "directory") {
		t.Errorf("skills as a file: %v", err)
	}
}
