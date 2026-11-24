package workeradapter

import (
	"fmt"
	"log/slog"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// CheckSessionHome compares the session pod's shipped persona and skills in
// the harness's config directory (lib.SessionHomeCovers) with the build-time
// manifest cmd/session-persona writes beside them. A difference is an error,
// and the worker stops before the harness runs. A directory without a
// manifest (an image built before it, or a run by hand) is logged as
// unverified and passes.
//
// This catches a bad build or a corrupted layer. The manifest ships in the
// same image as the files it describes, so it does not detect a replaced or
// altered image, and the harness owns the directory these files sit in, so
// it is not a boundary against the harness either.
func CheckSessionHome(dir string, log *slog.Logger) error {
	integrity, files, err := persona.CheckTree(dir, lib.SessionHomeCovers)
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
