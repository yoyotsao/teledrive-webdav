package fs

// TeleDrive patch: rclone's log names files by their stable ID, not their path.
//
// A drive's file names and folder layout say what its owner keeps there, and a
// log is the thing most likely to be pasted somewhere. Every log call that
// carries an object goes through logSlogWithObject; this makes it print
// "id=0x..." (the same number WinFsp/Windows and the bridge's own log use,
// see stable_id.go) for the object, and rewrites the two shapes of path that
// rclone also writes into message text:
//
//   - the bridge URL:  http://127.0.0.1:<port>/<escaped path>
//   - the VFS cache:   <cache-dir>\vfs\<remote>\<rel>  (and vfsMeta)
//
// Best effort: a message that formats a path some other way is not caught.
// rclone is pinned, so that only changes when someone upgrades it on purpose.

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

var (
	stableURLRe = regexp.MustCompile(`http://127\.0\.0\.1:\d+/([^\s"'?]*)`)
	// The cache directory and the file name may both contain spaces, so the
	// relative path runs to the end of the line (or a quote); stableLogText
	// cuts it at the first ": " where the sentence resumes.
	stableCacheRe = regexp.MustCompile(`(?i)(?:\\\\\?\\)?[A-Za-z]:[\\/][^"'\r\n]*?[\\/]vfs(?:Meta)?[\\/][^\\/\r\n"']+[\\/]([^"'\r\n]+)`)
)

func stableTag(vfsPath string) string {
	return fmt.Sprintf("id=0x%016x", StableID(vfsPath))
}

// stableLogObject is what the "object" attribute holds. An Fs is a remote name
// ("teledrive:"), not a file, and is kept; anything else is a file or folder
// path of some kind and becomes its ID.
func stableLogObject(o any) string {
	text := fmt.Sprintf("%+v", o)
	if _, isFs := o.(Fs); isFs {
		return text
	}
	return stableTag(stableObjectPath(o, text))
}

// stableObjectPath is the VFS path of a logged object. String() is not always
// that path: *vfs.Dir adds a trailing "/" and the file handles add " (r)" /
// " (rw)", and hashing those would give an ID that matches neither the inode
// WinFsp reports nor the bridge's own log. Ask the object for its path first
// (vfs Nodes have Path(), fs.Object and fs.Directory have Remote()); only when
// it has neither, clean up its String().
func stableObjectPath(o any, text string) string {
	switch v := o.(type) {
	case interface{ Path() string }:
		return v.Path()
	case interface{ Remote() string }:
		return v.Remote()
	}
	for _, suffix := range []string{" (rw)", " (r)", " (w)"} {
		text = strings.TrimSuffix(text, suffix)
	}
	text = strings.TrimSuffix(text, "/")
	return strings.TrimPrefix(text, "/")
}

// stableLogText rewrites the known path shapes inside a message.
func stableLogText(text string) string {
	text = stableURLRe.ReplaceAllStringFunc(text, func(match string) string {
		sub := stableURLRe.FindStringSubmatch(match)
		escaped := sub[1]
		if escaped == "" {
			return match
		}
		path, err := url.PathUnescape(escaped)
		if err != nil {
			path = escaped
		}
		return "http://127.0.0.1/" + stableTag(strings.TrimPrefix(path, "/"))
	})
	return stableCacheRe.ReplaceAllStringFunc(text, func(match string) string {
		sub := stableCacheRe.FindStringSubmatch(match)
		// "...\name.jpg: boom" -- the sentence resumes at the first ": ".
		rel, rest := sub[1], ""
		if i := strings.Index(rel, ": "); i >= 0 {
			rel, rest = rel[:i], rel[i:]
		}
		trimmed := strings.TrimRight(rel, ":,;")
		rest = rel[len(trimmed):] + rest
		return stableTag(strings.ReplaceAll(trimmed, `\`, "/")) + rest
	})
}
