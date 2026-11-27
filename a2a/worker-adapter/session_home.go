package workeradapter

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// CheckSessionHome compares the session pod's shipped persona and skills in
// dir, the active harness's persona directory, with the manifest beside them.
// covers says which paths under dir are the persona's (for Claude Code,
// lib.SessionHomeCovers: its own state sits beside them and isn't checked).
// A difference is an error, and the worker stops before the harness runs. A
// directory without a manifest (an image built before it, a harness whose
// persona nothing writes yet, or a run by hand) is logged as unverified and
// passes, and so is a directory that isn't there, which is logged as missing.
//
// This catches a bad build or a corrupted layer. The manifest ships in the
// same image as the files it describes, so it does not detect a replaced or
// altered image. For Claude Code the harness owns the directory these files
// sit in, so it is not a boundary against the harness either.
func CheckSessionHome(dir string, covers func(rel string) bool, log *slog.Logger) error {
	if _, err := os.Stat(dir); errors.Is(err, fs.ErrNotExist) {
		log.Warn("session skills integrity unverified: the persona directory is missing", "dir", dir)
		return nil
	}
	integrity, files, err := persona.CheckTree(dir, covers)
	if err != nil {
		return fmt.Errorf("the session's shipped persona and skills at %s: %w", dir, err)
	}
	if integrity == persona.IntegrityVerified {
		log.Info("session skills match their build-time manifest", "dir", dir, "files", files)
		return nil
	}
	log.Warn("session skills integrity unverified: no build-time manifest", "dir", dir, "manifest", persona.IntegrityManifestFile)
	return nil
}
