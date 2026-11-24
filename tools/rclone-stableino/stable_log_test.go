package fs

import (
	"strings"
	"testing"
)

// Vectors computed with the logic stable_ino.go had before StableID was
// extracted (FNV-64a, top bit cleared, 0 -> 1). They must never change: they
// are the keys of Windows' thumbnail cache. The same table is in the bridge's
// tests/test_fileid.py.
var stableVectors = map[string]uint64{
	"":                       0x4bf29ce484222325,
	"a.txt":                  0x7ed582b5571bbd5a,
	"Hololive/a/00000.m2ts":  0x53fbff8f92acd838,
	"日本語/名前.jpg":             0x152dc4f5a7f60e1c,
	"with space/x y.png":     0x6ea1f2f292ac0cfa,
	"game/a.zip/inner/f.bin": 0x10166415923efbdf,
	"name：full.jpg":          0x72bd39366a4a7069,
	"trail .txt":             0x76acee19a2e3427f,
	"e\u0301.jpg":            0x70ad05dabfa61ef2,
}

func TestStableIDMatchesVectors(t *testing.T) {
	for path, want := range stableVectors {
		if got := StableID(path); got != want {
			t.Errorf("StableID(%q) = %#x, want %#x", path, got, want)
		}
	}
}

func TestStableLogObjectBecomesID(t *testing.T) {
	if got, want := stableLogObject("日本語/名前.jpg"), "id=0x152dc4f5a7f60e1c"; got != want {
		t.Errorf("object = %q, want %q", got, want)
	}
	if got := stableLogObject("/a.txt"); got != "id=0x7ed582b5571bbd5a" {
		t.Errorf("leading slash not stripped: %q", got)
	}
}

// Stand-ins for what rclone logs: a vfs Node (Path()), an fs.Object
// (Remote()), a directory whose String() adds "/", and a file handle whose
// String() adds " (r)". All of them must hash their VFS path.
type nodeLike struct{ p string }

func (n nodeLike) Path() string   { return n.p }
func (n nodeLike) String() string { return n.p + "/" }

type objectLike struct{ p string }

func (o objectLike) Remote() string { return o.p }

type handleLike struct{ p string }

func (h handleLike) String() string { return h.p + " (r)" }

type rootLike struct{}

func (rootLike) String() string { return "/" }

func TestStableLogObjectIsThePathNotTheString(t *testing.T) {
	want := "id=0x152dc4f5a7f60e1c" // 日本語/名前.jpg
	for name, o := range map[string]any{
		"node":   nodeLike{"日本語/名前.jpg"},
		"object": objectLike{"日本語/名前.jpg"},
		"handle": handleLike{"日本語/名前.jpg"},
	} {
		if got := stableLogObject(o); got != want {
			t.Errorf("%s: %q, want %q", name, got, want)
		}
	}
	if got, want := stableLogObject(nodeLike{"a.txt"}), "id=0x7ed582b5571bbd5a"; got != want {
		t.Errorf("dir-style String() leaked into the hash: %q", got)
	}
	if got, want := stableLogObject(rootLike{}), "id=0x4bf29ce484222325"; got != want {
		t.Errorf("root: %q, want %q", got, want)
	}
}

func TestStableLogTextHandlesSpacesInCachePath(t *testing.T) {
	in := `open D:\my data\rclone\vfs\teledrive\with space\x y.png: boom`
	out := stableLogText(in)
	if !strings.Contains(out, "id=0x6ea1f2f292ac0cfa") || strings.Contains(out, "space") || strings.Contains(out, "x y") {
		t.Errorf("spaces not handled: %q", out)
	}
	if !strings.HasSuffix(out, ": boom") {
		t.Errorf("sentence after the path lost: %q", out)
	}
}

func TestStableLogTextRewritesBridgeURL(t *testing.T) {
	in := `Get "http://127.0.0.1:8081/%E6%97%A5%E6%9C%AC%E8%AA%9E/%E5%90%8D%E5%89%8D.jpg": EOF`
	out := stableLogText(in)
	if !strings.Contains(out, "id=0x152dc4f5a7f60e1c") || strings.Contains(out, "%E6") || strings.Contains(out, "日") {
		t.Errorf("url not rewritten: %q", out)
	}
	if !strings.HasSuffix(out, `": EOF`) {
		t.Errorf("surrounding text lost: %q", out)
	}
	if stableLogText("http://127.0.0.1:8081/") != "http://127.0.0.1:8081/" {
		t.Errorf("the bare root must be left alone")
	}
}

func TestStableLogTextRewritesVFSCachePath(t *testing.T) {
	in := `vfs cache: failed to open C:\Users\u\AppData\Local\rclone\vfs\teledrive\日本語\名前.jpg: boom`
	out := stableLogText(in)
	if !strings.Contains(out, "id=0x152dc4f5a7f60e1c") || strings.Contains(out, "日") {
		t.Errorf("cache path not rewritten: %q", out)
	}
	meta := stableLogText(`C:\x\vfsMeta\teledrive\a.txt`)
	if !strings.Contains(meta, "id=0x7ed582b5571bbd5a") {
		t.Errorf("vfsMeta path not rewritten: %q", meta)
	}
}
