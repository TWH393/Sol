using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Sol.Desktop;

public sealed record DesktopOptions(string EngineRoot, string Python, string DataRoot)
{
    public static DesktopOptions Load()
    {
        string root = AppContext.BaseDirectory;
        string path = Path.Combine(root, "sol.desktop.json");
        if (!File.Exists(path))
        {
            // Visual Studio F5 uses the existing isolated development data.
            for (var folder = new DirectoryInfo(root); folder != null; folder = folder.Parent)
                if (File.Exists(Path.Combine(folder.FullName, "companion", "engine.py")) &&
                    File.Exists(Path.Combine(folder.FullName, ".venv", "Scripts", "python.exe")))
                    return new(folder.FullName, Path.Combine(folder.FullName, ".venv", "Scripts", "python.exe"), Path.Combine(folder.FullName, "state"));
        }
        using var document = File.Exists(path) ? JsonDocument.Parse(File.ReadAllText(path)) : JsonDocument.Parse("{}");
        string Resolve(string key, string fallback) => Path.GetFullPath(Environment.ExpandEnvironmentVariables(
            document.RootElement.TryGetProperty(key, out var value) ? value.GetString() ?? fallback : fallback), root);
        return new(Resolve("engineRoot", "engine"), Resolve("python", "python/python.exe"),
            Resolve("dataRoot", Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Sol", "data")));
    }
}

/// <summary>Owns exactly one authenticated engine; never takes over an occupied port.</summary>
public sealed class EngineHost : IAsyncDisposable
{
    public DesktopOptions Options { get; }
    public string Origin { get; private set; } = "";
    public string LogPath => Path.Combine(Options.DataRoot, "sol-engine.log");
    public bool Ready { get; private set; }
    public string Status { get; private set; } = "引擎尚未启动";
    public event Action? Changed;
    private Process? process;
    private readonly SemaphoreSlim lifecycle = new(1, 1);
    private readonly object logLock = new();
    private bool stopping;
    private readonly HttpClient client = new(new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false }) { Timeout = Timeout.InfiniteTimeSpan };

    public EngineHost(DesktopOptions options) => Options = options;

    private void SetStatus(string status)
    {
        Status = status;
        Changed?.Invoke();
    }

    public async Task StartAsync()
    {
        await lifecycle.WaitAsync();
        try
        {
            if (process is { HasExited: false })
                throw new InvalidOperationException("内部引擎仍在运行，请先停止后再启动");
            Ready = false;
            stopping = false;
            Directory.CreateDirectory(Options.DataRoot);
            if (!File.Exists(Options.Python)) throw new FileNotFoundException("缺少内置 Python 运行时，请重新准备安装包", Options.Python);
            if (!File.Exists(Path.Combine(Options.EngineRoot, "companion", "engine.py")))
                throw new FileNotFoundException("缺少 Sol 引擎组件", Options.EngineRoot);
            int port = 24760;
            string configPath = Path.Combine(Options.DataRoot, "settings.json");
            if (File.Exists(configPath))
            {
                using var cfg = JsonDocument.Parse(await File.ReadAllTextAsync(configPath));
                if (cfg.RootElement.TryGetProperty("app", out var app) && app.TryGetProperty("port", out var p)) port = p.GetInt32();
            }
            if (port is < 1024 or > 65535) throw new InvalidDataException("内部端口配置无效");
            // Fail before constructing Application (which opens the data store).
            var listener = new TcpListener(IPAddress.Any, port);
            try { listener.Start(); }
            catch (SocketException e) { throw new IOException($"端口 {port} 已被占用，Sol 没有连接或停止其他程序", e); }
            finally { listener.Stop(); }
            Origin = $"http://127.0.0.1:{port}";
            string token = Convert.ToHexString(RandomNumberGenerator.GetBytes(40));
            client.DefaultRequestHeaders.Remove("X-Sol-Desktop");
            client.DefaultRequestHeaders.Add("X-Sol-Desktop", token);
            var start = new ProcessStartInfo(Options.Python)
            {
                WorkingDirectory = Options.EngineRoot, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            start.ArgumentList.Add("-u"); start.ArgumentList.Add("-m"); start.ArgumentList.Add("companion.engine");
            start.Environment.Remove("PYTHONHOME");
            start.Environment["PYTHONPATH"] = Options.EngineRoot;
            start.Environment["PYTHONUTF8"] = "1";
            start.Environment["PYTHONNOUSERSITE"] = "1";
            start.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
            start.Environment["COMPANION_DATA"] = Options.DataRoot;
            start.Environment["SOL_DESKTOP_TOKEN"] = token;
            string resources = Path.Combine(AppContext.BaseDirectory, "resources");
            if (Directory.Exists(resources)) start.Environment["SOL_RESOURCE_ROOT"] = resources;
            string browsers = Path.Combine(AppContext.BaseDirectory, "browsers");
            if (Directory.Exists(browsers)) start.Environment["PLAYWRIGHT_BROWSERS_PATH"] = browsers;
            var child = new Process { StartInfo = start, EnableRaisingEvents = true };
            child.OutputDataReceived += (_, e) => Log(e.Data);
            child.ErrorDataReceived += (_, e) => Log(e.Data);
            child.Exited += (_, _) =>
            {
                Ready = false;
                if (!stopping) SetStatus($"内部引擎已退出（代码 {child.ExitCode}），请查看引擎日志");
            };
            SetStatus("正在启动内部引擎…");
            process?.Dispose();
            process = null;
            Log("---- Sol engine start " + DateTimeOffset.Now.ToString("O") + " ----");
            try { child.Start(); }
            catch { child.Dispose(); throw; }
            process = child;
            child.BeginOutputReadLine(); child.BeginErrorReadLine();
            var deadline = DateTime.UtcNow.AddSeconds(45);
            while (!child.HasExited && DateTime.UtcNow < deadline)
            {
                try
                {
                    var health = await GetAsync("desktop/health");
                    if (health.GetProperty("pid").GetInt32() != child.Id || health.GetProperty("product").GetString() != "Sol" ||
                        health.GetProperty("protocol").GetInt32() != 1 || !string.Equals(Path.GetFullPath(health.GetProperty("data").GetString()!), Options.DataRoot, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("引擎身份校验失败，拒绝连接");
                    Ready = true;
                    SetStatus("内部引擎已连接");
                    return;
                }
                catch (HttpRequestException) { }
                catch (TaskCanceledException) { }
                await Task.Delay(250);
            }
            throw new IOException(child.HasExited ? $"引擎启动失败（代码 {child.ExitCode}），查看 {LogPath}" : $"引擎启动超过 45 秒，查看 {LogPath}");
        }
        catch (Exception e)
        {
            Ready = false;
            if (process is { HasExited: false })
            {
                stopping = true;
                process.StandardInput.Close();
                await WaitForStopAsync(process);
            }
            SetStatus(e.Message);
            throw;
        }
        finally { lifecycle.Release(); }
    }

    public async Task<JsonElement> GetAsync(string route, CancellationToken cancellation = default)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(TimeSpan.FromSeconds(route == "qq/status" ? 12 : 8));
        using var response = await client.GetAsync(Origin + "/api/" + route, deadline.Token);
        return await ReadAsync(response, deadline.Token);
    }

    public async Task<JsonElement> PostAsync(string route, object value, CancellationToken cancellation = default)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(TimeSpan.FromSeconds(route.StartsWith("services/", StringComparison.Ordinal) ? 180 : route == "models/catalog" ? 100 : route == "plan/login" ? 60 : route == "qq/qrcode" ? 40 : 20));
        try
        {
            using var response = await client.PostAsJsonAsync(Origin + "/api/" + route, value, deadline.Token);
            return await ReadAsync(response, deadline.Token);
        }
        catch (OperationCanceledException) when (!cancellation.IsCancellationRequested)
        { throw new IOException("操作等待超时，请查看活动记录确认执行状态，避免重复点击。"); }
    }

    public async Task DownloadAsync(string ident, string destination)
    {
        using var response = await client.GetAsync(Origin + "/api/files/" + Uri.EscapeDataString(ident) + "/download", HttpCompletionOption.ResponseHeadersRead);
        if (!response.IsSuccessStatusCode) { await ReadAsync(response, default); return; }
        string temporary = destination + ".sol-download-" + Guid.NewGuid().ToString("N");
        try
        {
            await using (var file = File.Create(temporary)) await response.Content.CopyToAsync(file);
            File.Move(temporary, destination, true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    private static async Task<JsonElement> ReadAsync(HttpResponseMessage response, CancellationToken cancellation)
    {
        using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync(cancellation));
        if (!response.IsSuccessStatusCode)
            throw new IOException(document.RootElement.TryGetProperty("error", out var error) ? error.GetString() : $"内部接口失败：{(int)response.StatusCode}");
        return document.RootElement.Clone();
    }

    public async Task StopAsync()
    {
        await lifecycle.WaitAsync();
        try
        {
            stopping = true;
            Ready = false;
            if (process is not { HasExited: false }) { SetStatus("引擎已停止"); return; }
            SetStatus("正在停止机器人和内部引擎…");
            // Closing the lifetime lease also works when HTTP is unavailable.
            process.StandardInput.Close();
            await WaitForStopAsync(process);
            SetStatus("引擎已停止");
        }
        finally { lifecycle.Release(); }
    }

    private async Task WaitForStopAsync(Process child)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(150));
        try { await child.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException)
        {
            Log("Graceful shutdown timed out; terminating owned process tree. Docker stop may need administrator attention.");
            if (!child.HasExited) child.Kill(entireProcessTree: true);
            await child.WaitForExitAsync();
            throw new IOException("引擎停止超时，已结束本软件进程树；QQ 容器是否停止需要检查，详见引擎日志");
        }
    }

    private void Log(string? line)
    {
        if (string.IsNullOrWhiteSpace(line)) return;
        lock (logLock)
        {
            try
            {
                if (File.Exists(LogPath) && new FileInfo(LogPath).Length > 8 * 1024 * 1024)
                    File.Move(LogPath, Path.ChangeExtension(LogPath, ".previous.log"), true);
                File.AppendAllText(LogPath, line + Environment.NewLine, Encoding.UTF8);
            }
            catch (IOException) { }
        }
    }

    public void ReportInterface(string stage) => Log("[Desktop] " + stage);

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
        process?.Dispose(); client.Dispose(); lifecycle.Dispose();
    }
}
