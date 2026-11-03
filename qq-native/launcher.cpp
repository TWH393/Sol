// Restricted to the isolated QQ copy prepared by Sol. The job owns only this child tree.
#include <windows.h>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>
#include <thread>
namespace fs = std::filesystem;
static int fail(const char* text) { std::cerr << text << " Win32=" << GetLastError() << std::endl; return 1; }
int wmain(int argc, wchar_t** argv) {
    if (argc != 4) return fail("需要独立QQ路径、独立资料目录与指定QQ号");
    fs::path client = fs::absolute(argv[1]), profile = fs::absolute(argv[2]);
    if (client.filename() != L"QQ.exe" || !fs::exists(client.parent_path()/L"sol-client.json") || profile.parent_path() != client.parent_path().parent_path()) return fail("拒绝接入非Sol独立QQ副本");
    std::wstring qq = argv[3]; if (qq.empty() || qq.find_first_not_of(L"0123456789") != std::wstring::npos) return fail("指定QQ号无效");
    wchar_t own_file[32768]; GetModuleFileNameW(nullptr, own_file, 32768); auto own = fs::path(own_file).parent_path();
    auto loader = own / L"sol-qq-loader.dll", bootstrap = own / L"bootstrap.cjs";
    if (!fs::exists(loader) || !fs::exists(bootstrap)) return fail("Sol接入组件不完整");
    fs::create_directories(profile/L"Roaming"); fs::create_directories(profile/L"Local");
    SetEnvironmentVariableW(L"SOL_QQ_BOOTSTRAP", bootstrap.c_str()); SetEnvironmentVariableW(L"SOL_QQ_EXPECTED", qq.c_str());
    SetEnvironmentVariableW(L"SOL_QQ_PROFILE", profile.c_str()); SetEnvironmentVariableW(L"SOL_QQ_STAGE_FILE", (profile.parent_path()/L"loader-stage.log").c_str());
    SetEnvironmentVariableW(L"USERPROFILE", profile.c_str()); SetEnvironmentVariableW(L"APPDATA", (profile/L"Roaming").c_str()); SetEnvironmentVariableW(L"LOCALAPPDATA", (profile/L"Local").c_str());
    HANDLE job = CreateJobObjectW(nullptr, nullptr); JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job || !SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) return fail("无法隔离QQ进程树");
    HANDLE lease = GetStdHandle(STD_INPUT_HANDLE);
    if (GetEnvironmentVariableW(L"SOL_QQ_LEASE", nullptr, 0)) {
        SetHandleInformation(lease, HANDLE_FLAG_INHERIT, 0);
        std::thread([job,lease] { char byte; DWORD count; ReadFile(lease, &byte, 1, &count, nullptr); TerminateJobObject(job, 0); ExitProcess(0); }).detach();
    }
    std::wstring command = L"\""+client.wstring()+L"\" --user-data-dir=\""+profile.wstring()+L"\"";
    std::vector<wchar_t> line(command.begin(), command.end()); line.push_back(0);
    STARTUPINFOW startup{}; startup.cb = sizeof(startup); startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW; startup.wShowWindow = SW_HIDE;
    SECURITY_ATTRIBUTES attrs{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    startup.hStdInput = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &attrs, OPEN_EXISTING, 0, nullptr); startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE); startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    for (auto handle : {startup.hStdInput, startup.hStdOutput, startup.hStdError}) if (handle && handle != INVALID_HANDLE_VALUE) SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(client.c_str(), line.data(), nullptr, nullptr, TRUE, CREATE_SUSPENDED, nullptr, client.parent_path().c_str(), &startup, &process)) { CloseHandle(job); return fail("独立QQ无法启动"); }
    if (!AssignProcessToJobObject(job, process.hProcess)) { TerminateProcess(process.hProcess, 1); CloseHandle(job); return fail("无法建立QQ进程隔离"); }
    std::wstring event_name = L"Local\\SolQQInit-"+std::to_wstring(process.dwProcessId);
    HANDLE ready = CreateEventW(nullptr, TRUE, FALSE, event_name.c_str());
    auto bytes = (loader.wstring().size()+1)*sizeof(wchar_t);
    void* remote = VirtualAllocEx(process.hProcess, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote || !WriteProcessMemory(process.hProcess, remote, loader.c_str(), bytes, nullptr)) { CloseHandle(job); return fail("无法准备独立QQ加载器"); }
    auto load = (LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    HANDLE thread = CreateRemoteThread(process.hProcess, nullptr, 0, load, remote, 0, nullptr);
    if (!thread || WaitForSingleObject(ready, 10000) != WAIT_OBJECT_0) { CloseHandle(job); return fail("Sol本地加载器尚未就绪"); }
    WaitForSingleObject(thread, 2000); CloseHandle(thread); CloseHandle(ready);
    VirtualFreeEx(process.hProcess, remote, 0, MEM_RELEASE); ResumeThread(process.hThread); CloseHandle(process.hThread);
    std::cout << "Sol QQ client PID=" << process.dwProcessId << std::endl;
    WaitForSingleObject(process.hProcess, INFINITE); DWORD code = 0; GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess); CloseHandle(job); return (int)code;
}
