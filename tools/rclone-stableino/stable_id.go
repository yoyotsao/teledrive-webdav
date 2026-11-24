package fs

// TeleDrive patch: the one definition of a file's stable ID.
//
// FNV-64a of the rclone VFS path (relative to the mount root, "/"-separated, no
// leading "/", the root is ""), top bit cleared, 0 mapped to 1. cmd/cmount
// hands this to WinFsp as the inode (see stable_ino.go), so Windows' thumbnail
// cache keys on it; the log patch (stable_log.go) prints the same number, and
// the bridge's fileid.py computes it too. Changing the value orphans every
// thumbnail Windows ever cached for the mount.
//
// It lives in package fs, not cmount, because fs/log.go needs it and cmount
// already imports fs: the other direction would be an import cycle.

import "hash/fnv"

// StableID returns the stable file ID for an rclone VFS path.
func StableID(path string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(path))
	// Keep the top bit clear: the VFS uses inode^(1<<63) for metadata files.
	id := h.Sum64() &^ (1 << 63)
	if id == 0 {
		id = 1
	}
	return id
}
