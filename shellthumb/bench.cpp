// How many files per second can the shell actually render?
//
// Does what Explorer does for each file in a folder: extract a thumbnail and
// read the image dimensions. Both go through the registered handlers, so this
// measures the delivered rate rather than either half in isolation.
//
// SIIGBF_THUMBNAILONLY matters — without it the shell may return a file-type
// icon without touching the file, which would flatter the result.

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
    if (argc < 2) {
        wprintf(L"usage: bench <folder> [count] [px]\n");
        return 1;
    }
    const std::wstring folder = argv[1];
    const int want = argc >= 3 ? _wtoi(argv[2]) : 20;
    const int px = argc >= 4 ? _wtoi(argv[3]) : 256;

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

    int thumbs = 0, dims = 0;
    // Binding a shell item to a path is its own cost on a network mount, and it
    // is paid twice per file. Timed separately so it cannot hide inside the
    // handlers' numbers.
    double bindTime = 0, thumbTime = 0, propTime = 0;
    const double start = Now(freq);
    for (const std::wstring& path : files) {
        IShellItemImageFactory* factory = nullptr;
        double mark = Now(freq);
        HRESULT bound = SHCreateItemFromParsingName(path.c_str(), nullptr, IID_PPV_ARGS(&factory));
        bindTime += Now(freq) - mark;
        if (SUCCEEDED(bound)) {
            SIZE size = { px, px };
            HBITMAP bitmap = nullptr;
            mark = Now(freq);
            HRESULT got = factory->GetImage(size, SIIGBF_THUMBNAILONLY, &bitmap);
            thumbTime += Now(freq) - mark;
            if (SUCCEEDED(got) && bitmap) {
                ++thumbs;
                DeleteObject(bitmap);
            }
            factory->Release();
        }
        IPropertyStore* store = nullptr;
        mark = Now(freq);
        HRESULT opened = SHGetPropertyStoreFromParsingName(path.c_str(), nullptr, GPS_DEFAULT,
                                                           IID_PPV_ARGS(&store));
        propTime += Now(freq) - mark;
        if (SUCCEEDED(opened)) {
            PROPVARIANT value;
            PropVariantInit(&value);
            if (SUCCEEDED(store->GetValue(PKEY_Image_Dimensions, &value)) && value.vt != VT_EMPTY)
                ++dims;
            PropVariantClear(&value);
            store->Release();
        }
    }
    const double elapsed = Now(freq) - start;

    wprintf(L"%zu files in %.2fs  =>  %.1f per second\n", files.size(), elapsed,
            files.size() / elapsed);
    wprintf(L"  thumbnails: %d/%zu   dimensions: %d/%zu\n", thumbs, files.size(), dims, files.size());
    wprintf(L"  bind %.2fs   GetImage %.2fs   property store %.2fs   rest %.2fs\n",
            bindTime, thumbTime, propTime, elapsed - bindTime - thumbTime - propTime);
    CoUninitialize();
    return 0;
}
