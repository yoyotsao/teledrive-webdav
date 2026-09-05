// Shell thumbnail handler for the TeleDrive mount.
//
// Explorer builds a thumbnail by reading the file: measured here, one 256px
// preview of an 18.6 MB PNG on the mount read all 18,629,212 bytes and took
// 18.8 seconds. Nothing about the file can prevent that — these images carry no
// embedded preview (the JPEGs have JFIF + ICC but no APP1/Exif, and PNG has no
// such thing at all), and Windows offers no way to point a file at a different
// image. A thumbnail provider is the one supported place to intervene: Explorer
// calls it *instead* of decoding the file, so we hand back the small preview
// Telegram already stores beside every photo and video, fetched from the bridge.
//
// Registration is per-extension and therefore machine-wide in effect, so this
// must be a good citizen for files it does not own: anything outside the mount
// is forwarded to the handler that was registered before us, which the installer
// recorded. Failing instead would strip thumbnails from every JPEG on the
// machine.

#include <windows.h>
#include <shlwapi.h>
#include <thumbcache.h>
#include <shobjidl.h>   // IInitializeWithItem, SHCreateItemFromParsingName
#include <wincodec.h>
#include <winhttp.h>
#include <propkey.h>
#include <new>
#include <string>
#include <cstdarg>
#include <cstdio>

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "ole32.lib")      // CoCreateInstance, CLSIDFromString
#pragma comment(lib, "advapi32.lib")   // Reg*
#pragma comment(lib, "user32.lib")     // CharUpperW
#pragma comment(lib, "gdi32.lib")      // CreateDIBSection, DeleteObject
#pragma comment(lib, "shell32.lib")    // SHCreateItemFromParsingName

// {7A3F1C28-9B6D-4E51-8F42-C0D3E5A91B74}
static const CLSID CLSID_TeleDriveThumb =
    { 0x7a3f1c28, 0x9b6d, 0x4e51, { 0x8f, 0x42, 0xc0, 0xd3, 0xe5, 0xa9, 0x1b, 0x74 } };

const wchar_t* kSettingsKey = L"Software\\TeleDriveWebDAV";

static HINSTANCE g_instance = nullptr;
LONG g_objects = 0;
static LONG g_locks = 0;

// --------------------------------------------------------------------------
// Settings, read from the key the installer writes
// --------------------------------------------------------------------------

struct Settings {
    // Either a drive (L"H:") or a folder (L"D:\TeleDrive"). The Cloud Filter
    // architecture mounts into a folder, so a bare drive-letter test would
    // intercept everything else on that drive too.
    std::wstring root;
    DWORD port = 8081;
};

// Settings live under HKCU for the thumbnail handler, but the property handler
// can be loaded by services running as another user — the search indexer, most
// obviously — where HKCU is not this user's. Falling back to HKLM keeps those
// callers working instead of silently answering with nothing.
static std::wstring ReadSetting(const wchar_t* subkey, const wchar_t* name);

static std::wstring ReadString(HKEY key, const wchar_t* name) {
    wchar_t buf[512];
    DWORD size = sizeof(buf);
    DWORD type = 0;
    if (RegQueryValueExW(key, name, nullptr, &type, reinterpret_cast<BYTE*>(buf), &size) != ERROR_SUCCESS)
        return std::wstring();
    if (type != REG_SZ)
        return std::wstring();
    buf[min(size / sizeof(wchar_t), _countof(buf) - 1)] = L'\0';
    return std::wstring(buf);
}

static Settings LoadSettings() {
    Settings settings;
    settings.root = ReadSetting(nullptr, L"MountRoot");
    if (settings.root.empty())
        settings.root = ReadSetting(nullptr, L"MountDrive");
    for (HKEY root : { HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE }) {
        HKEY key = nullptr;
        if (RegOpenKeyExW(root, kSettingsKey, 0, KEY_READ, &key) != ERROR_SUCCESS)
            continue;
        DWORD port = 0, size = sizeof(port), type = 0;
        LONG rc = RegQueryValueExW(key, L"Port", nullptr, &type, reinterpret_cast<BYTE*>(&port), &size);
        RegCloseKey(key);
        if (rc == ERROR_SUCCESS && type == REG_DWORD && port) {
            settings.port = port;
            break;
        }
    }
    if (!settings.root.empty())
        CharUpperW(&settings.root[0]);
    return settings;
}

