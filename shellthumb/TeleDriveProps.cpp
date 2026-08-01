// Property handler for files on the TeleDrive mount.
//
// The thumbnail handler stopped Explorer decoding whole images, but browsing a
// folder stayed slow: Explorer separately reads the head of every image to learn
// its pixel size — measured on the mount, 258 KB of a 2 MB JPEG, per file, with
// gaps of up to 9.7 seconds between thumbnail calls while it did so. Those reads
// go through a different shell extension point, the property handler, so the
// thumbnail one cannot help.
//
// Telegram already carries width, height and duration in the document's
// attributes, so the bridge can answer from metadata without moving a byte.
//
// Registration differs from the thumbnail handler in a way worth knowing: property
// handlers live under HKLM\...\PropertySystem\PropertyHandlers with no per-user
// override, so installing this needs administrator rights and affects every user
// on the machine. Anything off the mount is forwarded to the handler we displaced.

#include <windows.h>
#include <shlwapi.h>
#include <propsys.h>
#include <propkey.h>
#include <propvarutil.h>
#include <winhttp.h>
#include <new>
#include <string>
#include <vector>

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "propsys.lib")
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "user32.lib")

// {7A3F1C28-9B6D-4E51-8F42-C0D3E5A91B75}
extern const CLSID CLSID_TeleDriveProps =
    { 0x7a3f1c28, 0x9b6d, 0x4e51, { 0x8f, 0x42, 0xc0, 0xd3, 0xe5, 0xa9, 0x1b, 0x75 } };

// Shared with the thumbnail handler in the same DLL.
extern const wchar_t* kSettingsKey;
extern void Log(const wchar_t* format, ...);
extern bool OnMount(const std::wstring& path);
extern std::wstring RelativeToRoot(const std::wstring& path);
extern bool FetchFromBridge(const wchar_t* route, const std::wstring& path, std::string* out);
extern std::wstring FallbackFor(const std::wstring& path, const wchar_t* subkey);
extern LONG g_objects;

// --------------------------------------------------------------------------
// Just enough JSON for {"width":1920,"height":1080,"duration":12.5,"size":123}
// --------------------------------------------------------------------------

static bool JsonNumber(const std::string& json, const char* key, double* out) {
    std::string needle = std::string("\"") + key + "\"";
    size_t at = json.find(needle);
    if (at == std::string::npos)
        return false;
    at = json.find(':', at + needle.size());
    if (at == std::string::npos)
        return false;
    ++at;
    while (at < json.size() && (json[at] == ' ' || json[at] == '\t'))
        ++at;
    size_t start = at;
    while (at < json.size() && (isdigit(static_cast<unsigned char>(json[at])) || json[at] == '.' || json[at] == '-'))
        ++at;
    if (at == start)
        return false;
    *out = atof(json.substr(start, at - start).c_str());
    return true;
}

// --------------------------------------------------------------------------
// The store
// --------------------------------------------------------------------------

struct Entry {
    PROPERTYKEY key;
    PROPVARIANT value;
};

