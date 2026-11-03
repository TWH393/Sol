// Sol's own local QQ bootstrap. No third-party licensing service or remote signer.
#include <windows.h>
#include <string>
#include <atomic>
#include "MinHook.h"

using Env = void*;
using Value = void*;
using Define = int(__cdecl*)(Env, Value, size_t, const void*);
using Object = int(__cdecl*)(Env, Value*);
using String = int(__cdecl*)(Env, const char*, size_t, Value*);
using Run = int(__cdecl*)(Env, Value, Value*);
using Clear = int(__cdecl*)(Env, Value*);
using Boolean = int(__cdecl*)(Env, Value, bool*);
static Define real_define = nullptr;
static Object real_object = nullptr;
static String create_string = nullptr;
static Run run_script = nullptr;
static Clear clear_exception = nullptr;
static Boolean get_boolean = nullptr;
static decltype(&LoadLibraryExW) real_load_ex = nullptr;
static decltype(&LoadLibraryW) real_load = nullptr;
static std::atomic<bool> installed{false}, injected{false};
static thread_local bool busy = false;
static std::atomic<bool> entered{false};

static std::wstring env(const wchar_t* key) {
    wchar_t value[32768]{}; DWORD n = GetEnvironmentVariableW(key, value, 32768);
    return n && n < 32768 ? value : L"";
}
static std::string utf8(const std::wstring& input) {
    int n = WideCharToMultiByte(CP_UTF8, 0, input.c_str(), (int)input.size(), nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0'); WideCharToMultiByte(CP_UTF8, 0, input.c_str(), (int)input.size(), out.data(), n, nullptr, nullptr); return out;
}
static void stage(const char* text) {
    auto path = env(L"SOL_QQ_STAGE_FILE"); if (path.empty()) return;
    HANDLE file = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS, 0, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    std::string line = std::string(text) + "\r\n"; DWORD count; WriteFile(file, line.data(), (DWORD)line.size(), &count, nullptr); CloseHandle(file);
}
static void attempt_inject(Env context) {
    if (busy || injected.load()) return;
    if (!entered.exchange(true)) stage("已进入本地QQ NAPI初始化回调");
    busy = true;
    std::string path = utf8(env(L"SOL_QQ_BOOTSTRAP")), quoted;
    for (char c : path) { if (c == '\\' || c == '"') quoted += '\\'; quoted += c; }
    std::string script = "(()=>{if(process.type!=='browser')return false;const r=process.getBuiltinModule?.('module')?.createRequire(process.execPath)||process.mainModule?.require?.bind(process.mainModule)||(typeof require==='function'?require:null);if(!r)return false;const fs=r('node:fs');const start=()=>{try{const m={exports:{}};new Function('require','module','exports',fs.readFileSync(\""+quoted+"\",'utf8'))(r,m,m.exports);}catch(e){try{fs.appendFileSync(process.env.SOL_QQ_STAGE_FILE,'Sol JS初始化失败: '+String(e.message).slice(0,200)+'\\n');}catch{}}};r('node:timers').setImmediate(start);return true;})()";
    Value code{}, output{}; bool ok = false;
    if (create_string(context, script.c_str(), script.size(), &code) == 0 && run_script(context, code, &output) == 0) {
        get_boolean(context, output, &ok);
        if (ok) { injected.store(true); stage("已调度Sol接入模块，等待Node事件循环"); }
    } else if (clear_exception) { Value ignored{}; clear_exception(context, &ignored); }
    busy = false;
}
static int __cdecl hook_define(Env context, Value object, size_t count, const void* properties) {
    int result = real_define(context, object, count, properties);
    if (result == 0) attempt_inject(context); return result;
}
static int __cdecl hook_object(Env context, Value* output) {
    int result = real_object(context, output);
    if (result == 0) attempt_inject(context); return result;
}
static void install_napi(HMODULE module) {
    if (!module || installed.load()) return;
    auto address = GetProcAddress(module, "napi_define_properties"); if (!address) return;
    bool expected = false; if (!installed.compare_exchange_strong(expected, true)) return;
    create_string = (String)GetProcAddress(module, "napi_create_string_utf8");
    run_script = (Run)GetProcAddress(module, "napi_run_script");
    clear_exception = (Clear)GetProcAddress(module, "napi_get_and_clear_last_exception");
    get_boolean = (Boolean)GetProcAddress(module, "napi_get_value_bool");
    if (!create_string || !run_script || !get_boolean || MH_CreateHook((void*)address, (void*)hook_define, (void**)&real_define) != MH_OK || MH_EnableHook((void*)address) != MH_OK) {
        installed.store(false); stage("NAPI适配未完成"); return;
    }
    stage("已定位本地QQ NAPI接口，等待QQ初始化");
    auto object = GetProcAddress(module, "napi_create_object");
    if (object && MH_CreateHook((void*)object, (void*)hook_object, (void**)&real_object) == MH_OK) MH_EnableHook((void*)object);
}
static HMODULE WINAPI hook_load_ex(LPCWSTR path, HANDLE file, DWORD flags) {
    HMODULE module = real_load_ex(path, file, flags); install_napi(module); return module;
}
static HMODULE WINAPI hook_load(LPCWSTR path) {
    HMODULE module = real_load(path); install_napi(module); return module;
}
static DWORD WINAPI initialize(void*) {
    if (env(L"SOL_QQ_BOOTSTRAP").empty()) return 0;
    if (MH_Initialize() != MH_OK) { stage("加载器初始化失败"); return 0; }
    auto kernel = GetModuleHandleW(L"kernel32.dll");
    auto ex = GetProcAddress(kernel, "LoadLibraryExW"), simple = GetProcAddress(kernel, "LoadLibraryW");
    if (MH_CreateHook((void*)ex, (void*)hook_load_ex, (void**)&real_load_ex) != MH_OK || MH_CreateHook((void*)simple, (void*)hook_load, (void**)&real_load) != MH_OK || MH_EnableHook(MH_ALL_HOOKS) != MH_OK) {
        stage("本地模块加载适配失败"); return 0;
    }
    install_napi(GetModuleHandleW(L"QQNT.dll"));
    stage("Sol本地加载器已准备");
    std::wstring name = L"Local\\SolQQInit-" + std::to_wstring(GetCurrentProcessId());
    HANDLE event = OpenEventW(EVENT_MODIFY_STATE, FALSE, name.c_str()); if (event) { SetEvent(event); CloseHandle(event); }
    return 0;
}
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, void*) {
    if (reason == DLL_PROCESS_ATTACH) { DisableThreadLibraryCalls(module); HANDLE thread = CreateThread(nullptr, 0, initialize, nullptr, 0, nullptr); if (thread) CloseHandle(thread); }
    return TRUE;
}
