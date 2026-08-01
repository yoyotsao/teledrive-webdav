// Ask the shell for a file's image dimensions and say where the answer came from.
//
// This is the read Explorer does between thumbnails — measured on the mount, it
// pulled 258 KB of a 2 MB JPEG per file and left 5.2 second gaps. The property
// handler exists to answer it from Telegram's metadata instead. This probe times
// that path so the claim can be checked rather than assumed.

#include <windows.h>
#include <shlwapi.h>
#include <shobjidl.h>
#include <propsys.h>
#include <propkey.h>
#include <propvarutil.h>
#include <cstdio>

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "propsys.lib")

static void Show(IPropertyStore* store, REFPROPERTYKEY key, const wchar_t* label) {
    PROPVARIANT value;
    PropVariantInit(&value);
    if (SUCCEEDED(store->GetValue(key, &value)) && value.vt != VT_EMPTY) {
        wchar_t text[256] = L"";
        PropVariantToString(value, text, _countof(text));
        wprintf(L"  %s = %s\n", label, text);
    } else {
        wprintf(L"  %s = (absent)\n", label);
    }
    PropVariantClear(&value);
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 2) {
        wprintf(L"usage: propprobe <file>\n");
        return 1;
    }
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    LARGE_INTEGER freq, t0, t1;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);

    IPropertyStore* store = nullptr;
    // GPS_DEFAULT goes through the registered property handler, which is the
    // thing under test. GPS_FASTPROPERTIESONLY would skip it.
    HRESULT hr = SHGetPropertyStoreFromParsingName(argv[1], nullptr, GPS_DEFAULT,
                                                   IID_PPV_ARGS(&store));
    QueryPerformanceCounter(&t1);
    const double elapsed = double(t1.QuadPart - t0.QuadPart) / freq.QuadPart;

    if (FAILED(hr)) {
        wprintf(L"failed hr=0x%08X  %.3fs\n", hr, elapsed);
        CoUninitialize();
        return 2;
    }
    wprintf(L"opened in %.3fs\n", elapsed);
    Show(store, PKEY_Image_Dimensions, L"Dimensions");
    Show(store, PKEY_Image_HorizontalSize, L"Width");
    Show(store, PKEY_Image_VerticalSize, L"Height");
    store->Release();
    CoUninitialize();
    return 0;
}
