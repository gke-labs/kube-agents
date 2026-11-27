//go:build unix

package coreagent

import (
	"io/fs"
	"syscall"
)

func ownedBy(info fs.FileInfo, uid int) bool {
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(st.Uid) == uid
}
