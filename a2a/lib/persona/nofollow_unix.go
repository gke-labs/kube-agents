//go:build unix

package persona

import (
	"errors"
	"os"
	"syscall"
)

// openNoFollow opens a file for reading without following a symlink in its
// last component, so a file swapped for a link after the directory was
// listed is refused rather than read. O_NONBLOCK keeps a named pipe from
// blocking the open until a writer appears; readFile then refuses it as not
// a regular file.
func openNoFollow(name string) (*os.File, error) {
	f, err := os.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if errors.Is(err, syscall.ELOOP) {
		return nil, errSymlink
	}
	return f, err
}
