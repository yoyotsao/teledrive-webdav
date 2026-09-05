// Warm Windows' own thumbnail cache, by doing what Explorer does.
//
// Filling the bridge's caches only gets a cold folder to about 3 thumbnails a
// second, because the shell still has work to do per file whatever we answer.
// A folder that has been looked at once renders at 266 a second and never even
// reaches the handler — the difference is thumbcache_*.db, which nothing on the
// Python side can write. The only way in is to ask the shell for the thumbnail
// the same way Explorer does and let it cache the result.
//
// Reads UTF-8 paths from stdin, one per line, so the caller decides which files
// are worth warming and can pause between batches. Prints how many produced a
// thumbnail.
//
//   warmshell [threads] [px]
//
// SIIGBF_THUMBNAILONLY matters: without it the shell may hand back a file-type
// icon without ever consulting a provider, which caches nothing useful.
//
// stdout carries the count, once, at the end. Every file is also reported on
// stderr as it finishes ("+ <ms> <path>" warmed, "- <ms> <path>" not), flushed
// per line, because the interesting runs are the ones that never reach the end:
// the caller kills a batch that overruns its budget, and without the per-file
// lines a killed batch says nothing at all — not how many it warmed, not which
// path it was still holding. That is exactly the state this warm-up was in for
// weeks (every batch killed at 600s, "0 warmed", no idea why).

#include <windows.h>
#include <shlwapi.h>
#include <shobjidl.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "gdi32.lib")

static std::wstring Widen(const std::string& utf8) {
    if (utf8.empty())
        return std::wstring();
    int need = MultiByteToWideChar(CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), nullptr, 0);
    std::wstring out(need, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), &out[0], need);
    return out;
}

static std::string Narrow(const std::wstring& wide) {
    if (wide.empty())
        return std::string();
    int need = WideCharToMultiByte(CP_UTF8, 0, wide.data(), static_cast<int>(wide.size()), nullptr, 0, nullptr, nullptr);
    std::string out(need, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wide.data(), static_cast<int>(wide.size()), &out[0], need, nullptr, nullptr);
    return out;
}

// UTF-8 through the narrow stderr, not wide: fwprintf converts to the console
// codepage on the way out, which mangles every non-ASCII path — and those are
// most of them here. The reader decodes UTF-8.
static void Report(bool ok, long long ms, const std::wstring& path) {
    static std::mutex lock;
    std::lock_guard<std::mutex> guard(lock);
    fprintf(stderr, "%c %lld %s\n", ok ? '+' : '-', ms, Narrow(path).c_str());
    fflush(stderr);
}

static bool WarmOne(const std::wstring& path, int px) {
    IShellItemImageFactory* factory = nullptr;
    if (FAILED(SHCreateItemFromParsingName(path.c_str(), nullptr, IID_PPV_ARGS(&factory))))
        return false;
    SIZE size = { px, px };
    HBITMAP bitmap = nullptr;
    const HRESULT hr = factory->GetImage(size, SIIGBF_THUMBNAILONLY, &bitmap);
    factory->Release();
    if (FAILED(hr) || !bitmap)
        return false;
    DeleteObject(bitmap);
    return true;
}

int wmain(int argc, wchar_t** argv) {
    const int threads = argc >= 2 ? max(1, _wtoi(argv[1])) : 4;
    const int px = argc >= 3 ? _wtoi(argv[2]) : 256;

    std::vector<std::wstring> files;
    std::string line;
    for (int c = fgetc(stdin); ; c = fgetc(stdin)) {
        if (c == EOF || c == '\n') {
            while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
                line.pop_back();
            if (!line.empty())
                files.push_back(Widen(line));
            line.clear();
            if (c == EOF)
                break;
            continue;
        }
        line.push_back(static_cast<char>(c));
    }
    if (files.empty()) {
        wprintf(L"0\n");
        return 0;
    }

    std::atomic<size_t> next(0);
    std::atomic<int> warmed(0);
    std::vector<std::thread> pool;
    for (int i = 0; i < threads; ++i) {
        pool.emplace_back([&files, &next, &warmed, px]() {
            // One apartment per thread: the thumbnail handler is registered
            // ThreadingModel=Apartment, and giving each worker its own STA is
            // what lets them run at the same time instead of queueing on one.
            CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
            for (;;) {
                const size_t at = next.fetch_add(1);
                if (at >= files.size())
                    break;
                const auto started = std::chrono::steady_clock::now();
                const bool ok = WarmOne(files[at], px);
                const long long ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - started).count();
                Report(ok, ms, files[at]);
                if (ok)
                    warmed.fetch_add(1);
            }
            CoUninitialize();
        });
    }
    for (std::thread& t : pool)
        t.join();

    wprintf(L"%d\n", warmed.load());
    return 0;
}
