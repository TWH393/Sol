using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Microsoft.Win32;

namespace Sol.Desktop;

/// <summary>One complete UI; WPF owns only the desktop and engine lifecycle.</summary>
public sealed class UnifiedWindow : Window
{
    private readonly EngineHost engine;
    private readonly App app;
    // HWND-hosted WebView2 hides WPF siblings, including the loading cover.
    private WebView2CompositionControl web = new();
    private readonly Grid surface = new();
    private readonly Border cover = new();
    private readonly LoadingMark loadingMark = new();
    private readonly TextBlock status = new() { TextWrapping = TextWrapping.Wrap, MaxWidth = 720, Margin = new Thickness(0, 12, 0, 18) };
    private readonly Button retry = new() { Content = "重新连接内部引擎" };
    private readonly Button enter = new() { Content = "先进入管理界面", Visibility = Visibility.Collapsed };
    private readonly Button login = new() { Content = "在 Sol 内扫码", Visibility = Visibility.Collapsed };
    private readonly Button logs = new() { Content = "查看日志", Visibility = Visibility.Collapsed };
    private CancellationTokenSource? startupCancellation;
    private TaskCompletionSource<bool>? navigation;
    private bool pageRendered, browserFailed;
    private string pageToken = "";
    private ulong activeNavigationId;
    private string lastStage = "";
    private QqLoginWindow? loginWindow;
    private bool closed, loading;
    private bool? autostart;

