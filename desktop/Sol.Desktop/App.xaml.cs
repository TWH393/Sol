using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using Microsoft.Win32;
using Forms = System.Windows.Forms;

namespace Sol.Desktop;

public partial class App : System.Windows.Application
{
    private Mutex? singleInstance;
    private Forms.NotifyIcon? tray;
    private System.Drawing.Icon? trayIcon;
    private readonly CancellationTokenSource exit = new();
    private EngineHost? engine;
    private string pipeName = "";
    private bool ownsMutex;
    public bool Exiting { get; private set; }

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        DispatcherUnhandledException += (_, error) =>
        {
            error.Handled = true;
            ThemedDialog.Show(MainWindow, error.Exception.Message, "Sol · 操作失败");
        };
        try
        {
#if COLOR_REVIEW
            bool reviewOnly = true;
#else
            bool reviewOnly = Array.Exists(e.Args, arg => arg == "--color-review");
#endif
            if (reviewOnly)
            {
                Theme.Apply("light");
                var preview = new MainWindow(new EngineHost(new DesktopOptions("仅配色预览", "", "仅配色预览")), this, true);
                MainWindow = preview;
                preview.Show();
                await preview.OpenColorReviewAsync();
                return;
            }
            var options = DesktopOptions.Load();
            Theme.Apply(Theme.ReadPreference(options.DataRoot));
            string identity = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(options.DataRoot))).ToLowerInvariant()[..16];
            // Same identity as the old preview: both cannot own the same data.
            singleInstance = new Mutex(true, "Local\\CompanionStudio-" + identity, out ownsMutex);
            pipeName = "Sol-" + identity;
            if (!ownsMutex)
            {
                try
                {
                    using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.Out);
                    await pipe.ConnectAsync(1200);
                    await pipe.WriteAsync(new byte[] { 1 });
                }
                catch (Exception) { ThemedDialog.Show(MainWindow, "同一数据目录的程序已经运行，请从任务栏或托盘打开。", "Sol"); }
                Shutdown(); return;
            }
            engine = new EngineHost(options);
            var window = new UnifiedWindow(engine, this);
            MainWindow = window;
            CreateTray(window);
            _ = ListenForActivationAsync(window);
            if (!Array.Exists(e.Args, arg => arg == "--tray")) window.Show();
            await window.StartEngineAsync();
        }
        catch (Exception error)
        {
            ThemedDialog.Show(MainWindow, error.Message, "Sol · 无法启动");
            if (engine != null) await ExitAsync(); else Shutdown(1);
        }
    }

    private void CreateTray(Window window)
    {
        tray = new Forms.NotifyIcon { Text = "Sol" };
        UpdateTrayTheme();
        tray.Visible = true;
        Theme.Changed += UpdateTrayTheme;
        var menu = new Forms.ContextMenuStrip
        {
            BackColor = System.Drawing.ColorTranslator.FromHtml(Theme.IsDark ? "#292929" : "#FAF8F3"),
            ForeColor = System.Drawing.ColorTranslator.FromHtml(Theme.IsDark ? "#ECECEC" : "#302B25")
        };
        menu.Items.Add("打开 Sol", null, (_, _) => Dispatcher.Invoke(() => BringForward(window)));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("退出并停止机器人", null, async (_, _) => await Dispatcher.InvokeAsync(ExitAsync).Task.Unwrap());
        tray.ContextMenuStrip = menu;
        tray.DoubleClick += (_, _) => Dispatcher.Invoke(() => BringForward(window));
    }

    private void UpdateTrayTheme()
    {
        if (tray == null) return;
        var previous = trayIcon;
        string name = Theme.IsDark ? "sol-dark" : "sol-main";
        using (var resource = GetResourceStream(new Uri("/Assets/" + name + ".ico", UriKind.Relative)).Stream)
        using (var icon = new System.Drawing.Icon(resource, new System.Drawing.Size(32, 32)))
            trayIcon = (System.Drawing.Icon)icon.Clone();
        tray.Icon = trayIcon;
        previous?.Dispose();
        if (tray.ContextMenuStrip is { } menu)
        {
            menu.BackColor = System.Drawing.ColorTranslator.FromHtml(Theme.IsDark ? "#292929" : "#FAF8F3");
            menu.ForeColor = System.Drawing.ColorTranslator.FromHtml(Theme.IsDark ? "#ECECEC" : "#302B25");
        }
    }

    private async Task ListenForActivationAsync(Window window)
    {
        while (!exit.IsCancellationRequested)
        {
            try
            {
                using var pipe = new NamedPipeServerStream(pipeName, PipeDirection.In, 1, PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync(exit.Token);
                byte[] message = new byte[1];
                using var readDeadline = CancellationTokenSource.CreateLinkedTokenSource(exit.Token);
                readDeadline.CancelAfter(TimeSpan.FromSeconds(2));
                if (await pipe.ReadAsync(message, readDeadline.Token) == 1)
                    await Dispatcher.InvokeAsync(() => BringForward(window));
            }
            catch (OperationCanceledException) { }
            catch (IOException) { await Task.Delay(500, exit.Token).ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing); }
        }
    }

    public static void BringForward(Window window)
    {
        window.Show();
        if (window.WindowState == WindowState.Minimized) window.WindowState = WindowState.Normal;
        window.Activate();
    }

    public static void ConfigureAutostart(bool enabled, DesktopOptions options)
    {
        using var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
        string command = "\"" + Environment.ProcessPath + "\" --tray";
        if (enabled) key.SetValue("Sol", command); else key.DeleteValue("Sol", false);
        string legacy = "\"" + Path.Combine(options.EngineRoot, "Companion.exe") + "\" --tray";
        // Remove only this project's obsolete registration, never another bot.
        if (string.Equals(key.GetValue("CompanionStudio") as string, legacy, StringComparison.OrdinalIgnoreCase))
            key.DeleteValue("CompanionStudio", false);
    }

    public async Task ExitAsync()
    {
        if (Exiting) return;
        Exiting = true;
        exit.Cancel();
        if (MainWindow is MainWindow window) { BringForward(window); window.BeginShutdown(); }
        else if (MainWindow is UnifiedWindow unified) unified.BeginShutdown();
        try { if (engine != null) await engine.DisposeAsync(); }
        catch (Exception e) { ThemedDialog.Show(MainWindow, e.Message, "Sol · 停止未完全确认"); }
        finally { tray?.Dispose(); Shutdown(); }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Theme.Changed -= UpdateTrayTheme;
        exit.Cancel(); tray?.Dispose(); trayIcon?.Dispose();
        if (ownsMutex) singleInstance?.ReleaseMutex();
        singleInstance?.Dispose();
        base.OnExit(e);
    }
}
