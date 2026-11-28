//go:build !unix

package coreagent

import "io/fs"

func ownedBy(fs.FileInfo, int) bool { return false }