static const Settings& GetSettings() {
    // Read once per host process: the surrogate is short-lived, and a thumbnail
    // must not pay a registry round trip per file.
    //
    // The one-time init has to be the *compiler's* (C++11 magic static), not a
    // hand-rolled flag. The flag this replaced set itself before the registry
    // reads, so the sibling threads Explorer starts for one folder took the
    // early return and got root="" -> OnMount() said false -> those files went
    // to the built-in handler, which reads the whole file. Measured on a fresh
    // surrogate: of six files handed to warmshell at once, three were delegated
    // (one logged onMount=0, two "preview fetch failed"), and the batch took
    // 9.4s instead of the ~0.3s the three that won the race needed.
    static const Settings settings = LoadSettings();
    return settings;
}

// The handler we displaced, per extension. Images and videos land on different
// providers ({C7657C4A-...} and {9DBD2C50-...} on a stock machine), so one
// global fallback would hand video files to the image decoder.
std::wstring FallbackFor(const std::wstring& path, const wchar_t* subkey) {
    size_t dot = path.rfind(L'.');
    if (dot == std::wstring::npos)
        return std::wstring();
    std::wstring ext = path.substr(dot);
    CharLowerW(&ext[0]);
    return ReadSetting(subkey, ext.c_str());
}

static std::wstring ReadSetting(const wchar_t* subkey, const wchar_t* name) {
    std::wstring path = kSettingsKey;
    if (subkey) {
        path += L"\\";
        path += subkey;
    }
    for (HKEY root : { HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE }) {
        HKEY key = nullptr;
        if (RegOpenKeyExW(root, path.c_str(), 0, KEY_READ, &key) != ERROR_SUCCESS)
            continue;
        std::wstring value = ReadString(key, name);
        RegCloseKey(key);
        if (!value.empty())
            return value;
    }
    return std::wstring();
}

// Diagnostics. Off unless HKCU\Software\TeleDriveWebDAV\LogPath is set, because
// the only way to tell "the shell never called us" from "we were called and fell
// back" is to have the handler say so itself.
void Log(const wchar_t* format, ...) {
    static std::wstring path;
    static bool checked = false;
    if (!checked) {
        checked = true;
        HKEY key = nullptr;
        if (RegOpenKeyExW(HKEY_CURRENT_USER, kSettingsKey, 0, KEY_READ, &key) == ERROR_SUCCESS) {
            path = ReadString(key, L"LogPath");
            RegCloseKey(key);
        }
        if (path.empty())
            path = ReadSetting(nullptr, L"LogPath");
    }
    if (path.empty())
        return;

    wchar_t body[1024];
    va_list args;
    va_start(args, format);
    _vsnwprintf_s(body, _countof(body), _TRUNCATE, format, args);
    va_end(args);

    // Wall clock plus milliseconds since the DLL loaded: the question these logs
    // exist to answer is where the seconds go, and "which step" needs the gap
    // between lines, not just their order.
    static const ULONGLONG start = GetTickCount64();
    SYSTEMTIME now;
    GetLocalTime(&now);
    wchar_t line[1200];
    _snwprintf_s(line, _countof(line), _TRUNCATE, L"%02d:%02d:%02d.%03d +%llums  %s",
                 now.wHour, now.wMinute, now.wSecond, now.wMilliseconds,
                 GetTickCount64() - start, body);

    HANDLE file = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE)
        return;
    std::string utf8;
    int need = WideCharToMultiByte(CP_UTF8, 0, line, -1, nullptr, 0, nullptr, nullptr);
    if (need > 1) {
        utf8.resize(need - 1);
        WideCharToMultiByte(CP_UTF8, 0, line, -1, &utf8[0], need, nullptr, nullptr);
        utf8 += "\r\n";
        DWORD written = 0;
        WriteFile(file, utf8.data(), static_cast<DWORD>(utf8.size()), &written, nullptr);
    }
    CloseHandle(file);
}

// Named rather than written inline: a lone backslash literal is the one thing
// every layer between here and the file on disk wants to eat.
static const wchar_t kSep = L'\\';

