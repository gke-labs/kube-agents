package persona

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
)

// IntegrityManifestFile is the persona's build-time content manifest: one
// `sha256sum` line for every file in the persona but itself. Its name is the
// one the platform agent image gives the manifest of its skill trees
// (deploy/docker/Dockerfile, agents/platform/scripts/verify_skills_provenance.py),
// and the line format is the one sha256sum writes. Unlike that script, only
// the root manifest is left out: a file of the same name in a skill is
// rendered, so it must be listed.
//
// What the check proves, and what it doesn't: the persona's files are the
// ones its build recorded. That catches a bad build, a corrupted layer, a
// staging copy that went wrong, or a later code path that writes the tree.
// The manifest ships inside the image it describes, so a replaced or
// deliberately altered image carries a manifest that matches it. This is
// not a security boundary.
const IntegrityManifestFile = "skills_manifest.sha256"

const (
	// manifestCommentPrefix starts a line the manifest parser skips.
	manifestCommentPrefix = "#"
	// sha256sumBinaryMarker and findDotPrefix are stripped from a manifest
	// path: sha256sum writes the first in binary mode, and `find .` the
	// second.
	sha256sumBinaryMarker = "*"
	findDotPrefix         = "./"
	// sha256sumTextSeparator is what sha256sum writes between a digest and
	// its path in text mode.
	sha256sumTextSeparator = "  "
	// integrityManifestMode is the manifest's file mode: readable by all,
	// like the files it describes.
	integrityManifestMode = 0o644
	// manifestLineBreaks can't appear in a manifest path: each entry is one
	// line.
	manifestLineBreaks = "\r\n"
	// manifestFieldSeparators split a manifest line's digest from its path.
	manifestFieldSeparators = " \t"
)

// sha256HexRE is a SHA-256 digest as sha256sum writes it.
var sha256HexRE = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Integrity is what Load established about the persona's content.
type Integrity int

const (
	// IntegrityUnverified: the persona ships no IntegrityManifestFile, and
	// Load was not asked to require one. Its content was not checked.
	IntegrityUnverified Integrity = iota
	// IntegrityVerified: every file in the persona matched its manifest.
	IntegrityVerified
)

func (i Integrity) String() string {
	if i == IntegrityVerified {
		return "verified"
	}
	return "unverified"
}

// Options change what Load requires.
type Options struct {
	// RequireIntegrity refuses a persona that ships no
	// IntegrityManifestFile. Without it such a persona loads with
	// IntegrityUnverified. A manifest that is present is checked either way.
	RequireIntegrity bool
}

// checkIntegrity compares every file under the persona directory with its
// IntegrityManifestFile, adding a problem for each difference so a refused
// persona reports all of them at once. It returns the outcome and how many
// files it checked.
func (l *loader) checkIntegrity(require bool) (Integrity, int) {
	data, ok, missing := l.readFile(IntegrityManifestFile, filepath.Join(l.dir, IntegrityManifestFile))
	if missing {
		if require {
			l.add(IntegrityManifestFile, "", "is missing, and this worker requires a persona to carry one")
		}
		return IntegrityUnverified, 0
	}
	if !ok {
		// readFile has said why (a symlink, a device, oversize).
		return IntegrityUnverified, 0
	}
	expected, parsed := l.parseIntegrityManifest(data)
	if !parsed {
		return IntegrityUnverified, 0
	}
	// Taken before the scan, so a symlink or other non-regular entry the
	// scan reports leaves the persona unverified too.
	before := len(l.problems)
	found, refused, ok := l.scanForIntegrity()
	if !ok {
		return IntegrityUnverified, 0
	}
	for _, rel := range sortedKeys(found) {
		want, listed := expected[rel]
		switch {
		case !listed:
			l.add(rel, "", "is not in %s: the build did not record it", IntegrityManifestFile)
		case found[rel] != want:
			l.add(rel, "", "differs from %s: built %s, found %s", IntegrityManifestFile, want, found[rel])
		}
	}
	for _, rel := range sortedKeys(expected) {
		// A listed path the scan refused is there, just not a regular
		// file, and the scan has said so.
		if _, present := found[rel]; !present && !refused[rel] {
			l.add(rel, "", "is recorded in %s but missing", IntegrityManifestFile)
		}
	}
	if len(l.problems) > before {
		return IntegrityUnverified, len(found)
	}
	return IntegrityVerified, len(found)
}

