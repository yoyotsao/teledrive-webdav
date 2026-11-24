//go:build cmount && ((linux && cgo) || (darwin && cgo) || (freebsd && cgo) || (openbsd && cgo) || windows)

package cmount

// TeleDrive patch: stable inode numbers.
//
// Upstream hands the VFS's in-memory counter to the OS as st_ino. On Windows,
// WinFsp turns st_ino into the file's IndexNumber (its "file ID"), and the
// shell's thumbnail cache (thumbcache_*.db) keys entries on that ID. The counter
// restarts on every mount (and on `rc vfs/forget`), so every thumbnail Windows
// ever cached for the mount is orphaned by a remount.
//
// Hashing the path gives the same ID for the same file on every mount. A rename
// changes the ID, which is the behaviour a renamed file should get anyway.
//
// The hash itself is fs.StableID (see fs/stable_id.go): the log patch needs it
// too, and package fs cannot import cmount.

import (
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/vfs"
)

func stableIno(node vfs.Node) uint64 {
	return fs.StableID(node.Path())
}