    public UnifiedWindow(EngineHost engine, App app)
    {
        this.engine = engine; this.app = app;
        Title = "Sol"; Width = 1400; Height = 920; MinWidth = 1000; MinHeight = 640;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        SetResourceReference(BackgroundProperty, "Surface"); SetResourceReference(ForegroundProperty, "Text");
        surface.Children.Add(web);
        var body = new StackPanel { HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(30) };
        body.Children.Add(loadingMark);
        body.Children.Add(new TextBlock { Text = "Sol", FontSize = 30, FontWeight = FontWeights.Medium, TextAlignment = TextAlignment.Center, Margin = new Thickness(0, 15, 0, 7) });
        status.TextAlignment = TextAlignment.Center;
        body.Children.Add(status);
        var controls = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Center };
        foreach (var button in new[] { retry, login, enter, logs }) { button.Margin = new Thickness(5); controls.Children.Add(button); }
        body.Children.Add(controls);
        cover.Child = body; cover.SetResourceReference(Border.BackgroundProperty, "Surface"); surface.Children.Add(cover); Content = surface;
        Panel.SetZIndex(cover, 10);
        status.Text = "正在准备内部引擎…"; retry.Visibility = Visibility.Collapsed;
        retry.Click += async (_, _) => await StartEngineAsync();
        enter.Click += (_, _) => { startupCancellation?.Cancel(); HideCover(); };
        logs.Click += (_, _) => Process.Start(new ProcessStartInfo(File.Exists(engine.LogPath) ? engine.LogPath : engine.Options.DataRoot) { UseShellExecute = true });
        login.Click += (_, _) =>
        {
            if (loginWindow != null) { App.BringForward(loginWindow); return; }
            loginWindow = new QqLoginWindow(engine, this, requestQr: true);
            loginWindow.Closed += (_, _) => loginWindow = null;
            loginWindow.Show();
        };
        loadingMark.SetRunning(true);
        SourceInitialized += (_, _) => Theme.ApplyWindowChrome(this);
        Theme.Changed += UpdateChrome;
        Closing += OnClosing;
        Closed += (_, _) => { closed = true; startupCancellation?.Cancel(); Theme.Changed -= UpdateChrome; web.Dispose(); };
        engine.Changed += () => Dispatcher.BeginInvoke(() =>
        {
            if (closed) return;
            if (!engine.Ready && engine.Status.StartsWith("内部引擎已退出", StringComparison.Ordinal)) ShowFailure(engine.Status);
        });
    }

    private void UpdateChrome() => Theme.ApplyWindowChrome(this);
    private bool Local(string source) => Uri.TryCreate(source, UriKind.Absolute, out var uri) && uri.GetLeftPart(UriPartial.Authority) == engine.Origin;
    private void ShowFailure(string message)
    {
        engine.ReportInterface("失败：" + message);
        startupCancellation?.Cancel();
        status.Text = message; loadingMark.SetRunning(false);
        retry.Content = engine.Ready ? "重新检查连接" : "重新连接内部引擎";
        retry.Visibility = logs.Visibility = Visibility.Visible; cover.Visibility = Visibility.Visible;
        enter.Visibility = engine.Ready && pageRendered && !browserFailed ? Visibility.Visible : Visibility.Collapsed;
        enter.IsEnabled = true;
    }

    private void ShowBusy(string message)
    {
        engine.ReportInterface(message);
        status.Text = message; retry.Visibility = login.Visibility = logs.Visibility = enter.Visibility = Visibility.Collapsed;
        cover.Visibility = Visibility.Visible; loadingMark.SetRunning(true);
    }

    private void HideCover()
    {
        cover.Visibility = Visibility.Collapsed; loadingMark.SetRunning(false);
    }

    private async Task WaitForStartupAsync(CancellationToken cancellation)
    {
        var began = DateTime.UtcNow;
        while (!closed)
        {
            cancellation.ThrowIfCancellationRequested();
            var data = await engine.GetAsync("desktop/startup", cancellation);
            string phase = data.GetProperty("phase").GetString() ?? "";
            string stage = data.GetProperty("stage").GetString() ?? "正在准备服务";
            if (lastStage != phase + ":" + stage) { lastStage = phase + ":" + stage; engine.ReportInterface(lastStage); }
            bool pageReady = pageRendered && navigation?.Task.IsCompletedSuccessfully == true && navigation.Task.Result;
            if (navigation?.Task.IsCompletedSuccessfully == true && !navigation.Task.Result) return;
            status.Text = phase == "ready" && !pageReady ? "内部服务已就绪，正在加载界面…" : stage;
            if (phase == "error")
            {
                string detail = data.TryGetProperty("error", out var error) ? error.GetString() ?? "" : "";
                ShowFailure(stage + (detail.Length > 0 ? "\n" + detail : "")); return;
            }
            if (phase == "ready" && pageReady) { HideCover(); loginWindow?.Close(); return; }
            bool scan = phase == "needs_scan";
            login.Visibility = scan ? Visibility.Visible : Visibility.Collapsed;
            loadingMark.SetRunning(!scan);
            if (scan || DateTime.UtcNow - began > TimeSpan.FromSeconds(8))
            {
                enter.Visibility = Visibility.Visible; enter.IsEnabled = pageReady;
            }
            if (DateTime.UtcNow - began > TimeSpan.FromSeconds(90))
            {
                status.Text += "\n加载时间较长，仍在等待完成；可查看日志";
                logs.Visibility = Visibility.Visible;
            }
            if (navigation?.Task.IsCompletedSuccessfully == true && !navigation.Task.Result) return;
            await Task.Delay(1000, cancellation);
        }
    }

    public async Task StartEngineAsync()
    {
        if (loading || closed) return;
        loading = true; retry.IsEnabled = false;
        startupCancellation?.Cancel();
        startupCancellation?.Dispose();
        startupCancellation = new CancellationTokenSource();
        try
        {
            ShowBusy("正在连接内部引擎…");
            pageRendered = false;
            pageToken = Guid.NewGuid().ToString("N");
            lastStage = ""; activeNavigationId = 0;
            if (browserFailed)
            {
                web.Dispose(); surface.Children.Remove(web);
                web = new WebView2CompositionControl(); surface.Children.Insert(0, web);
                browserFailed = false;
            }
            if (!engine.Ready) await engine.StartAsync();
            if (web.CoreWebView2 == null)
            {
                var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(engine.Options.DataRoot, "desktop", "webview")).WaitAsync(TimeSpan.FromSeconds(45), startupCancellation.Token);
                await web.EnsureCoreWebView2Async(environment).WaitAsync(TimeSpan.FromSeconds(45), startupCancellation.Token);
                var core = web.CoreWebView2 ?? throw new InvalidOperationException("桌面页面组件尚未初始化");
                core.Settings.AreDevToolsEnabled = false;
                core.Settings.IsStatusBarEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = false;
                core.Settings.IsPasswordAutosaveEnabled = false;
                core.Settings.IsGeneralAutofillEnabled = false;
                core.WebMessageReceived += Preferences;
                core.NewWindowRequested += NewWindow;
                core.NavigationStarting += (_, e) =>
                {
                    if (Local(e.Uri) && new Uri(e.Uri).Query.Contains("boot=" + pageToken, StringComparison.Ordinal))
                    { activeNavigationId = e.NavigationId; engine.ReportInterface("页面开始导航"); }
                    if (e.Uri == "about:blank" || Local(e.Uri)) return;
                    e.Cancel = true; OpenExternal(e.Uri);
                };
                core.NavigationCompleted += (_, e) =>
                {
                    if (closed) return;
                    if (activeNavigationId == 0 || e.NavigationId != activeNavigationId) return;
                    engine.ReportInterface("页面导航结束：" + (e.IsSuccess ? "等待渲染完成" : e.WebErrorStatus.ToString()));
                    navigation?.TrySetResult(e.IsSuccess);
                    if (!e.IsSuccess) ShowFailure("桌面界面未能加载：" + e.WebErrorStatus + "。可点击重新连接。");
                };
                core.ProcessFailed += (_, e) => { browserFailed = true; pageRendered = false; ShowFailure("界面进程异常：" + e.ProcessFailedKind + "。内部引擎状态未改变，可点击重新连接恢复界面。"); };
                core.DownloadStarting += async (_, e) =>
                {
                    var deferred = e.GetDeferral();
                    try
                    {
                        await Task.Yield();
                        if (closed) { e.Cancel = true; return; }
                        var dialog = new SaveFileDialog { FileName = Path.GetFileName(e.ResultFilePath), Title = "保存文件" };
                        if (dialog.ShowDialog(this) == true) { e.ResultFilePath = dialog.FileName; e.Handled = true; }
                        else e.Cancel = true;
                    }
                    finally { deferred.Complete(); }
                };
            }
            navigation = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            web.CoreWebView2.Navigate(engine.Origin + "/?desktop=1&boot=" + pageToken + "&theme=" + (Theme.IsDark ? "dark" : "light"));
            await WaitForStartupAsync(startupCancellation.Token);
        }
        catch (OperationCanceledException) when (closed || startupCancellation.IsCancellationRequested) { }
        catch (OperationCanceledException) { ShowFailure("读取启动状态超时，内部服务状态尚未确认。可以重新检查连接或进入管理界面查看详情。"); }
        catch (WebView2RuntimeNotFoundException) { ShowFailure("缺少微软Edge WebView2运行时，请安装后重新打开Sol。安装页：https://developer.microsoft.com/microsoft-edge/webview2/"); }
        catch (TimeoutException) { browserFailed = true; ShowFailure("桌面网页组件初始化超过45秒，尚未完成。内部引擎不会因此重启；可点击重新连接重建界面组件。"); }
        catch (Exception e) { ShowFailure("Sol界面未能启动：" + e.Message); if (!IsVisible) App.BringForward(this); }
        finally { loading = false; retry.IsEnabled = true; }
    }

    private void Preferences(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (!Local(e.Source) || closed) return;
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            var value = doc.RootElement;
            switch (value.GetProperty("type").GetString())
            {
                case "page-ready":
                    if (value.TryGetProperty("boot", out var readyToken) && readyToken.GetString() == pageToken)
                    { pageRendered = true; engine.ReportInterface("页面已完成首次渲染"); }
                    break;
                case "page-error":
                    if (loading && value.TryGetProperty("boot", out var failedToken) && failedToken.GetString() == pageToken)
                        ShowFailure("界面加载失败：" + (value.TryGetProperty("message", out var detail) ? detail.GetString() : "页面初始化未完成"));
                    break;
                case "preferences":
                    string theme = value.GetProperty("theme").GetString() ?? "light";
                    if ((theme is "light" or "dark") && (theme == "dark") != Theme.IsDark) Theme.Apply(theme);
                    bool enabled = value.GetProperty("autostart").GetBoolean();
                    if (autostart != enabled) { App.ConfigureAutostart(enabled, engine.Options); autostart = enabled; }
                    break;
                case "open-data": Process.Start(new ProcessStartInfo(engine.Options.DataRoot) { UseShellExecute = true }); break;
                case "open-logs": Process.Start(new ProcessStartInfo(File.Exists(engine.LogPath) ? engine.LogPath : engine.Options.DataRoot) { UseShellExecute = true }); break;
                case "stop-engine": _ = StopEngineAsync(false); break;
                case "restart-engine": _ = StopEngineAsync(true); break;
                case "exit": _ = app.ExitAsync(); break;
            }
        }
        catch (Exception error) { ThemedDialog.Show(this, error.Message, "Sol · 桌面操作失败"); }
    }

    private async void NewWindow(object? sender, CoreWebView2NewWindowRequestedEventArgs e)
    {
        e.Handled = true; var deferred = e.GetDeferral();
        try
        {
            if (Local(e.Uri) && new Uri(e.Uri).AbsolutePath == "/plan/open")
            {
                var result = await engine.PostAsync("plan/login", new { });
                OpenExternal(result.GetProperty("url").GetString()!);
            }
            else if (Local(e.Uri)) web.CoreWebView2.Navigate(e.Uri);
            else OpenExternal(e.Uri);
        }
        catch (Exception error) { ThemedDialog.Show(this, error.Message, "Sol · 无法打开"); }
        finally { deferred.Complete(); }
    }

    private async Task StopEngineAsync(bool restart)
    {
        if (loading || closed) return;
        startupCancellation?.Cancel();
        loading = true; retry.IsEnabled = false; ShowBusy("正在停止内部引擎…");
        bool stopped = false;
        try { await engine.StopAsync(); stopped = true; ShowFailure("内部引擎已停止，点击下方按钮可重新启动。"); }
        catch (Exception error) { ShowFailure("停止状态未能确认：" + error.Message); }
        finally { loading = false; retry.IsEnabled = true; }
        if (restart && stopped) await StartEngineAsync();
    }

    private static void OpenExternal(string target)
    {
        if (Uri.TryCreate(target, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https")
            Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
    }
    private void OnClosing(object? sender, CancelEventArgs e) { if (!app.Exiting) { e.Cancel = true; Hide(); } }
    public void BeginShutdown() { startupCancellation?.Cancel(); loginWindow?.Close(); IsEnabled = false; ShowBusy("正在停止机器人…"); }
}