bool OnMount(const std::wstring& path) {
    const Settings& s = GetSettings();
    if (s.root.size() < 2 || path.size() < s.root.size())
        return false;
    // Case-insensitive prefix, and the next character must be a separator so
    // "D:\TeleDriveOther" does not match "D:\TeleDrive".
    if (CompareStringOrdinal(path.c_str(), static_cast<int>(s.root.size()),
                             s.root.c_str(), static_cast<int>(s.root.size()),
                             TRUE) != CSTR_EQUAL)
        return false;
    return path.size() == s.root.size() || path[s.root.size()] == kSep
           || s.root[s.root.size() - 1] == L':';
}

// The path the bridge wants, relative to the mount root.
std::wstring RelativeToRoot(const std::wstring& path) {
    const Settings& s = GetSettings();
    if (path.size() <= s.root.size())
        return std::wstring();
    std::wstring rest = path.substr(s.root.size());
    while (!rest.empty() && rest[0] == kSep)
        rest.erase(0, 1);
    for (wchar_t& c : rest)
        if (c == kSep)
            c = L'/';
    return rest;
}

// --------------------------------------------------------------------------
// Fetching the preview from the bridge
// --------------------------------------------------------------------------

static std::wstring UrlEscape(const std::wstring& text) {
    // Percent-encode everything outside the unreserved set. Paths here are full
    // of spaces and non-ASCII, and the query string has to survive both.
    static const wchar_t* kHex = L"0123456789ABCDEF";
    std::string utf8;
    int need = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (need <= 0)
        return std::wstring();
    utf8.resize(need - 1);
    WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, &utf8[0], need, nullptr, nullptr);

    std::wstring out;
    for (unsigned char c : utf8) {
        // Spelled out rather than iswalnum(): that takes a wide character, so a
        // UTF-8 continuation byte arrives as the codepoint of the same number.
        // 0xE6 became U+00E6 'æ', which is a letter, so it was emitted raw while
        // its neighbours were escaped — "湊あくあ" went out as "æ¹%8Aã%81%82…"
        // and the bridge could not resolve it. Every path with a non-ASCII
        // character therefore 404'd and fell back to reading the whole file.
        // Ranges, not isalnum() either: the CRT's locale is whatever the host
        // process left it as, and this must not depend on that.
        const bool unreserved =
            (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
        if (unreserved || c == '-' || c == '_' || c == '.' || c == '~') {
            out.push_back(static_cast<wchar_t>(c));
        } else {
            out.push_back(L'%');
            out.push_back(kHex[c >> 4]);
            out.push_back(kHex[c & 0xF]);
        }
    }
    return out;
}

bool FetchFromBridge(const wchar_t* route, const std::wstring& path, std::string* out) {
    const Settings& s = GetSettings();
    HINTERNET session = WinHttpOpen(L"TeleDriveThumb/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                                    WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session)
        return false;
    // Explorer must never hang on a thumbnail: if the bridge is down or slow we
    // want a fast failure and the normal fallback, not a frozen folder.
    WinHttpSetTimeouts(session, 2000, 2000, 5000, 15000);

    bool ok = false;
    HINTERNET connect = WinHttpConnect(session, L"127.0.0.1", static_cast<INTERNET_PORT>(s.port), 0);
    if (connect) {
        std::wstring target = std::wstring(route) + L"?path=" + UrlEscape(path);
        HINTERNET request = WinHttpOpenRequest(connect, L"GET", target.c_str(), nullptr,
                                               WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        if (request) {
            if (WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0)
                && WinHttpReceiveResponse(request, nullptr)) {
                DWORD status = 0, size = sizeof(status);
                WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                    WINHTTP_HEADER_NAME_BY_INDEX, &status, &size, WINHTTP_NO_HEADER_INDEX);
                if (status == 200) {
                    DWORD available = 0;
                    while (WinHttpQueryDataAvailable(request, &available) && available) {
                        size_t at = out->size();
                        out->resize(at + available);
                        DWORD read = 0;
                        if (!WinHttpReadData(request, &(*out)[at], available, &read)) {
                            out->resize(at);
                            break;
                        }
                        out->resize(at + read);
                    }
                    ok = !out->empty();
                }
            }
            WinHttpCloseHandle(request);
        }
        WinHttpCloseHandle(connect);
    }
    WinHttpCloseHandle(session);
    return ok;
}