// parseIntegrityManifest reads sha256sum output into {path: digest}. It is
// stricter than sha256sum -c: a malformed line, a digest that is not 64
// lowercase hex characters, a path that leaves the persona, and a repeated
// path are each a problem.
func (l *loader) parseIntegrityManifest(data []byte) (map[string]string, bool) {
	entries := map[string]string{}
	before := len(l.problems)
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 0, maxFileBytes), maxFileBytes)
	for n := 1; sc.Scan(); n++ {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, manifestCommentPrefix) {
			continue
		}
		field := fmt.Sprintf("line %d", n)
		// sha256sum -c splits on the first run of blanks, tabs included.
		cut := strings.IndexAny(line, manifestFieldSeparators)
		if cut < 0 {
			l.add(IntegrityManifestFile, field, "is not a sha256sum line (<digest>  <path>)")
			continue
		}
		digest, name := line[:cut], strings.TrimSpace(line[cut:])
		if name == "" {
			l.add(IntegrityManifestFile, field, "is not a sha256sum line (<digest>  <path>)")
			continue
		}
		name = strings.TrimPrefix(name, sha256sumBinaryMarker)
		name = strings.TrimPrefix(name, findDotPrefix)
		switch {
		case !sha256HexRE.MatchString(digest):
			l.add(IntegrityManifestFile, field, "%q is not a lowercase hex SHA-256 digest", digest)
		case !LocalPath(name) || path.Clean(name) != name:
			l.add(IntegrityManifestFile, field, "%q is not a plain path inside the persona", name)
		case name == IntegrityManifestFile:
			l.add(IntegrityManifestFile, field, "lists the manifest itself, which can't carry its own digest (build it with ! -path ./%s)", IntegrityManifestFile)
		case l.scope != nil && !l.scope(name):
			l.add(IntegrityManifestFile, field, "%q is outside the files this check covers", name)
		case entries[name] != "":
			l.add(IntegrityManifestFile, field, "%q is listed twice", name)
		default:
			entries[name] = digest
		}
	}
	if err := sc.Err(); err != nil {
		l.add(IntegrityManifestFile, "", "%v", err)
	}
	return entries, len(l.problems) == before
}

// scanForIntegrity hashes every regular file under the persona directory
// except the root manifest. A file the format reader already read is taken
// at the digest of those bytes, so what is verified is what renders; the
// rest (README.md, say) is hashed here. A symlink or other non-regular entry anywhere is
// a problem rather than something to follow or skip: the build records
// regular files only, so one is always something it did not produce. A file
// named like the manifest below the root is an ordinary file here, and is
// reported unless the manifest lists it.
func (l *loader) scanForIntegrity() (found map[string]string, refused map[string]bool, ok bool) {
	found = map[string]string{}
	refused = map[string]bool{}
	// The trailing separator makes WalkDir descend into a persona directory
	// that is itself a symlink (a mount path pointing at the payload), as
	// os.ReadDir does for the format reader.
	root := l.dir + string(filepath.Separator)
	err := filepath.WalkDir(root, func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, full)
		rel = filepath.ToSlash(rel)
		if rel == "." {
			return nil
		}
		if l.scope != nil && !l.scope(rel) {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 {
			l.add(rel, "", "is a symlink; %s covers regular files only", IntegrityManifestFile)
			refused[rel] = true
			return nil
		}
		if d.IsDir() {
			return nil
		}
		if rel == IntegrityManifestFile {
			return nil
		}
		if !d.Type().IsRegular() {
			l.add(rel, "", "is not a regular file; %s covers regular files only", IntegrityManifestFile)
			refused[rel] = true
			return nil
		}
		if sum, read := l.readSums[rel]; read {
			found[rel] = sum
			return nil
		}
		sum, err := hashNoFollow(full)
		if errors.Is(err, errSymlink) || errors.Is(err, errNotRegular) {
			l.add(rel, "", "is not a regular file; %s covers regular files only", IntegrityManifestFile)
			refused[rel] = true
			return nil
		}
		if err != nil {
			return err
		}
		found[rel] = sum
		return nil
	})
	if err != nil {
		l.err = err
		return nil, nil, false
	}
	return found, refused, true
}

// errNotRegular is what hashNoFollow returns for an open file that is not a
// regular file.
var errNotRegular = errors.New("not a regular file")

