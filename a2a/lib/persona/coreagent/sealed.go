package coreagent

import (
	"fmt"
	"io/fs"
	"path/filepath"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// VerifyRendering checks a rendering the harness is about to read: nothing
// in it may be writable (no write bit on any entry, and none owned by uid,
// the harness's user, which could add one; a negative uid skips that part), and its files must match the content
// manifest the render step wrote beside them. A worker that requires its
// persona refuses one that fails, rather than read files it might have been
// able to change.
func VerifyRendering(root string, uid int) error {
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if info.Mode().Perm()&0o222 != 0 {
			return fmt.Errorf("%s is writable (%v)", p, info.Mode().Perm())
		}
		if uid >= 0 && ownedBy(info, uid) {
			return fmt.Errorf("%s is owned by uid %d, the harness's, which could make it writable", p, uid)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	integrity, _, err := persona.CheckTree(root, Covers)
	if err != nil {
		return fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	if integrity != persona.IntegrityVerified {
		return fmt.Errorf("the rendered persona at %s has no content manifest", root)
	}
	return nil
}