// --------------------------------------------------------------------------
// JPEG bytes -> HBITMAP, scaled to what Explorer asked for
// --------------------------------------------------------------------------

static HRESULT DecodeToBitmap(const std::string& jpeg, UINT requested, HBITMAP* out) {
    IWICImagingFactory* factory = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_PPV_ARGS(&factory));
    if (FAILED(hr))
        return hr;

    IWICStream* stream = nullptr;
    IWICBitmapDecoder* decoder = nullptr;
    IWICBitmapFrameDecode* frame = nullptr;
    IWICBitmapScaler* scaler = nullptr;
    IWICFormatConverter* converter = nullptr;

    hr = factory->CreateStream(&stream);
    if (SUCCEEDED(hr))
        hr = stream->InitializeFromMemory(reinterpret_cast<BYTE*>(const_cast<char*>(jpeg.data())),
                                          static_cast<DWORD>(jpeg.size()));
    if (SUCCEEDED(hr))
        hr = factory->CreateDecoderFromStream(stream, nullptr, WICDecodeMetadataCacheOnDemand, &decoder);
    if (SUCCEEDED(hr))
        hr = decoder->GetFrame(0, &frame);

    UINT width = 0, height = 0;
    if (SUCCEEDED(hr))
        hr = frame->GetSize(&width, &height);

    IWICBitmapSource* source = frame;
    if (SUCCEEDED(hr) && width && height) {
        // Fit inside the square Explorer asked for, keeping the aspect ratio.
        // Never upscale: a 200px preview blown up to 1024 looks worse than the
        // small image Explorer would letterbox itself.
        double scale = min(static_cast<double>(requested) / width, static_cast<double>(requested) / height);
        if (scale < 1.0) {
            UINT tw = max(1u, static_cast<UINT>(width * scale + 0.5));
            UINT th = max(1u, static_cast<UINT>(height * scale + 0.5));
            if (SUCCEEDED(factory->CreateBitmapScaler(&scaler))
                && SUCCEEDED(scaler->Initialize(frame, tw, th, WICBitmapInterpolationModeFant))) {
                source = scaler;
                width = tw;
                height = th;
            }
        }
    }

    if (SUCCEEDED(hr))
        hr = factory->CreateFormatConverter(&converter);
    if (SUCCEEDED(hr))
        hr = converter->Initialize(source, GUID_WICPixelFormat32bppBGRA, WICBitmapDitherTypeNone,
                                   nullptr, 0.0, WICBitmapPaletteTypeCustom);

    if (SUCCEEDED(hr)) {
        BITMAPINFO info = {};
        info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        info.bmiHeader.biWidth = static_cast<LONG>(width);
        info.bmiHeader.biHeight = -static_cast<LONG>(height);  // top-down
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;

        void* bits = nullptr;
        HBITMAP bitmap = CreateDIBSection(nullptr, &info, DIB_RGB_COLORS, &bits, nullptr, 0);
        if (!bitmap) {
            hr = E_OUTOFMEMORY;
        } else {
            const UINT stride = width * 4;
            hr = converter->CopyPixels(nullptr, stride, stride * height, static_cast<BYTE*>(bits));
            if (SUCCEEDED(hr))
                *out = bitmap;
            else
                DeleteObject(bitmap);
        }
    }

    if (converter) converter->Release();
    if (scaler) scaler->Release();
    if (frame) frame->Release();
    if (decoder) decoder->Release();
    if (stream) stream->Release();
    factory->Release();
    return hr;
}

// --------------------------------------------------------------------------
// The provider
// --------------------------------------------------------------------------

