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
	found, ok := l.scanForIntegrity()
	if !ok {
		return IntegrityUnverified, 0
	}
	before := len(l.problems)
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
		if _, present := found[rel]; !present {
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
func (l *loader) scanForIntegrity() (map[string]string, bool) {
	found := map[string]string{}
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
		if d.Type()&fs.ModeSymlink != 0 {
			l.add(rel, "", "is a symlink; %s covers regular files only", IntegrityManifestFile)
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
			return nil
		}
		if sum, read := l.readSums[rel]; read {
			found[rel] = sum
			return nil
		}
		sum, err := hashNoFollow(full)
		if errors.Is(err, errSymlink) || errors.Is(err, errNotRegular) {
			l.add(rel, "", "is not a regular file; %s covers regular files only", IntegrityManifestFile)
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
		return nil, false
	}
	return found, true
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
