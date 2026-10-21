//go:build unix

package persona

import (
	"errors"
	"os"
	"syscall"
)

// openNoFollow opens a file for reading without following a symlink in its
// last component, so a file swapped for a link after the directory was
// listed is refused rather than read.
func openNoFollow(name string) (*os.File, error) {
	f, err := os.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if errors.Is(err, syscall.ELOOP) {
		return nil, errSymlink
	}
	return f, err
}
