// Which half of the shell's per-file work reads the file?
//
// bench.exe does both a thumbnail extraction and a property read, and its split
// timing blames GetImage — but the two run back to back per file, so lazy work
// from one lands in the other's measurement. This does exactly one of them.
//
//   isolate thumb  <folder> [count] [px]
//   isolate props  <folder> [count]

#include <windows.h>
#include <shlwapi.h>
#include <shobjidl.h>
#include <propsys.h>
#include <propkey.h>
#include <propvarutil.h>
#include <cstdio>
#include <string>
#include <vector>

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "propsys.lib")
#pragma comment(lib, "gdi32.lib")

static double Now(LARGE_INTEGER freq) {
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return double(t.QuadPart) / freq.QuadPart;
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 3) {
        wprintf(L"usage: isolate <thumb|props> <folder> [count] [px]\n");
        return 1;
    }
    const std::wstring mode = argv[1];
    const std::wstring folder = argv[2];
    const int want = argc >= 4 ? _wtoi(argv[3]) : 8;
    const int px = argc >= 5 ? _wtoi(argv[4]) : 256;

    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);

    std::vector<std::wstring> files;
    WIN32_FIND_DATAW found;
    HANDLE handle = FindFirstFileW((folder + L"\\*").c_str(), &found);
    if (handle != INVALID_HANDLE_VALUE) {
        do {
            if (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
                continue;
            files.push_back(folder + L"\\" + found.cFileName);
            if (static_cast<int>(files.size()) >= want)
                break;
        } while (FindNextFileW(handle, &found));
        FindClose(handle);
    }
    if (files.empty()) {
        wprintf(L"no files in %s\n", folder.c_str());
        return 2;
    }

    int got = 0;
    const double start = Now(freq);
    for (const std::wstring& path : files) {
        if (mode == L"thumb") {
            IShellItemImageFactory* factory = nullptr;
            if (SUCCEEDED(SHCreateItemFromParsingName(path.c_str(), nullptr, IID_PPV_ARGS(&factory)))) {
                SIZE size = { px, px };
                HBITMAP bitmap = nullptr;
                if (SUCCEEDED(factory->GetImage(size, SIIGBF_THUMBNAILONLY, &bitmap)) && bitmap) {
                    ++got;
                    DeleteObject(bitmap);
                }
                factory->Release();
            }
        } else {
            IPropertyStore* store = nullptr;
            if (SUCCEEDED(SHGetPropertyStoreFromParsingName(path.c_str(), nullptr, GPS_DEFAULT,
                                                            IID_PPV_ARGS(&store)))) {
                PROPVARIANT value;
                PropVariantInit(&value);
                if (SUCCEEDED(store->GetValue(PKEY_Image_Dimensions, &value)) && value.vt != VT_EMPTY)
                    ++got;
                PropVariantClear(&value);
                store->Release();
            }
        }
    }
    const double elapsed = Now(freq) - start;
    wprintf(L"%s: %zu files in %.2fs  =>  %.1f per second   (%d answered)\n",
            mode.c_str(), files.size(), elapsed, files.size() / elapsed, got);
    CoUninitialize();
    return 0;
}