class CThumbProvider : public IInitializeWithFile, public IThumbnailProvider {
public:
    CThumbProvider() : m_refs(1) { InterlockedIncrement(&g_objects); }

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) {
        static const QITAB qit[] = {
            QITABENT(CThumbProvider, IInitializeWithFile),
            QITABENT(CThumbProvider, IThumbnailProvider),
            { nullptr, 0 },
        };
        return QISearch(this, qit, riid, ppv);
    }
    IFACEMETHODIMP_(ULONG) AddRef() { return InterlockedIncrement(&m_refs); }
    IFACEMETHODIMP_(ULONG) Release() {
        ULONG left = InterlockedDecrement(&m_refs);
        if (!left)
            delete this;
        return left;
    }

    // IInitializeWithFile — chosen over the stream variants on purpose: the path
    // is the whole input. A stream would only offer the original's bytes, which
    // is exactly what this handler exists to avoid reading.
    IFACEMETHODIMP Initialize(LPCWSTR path, DWORD mode) {
        Log(L"Initialize: %s", path ? path : L"(null)");
        if (!m_path.empty())
            return HRESULT_FROM_WIN32(ERROR_ALREADY_INITIALIZED);
        m_path = path ? path : L"";
        m_mode = mode;
        return m_path.empty() ? E_INVALIDARG : S_OK;
    }

    IFACEMETHODIMP GetThumbnail(UINT cx, HBITMAP* phbmp, WTS_ALPHATYPE* pdwAlpha) {
        if (!phbmp || !pdwAlpha)
            return E_POINTER;
        *phbmp = nullptr;
        *pdwAlpha = WTSAT_UNKNOWN;
        if (m_path.empty())
            return E_UNEXPECTED;

        Log(L"GetThumbnail: cx=%u onMount=%d path=%s", cx, OnMount(m_path) ? 1 : 0, m_path.c_str());
        if (!OnMount(m_path))
            return Delegate(cx, phbmp, pdwAlpha);

        std::string jpeg;
        if (!FetchFromBridge(L"/rpc/thumb", m_path, &jpeg)) {
            Log(L"  preview fetch failed -> delegating");
            // No preview stored, or the bridge is not running. Falling back to
            // the displaced handler means the file still gets a thumbnail, just
            // the expensive way — better than a blank icon.
            return Delegate(cx, phbmp, pdwAlpha);
        }
        Log(L"  fetched %u bytes", static_cast<unsigned>(jpeg.size()));
        HBITMAP bitmap = nullptr;
        HRESULT hr = DecodeToBitmap(jpeg, cx, &bitmap);
        if (FAILED(hr)) {
            Log(L"  decode failed hr=0x%08X -> delegating", hr);
            return Delegate(cx, phbmp, pdwAlpha);
        }
        Log(L"  decoded, returning");
        *phbmp = bitmap;
        *pdwAlpha = WTSAT_RGB;  // Telegram previews are opaque JPEG
        return S_OK;
    }