// hashNoFollow returns a regular file's SHA-256, opening it the way readFile
// does so a file swapped for a link after the walk listed it is refused.
func hashNoFollow(full string) (string, error) {
	f, err := openNoFollow(full)
	if err != nil {
		return "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", errNotRegular
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// CheckTree compares the files under dir that scope selects with dir's
// IntegrityManifestFile, for a tree that is not a persona directory: the
// session pod's Claude Code home, say, where the harness keeps its own state
// beside the shipped files. scope takes a dir-relative, slash-separated path
// and reports whether the check covers it; a directory it rejects is not
// entered, and a manifest entry it rejects is a problem. A tree without a
// manifest is IntegrityUnverified with a nil error; any difference is an
// *InvalidError listing them all.
func CheckTree(dir string, scope func(rel string) bool) (Integrity, int, error) {
	l := &loader{dir: dir, scope: scope}
	integrity, files := l.checkIntegrity(false)
	if l.err != nil {
		return IntegrityUnverified, 0, l.err
	}
	if len(l.problems) > 0 {
		return IntegrityUnverified, files, &InvalidError{Dir: dir, Problems: l.problems}
	}
	return integrity, files, nil
}

// FormatIntegrityManifest renders files, keyed by slash-separated path
// relative to the tree's root, as an IntegrityManifestFile: one sha256sum
// line each, "./"-prefixed and sorted by path, as the image builds write it.
func FormatIntegrityManifest(files map[string][]byte) []byte {
	var b strings.Builder
	for _, rel := range sortedKeys(files) {
		sum := sha256.Sum256(files[rel])
		b.WriteString(hex.EncodeToString(sum[:]) + sha256sumTextSeparator + findDotPrefix + rel + "\n")
	}
	return []byte(b.String())
}

// WriteIntegrityManifest writes dir's IntegrityManifestFile over every
// regular file under it but the root manifest, replacing one already there.
// A symlink or other non-regular entry is an error: the check would refuse
// it, so a manifest can't describe it.
func WriteIntegrityManifest(dir string) error {
	files := map[string][]byte{}
	// The trailing separator walks into a dir that is itself a symlink, as
	// the check does (scanForIntegrity).
	root := dir + string(filepath.Separator)
	err := filepath.WalkDir(root, func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, full)
		rel = filepath.ToSlash(rel)
		switch {
		case rel == ".", d.IsDir() && d.Type()&fs.ModeSymlink == 0:
			return nil
		case rel == IntegrityManifestFile:
			return nil
		case !d.Type().IsRegular():
			return fmt.Errorf("%s: not a regular file; %s covers regular files only", rel, IntegrityManifestFile)
		case strings.ContainsAny(rel, manifestLineBreaks):
			return fmt.Errorf("%q: a line break in a name can't be written as one manifest line", rel)
		}
		f, err := openNoFollow(full)
		if err != nil {
			return err
		}
		defer f.Close()
		data, err := io.ReadAll(f)
		if err != nil {
			return err
		}
		files[rel] = data
		return nil
	})
	if err != nil {
		return err
	}
	return writeManifestFile(dir, files)
}

// WriteIntegrityManifestOf writes dir's IntegrityManifestFile over exactly
// the files rels names (slash-separated, relative to dir), such as a
// repository's tracked files, so anything else in the directory (an ignored
// cache, say) is left out and left alone. A name that is not a plain path
// inside dir, holds a line break, or is not a regular file is an error.
func WriteIntegrityManifestOf(dir string, rels []string) error {
	files := map[string][]byte{}
	for _, rel := range rels {
		switch {
		case rel == IntegrityManifestFile:
			continue
		case !LocalPath(rel) || path.Clean(rel) != rel:
			return fmt.Errorf("%q: not a plain path inside the persona", rel)
		case strings.ContainsAny(rel, manifestLineBreaks):
			return fmt.Errorf("%q: a line break in a name can't be written as one manifest line", rel)
		}
		f, err := openNoFollow(filepath.Join(dir, filepath.FromSlash(rel)))
		if errors.Is(err, errSymlink) {
			return fmt.Errorf("%s: not a regular file; %s covers regular files only", rel, IntegrityManifestFile)
		}
		if err != nil {
			return err
		}
		data, err := readRegular(f)
		_ = f.Close() // read-only; readRegular's error is the one that matters
		if err != nil {
			return fmt.Errorf("%s: %w", rel, err)
		}
		files[rel] = data
	}
	return writeManifestFile(dir, files)
}

// readRegular reads an open file that must be a regular file.
func readRegular(f *os.File) ([]byte, error) {
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("not a regular file; %s covers regular files only", IntegrityManifestFile)
	}
	return io.ReadAll(f)
}

// writeManifestFile writes files' manifest into dir.
func writeManifestFile(dir string, files map[string][]byte) error {
	// Written beside it and renamed over it, so a symlink planted at the
	// manifest's path is replaced rather than followed.
	tmp, err := os.CreateTemp(dir, IntegrityManifestFile+".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(FormatIntegrityManifest(files)); err != nil {
		_ = tmp.Close() // the write error is the one to report; the temp file is removed either way
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), integrityManifestMode); err != nil { // #nosec G302 -- a build input committed beside the persona; read by everyone by design
		return err
	}
	return os.Rename(tmp.Name(), filepath.Join(dir, IntegrityManifestFile))
}