class CPropStore : public IPropertyStore, public IPropertyStoreCapabilities, public IInitializeWithFile {
public:
    CPropStore() : m_refs(1) { InterlockedIncrement(&g_objects); }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) {
        static const QITAB qit[] = {
            QITABENT(CPropStore, IPropertyStore),
            QITABENT(CPropStore, IPropertyStoreCapabilities),
            QITABENT(CPropStore, IInitializeWithFile),
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

    IFACEMETHODIMP Initialize(LPCWSTR path, DWORD mode) {
        if (!m_path.empty())
            return HRESULT_FROM_WIN32(ERROR_ALREADY_INITIALIZED);
        m_path = path ? path : L"";
        m_mode = mode;
        if (m_path.empty())
            return E_INVALIDARG;
        Log(L"props Initialize: %s", m_path.c_str());
        if (!OnMount(m_path))
            return InitDelegate();
        Load();
        return S_OK;
    }

    IFACEMETHODIMP GetCount(DWORD* count) {
        if (!count)
            return E_POINTER;
        if (m_inner)
            return m_inner->GetCount(count);
        *count = static_cast<DWORD>(m_values.size());
        return S_OK;
    }

    IFACEMETHODIMP GetAt(DWORD index, PROPERTYKEY* key) {
        if (!key)
            return E_POINTER;
        if (m_inner)
            return m_inner->GetAt(index, key);
        if (index >= m_values.size())
            return E_INVALIDARG;
        *key = m_values[index].key;
        return S_OK;
    }

    IFACEMETHODIMP GetValue(REFPROPERTYKEY key, PROPVARIANT* value) {
        if (!value)
            return E_POINTER;
        if (m_inner)
            return m_inner->GetValue(key, value);
        PropVariantInit(value);
        for (const Entry& e : m_values) {
            if (IsEqualPropertyKey(e.key, key))
                return PropVariantCopy(value, &e.value);
        }
        return S_OK;  // "not present" is an empty VT_EMPTY, not an error
    }

    // Read-only: nothing here can be written back to Telegram.
    IFACEMETHODIMP SetValue(REFPROPERTYKEY, REFPROPVARIANT) { return STG_E_ACCESSDENIED; }
    IFACEMETHODIMP Commit() { return STG_E_ACCESSDENIED; }
    IFACEMETHODIMP IsPropertyWritable(REFPROPERTYKEY) { return S_FALSE; }

private:
    ~CPropStore() {
        for (Entry& e : m_values)
            PropVariantClear(&e.value);
        if (m_inner)
            m_inner->Release();
        InterlockedDecrement(&g_objects);
    }

    void Add(REFPROPERTYKEY key, UINT value) {
        Entry e = {};
        e.key = key;
        if (SUCCEEDED(InitPropVariantFromUInt32(value, &e.value)))
            m_values.push_back(e);
    }

    void Load() {
        std::string json;
        if (!FetchFromBridge(L"/rpc/props", m_path, &json)) {
            Log(L"  props fetch failed -> delegating");
            InitDelegate();
            return;
        }
        double width = 0, height = 0, duration = 0;
        bool haveW = JsonNumber(json, "width", &width);
        bool haveH = JsonNumber(json, "height", &height);
        if (haveW && haveH) {
            Add(PKEY_Image_HorizontalSize, static_cast<UINT>(width));
            Add(PKEY_Image_VerticalSize, static_cast<UINT>(height));
            // The dimensions string is what Explorer's "Dimensions" column shows.
            wchar_t text[64];
            _snwprintf_s(text, _countof(text), _TRUNCATE, L"%u x %u",
                         static_cast<UINT>(width), static_cast<UINT>(height));
            Entry e = {};
            e.key = PKEY_Image_Dimensions;
            if (SUCCEEDED(InitPropVariantFromString(text, &e.value)))
                m_values.push_back(e);
        }
        if (JsonNumber(json, "duration", &duration) && duration > 0) {
            // 100-nanosecond units, the shell's unit for durations.
            Entry e = {};
            e.key = PKEY_Media_Duration;
            if (SUCCEEDED(InitPropVariantFromUInt64(static_cast<ULONGLONG>(duration * 10000000.0), &e.value)))
                m_values.push_back(e);
            if (haveW && haveH) {
                Add(PKEY_Video_FrameWidth, static_cast<UINT>(width));
                Add(PKEY_Video_FrameHeight, static_cast<UINT>(height));
            }
        }
        Log(L"  props: %u values", static_cast<unsigned>(m_values.size()));
    }

    // Hand off to whatever handler was registered before us.
    HRESULT InitDelegate() {
        std::wstring fallback = FallbackFor(m_path, L"PropFallback");
        if (fallback.empty())
            return S_OK;  // nothing to delegate to: answer with no properties
        CLSID clsid;
        if (FAILED(CLSIDFromString(fallback.c_str(), &clsid)) || IsEqualCLSID(clsid, CLSID_TeleDriveProps))
            return S_OK;

        IPropertyStore* inner = nullptr;
        if (FAILED(CoCreateInstance(clsid, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&inner))))
            return S_OK;

        HRESULT hr = E_FAIL;
        IInitializeWithFile* withFile = nullptr;
        IInitializeWithStream* withStream = nullptr;
        if (SUCCEEDED(inner->QueryInterface(IID_PPV_ARGS(&withFile)))) {
            hr = withFile->Initialize(m_path.c_str(), m_mode);
            withFile->Release();
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
        if (FAILED(hr)) {
            inner->Release();
            return S_OK;
        }
        m_inner = inner;
        return S_OK;
    }

    LONG m_refs;
    std::wstring m_path;
    DWORD m_mode = 0;
    std::vector<Entry> m_values;
    IPropertyStore* m_inner = nullptr;  // set when the file is not ours
};

HRESULT CreatePropStore(REFIID riid, void** ppv) {
    CPropStore* store = new (std::nothrow) CPropStore();
    if (!store)
        return E_OUTOFMEMORY;
    HRESULT hr = store->QueryInterface(riid, ppv);
    store->Release();
    return hr;
}