private:
    ~CThumbProvider() { InterlockedDecrement(&g_objects); }

    // Hand the file to whatever provider was registered before us.
    HRESULT Delegate(UINT cx, HBITMAP* phbmp, WTS_ALPHATYPE* pdwAlpha) {
        std::wstring fallback = FallbackFor(m_path, L"Fallback");
        if (fallback.empty())
            return E_NOTIMPL;
        CLSID clsid;
        if (FAILED(CLSIDFromString(fallback.c_str(), &clsid)))
            return E_NOTIMPL;
        if (IsEqualCLSID(clsid, CLSID_TeleDriveThumb))
            return E_NOTIMPL;  // a botched install would otherwise recurse

        IThumbnailProvider* inner = nullptr;
        HRESULT hr = CoCreateInstance(clsid, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&inner));
        if (FAILED(hr))
            return hr;

        // Whichever way it wants to be initialized.
        IInitializeWithFile* withFile = nullptr;
        IInitializeWithItem* withItem = nullptr;
        IInitializeWithStream* withStream = nullptr;
        hr = E_FAIL;
        if (SUCCEEDED(inner->QueryInterface(IID_PPV_ARGS(&withFile)))) {
            hr = withFile->Initialize(m_path.c_str(), m_mode);
            withFile->Release();
        } else if (SUCCEEDED(inner->QueryInterface(IID_PPV_ARGS(&withItem)))) {
            // IInitializeWithItem is not an alternative spelling of the other
            // two — for video it is the only one that works. A video file has no
            // provider that decodes it: HKCR\.mp4\ShellEx\{e357fccd-...} names
            // shell32's Property Thumbnail Handler {9DBD2C50-...}, which pulls
            // System.ThumbnailStream out of the file's property store, and it
            // takes a shell item because that is what a property store is opened
            // from. It offers neither IInitializeWithFile nor
            // IInitializeWithStream, so with only those two tried this returned
            // E_FAIL and the shell asked nobody else: every video off the mount
            // lost its thumbnail, machine-wide, because this handler is
            // registered per extension. Measured on one file under two names,
            // .mp4 (ours, forwarding) 0 of 3 answered against .m4v (never
            // claimed, same {9DBD2C50}) 3 of 3.
            IShellItem* item = nullptr;
            hr = SHCreateItemFromParsingName(m_path.c_str(), nullptr, IID_PPV_ARGS(&item));
            if (SUCCEEDED(hr)) {
                hr = withItem->Initialize(item, m_mode);
                item->Release();
            }
            withItem->Release();
        } else if (SUCCEEDED(inner->QueryInterface(IID_PPV_ARGS(&withStream)))) {
            IStream* stream = nullptr;
            hr = SHCreateStreamOnFileEx(m_path.c_str(), STGM_READ | STGM_SHARE_DENY_NONE,
                                        FILE_ATTRIBUTE_NORMAL, FALSE, nullptr, &stream);
            if (SUCCEEDED(hr)) {
                hr = withStream->Initialize(stream, STGM_READ);
                stream->Release();
            }
            withStream->Release();
        }
        if (SUCCEEDED(hr))
            hr = inner->GetThumbnail(cx, phbmp, pdwAlpha);
        inner->Release();
        return hr;
    }

    LONG m_refs;
    std::wstring m_path;
    DWORD m_mode = 0;
};

// --------------------------------------------------------------------------
// Class factory and exports
// --------------------------------------------------------------------------

// Both handlers live in this DLL, so the factory carries the class it makes.
extern const CLSID CLSID_TeleDriveProps;
HRESULT CreatePropStore(REFIID riid, void** ppv);

class CClassFactory : public IClassFactory {
public:
    explicit CClassFactory(bool props) : m_refs(1), m_props(props) { InterlockedIncrement(&g_objects); }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) {
        static const QITAB qit[] = { QITABENT(CClassFactory, IClassFactory), { nullptr, 0 } };
        return QISearch(this, qit, riid, ppv);
    }
    IFACEMETHODIMP_(ULONG) AddRef() { return InterlockedIncrement(&m_refs); }
    IFACEMETHODIMP_(ULONG) Release() {
        ULONG left = InterlockedDecrement(&m_refs);
        if (!left)
            delete this;
        return left;
    }

    IFACEMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) {
        if (outer)
            return CLASS_E_NOAGGREGATION;
        if (m_props)
            return CreatePropStore(riid, ppv);
        CThumbProvider* provider = new (std::nothrow) CThumbProvider();
        if (!provider)
            return E_OUTOFMEMORY;
        HRESULT hr = provider->QueryInterface(riid, ppv);
        provider->Release();
        return hr;
    }

    IFACEMETHODIMP LockServer(BOOL lock) {
        if (lock)
            InterlockedIncrement(&g_locks);
        else
            InterlockedDecrement(&g_locks);
        return S_OK;
    }

private:
    ~CClassFactory() { InterlockedDecrement(&g_objects); }
    LONG m_refs;
    bool m_props;
};

STDAPI_(BOOL) DllMain(HINSTANCE instance, DWORD reason, void*) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_instance = instance;
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv) {
    bool props = IsEqualCLSID(rclsid, CLSID_TeleDriveProps) != FALSE;
    if (!props && !IsEqualCLSID(rclsid, CLSID_TeleDriveThumb))
        return CLASS_E_CLASSNOTAVAILABLE;
    Log(L"DllGetClassObject: %s", props ? L"properties" : L"thumbnail");
    CClassFactory* factory = new (std::nothrow) CClassFactory(props);
    if (!factory)
        return E_OUTOFMEMORY;
    HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release();
    return hr;
}

STDAPI DllCanUnloadNow() {
    return (g_objects == 0 && g_locks == 0) ? S_OK : S_FALSE;
}
