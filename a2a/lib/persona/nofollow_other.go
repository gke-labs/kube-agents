//go:build !unix

package persona

import "os"

// openNoFollow is Lstat then open where O_NOFOLLOW does not exist. The
// window between the two is the unix build's reason for the flag.
func openNoFollow(name string) (*os.File, error) {
	info, err := os.Lstat(name)
	if err != nil {
		return nil, err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return nil, errSymlink
	}
	return os.Open(name)
}
