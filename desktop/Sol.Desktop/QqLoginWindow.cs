using System;
using System.IO;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Sol.Desktop;

/// <summary>The login surface uses only Sol's authenticated API, not a separate LLBot browser.</summary>
public sealed class QqLoginWindow : Window
{
    private readonly EngineHost engine;
    private readonly CancellationTokenSource lifetime = new();
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(3) };
    private readonly TextBlock state = new() { LineHeight = 24 };
    private readonly TextBlock error = new() { Margin = new Thickness(0, 12, 0, 12) };
    private readonly TextBlock hint = new() { Text = "启动 QQ 服务后在这里扫码", TextAlignment = TextAlignment.Center, Margin = new Thickness(0, 12, 0, 0) };
    private readonly Image qr = new() { Width = 240, Height = 240, Stretch = Stretch.Uniform, Visibility = Visibility.Collapsed };
    private bool busy, polling, closed;
    private DateTime expires, opened = DateTime.UtcNow;

    public QqLoginWindow(EngineHost engine, Window owner, bool requestQr = false)
    {
        this.engine = engine; Owner = owner; Title = "Sol · 登录机器人 QQ";
        Width = 820; Height = 670; MinWidth = 700; MinHeight = 540; WindowStartupLocation = WindowStartupLocation.CenterOwner;
        var root = new StackPanel { Margin = new Thickness(30) };
        root.Children.Add(new TextBlock { Text = "登录机器人 QQ", FontSize = 25, FontWeight = FontWeights.SemiBold });
        root.Children.Add(Theme.Bind(new TextBlock { Text = "在 Sol 内扫码登录指定账号。当前版本先支持私聊和群聊文字。", Margin = new Thickness(0, 10, 0, 24), TextWrapping = TextWrapping.Wrap }, (TextBlock.ForegroundProperty, "Muted")));
        var columns = new Grid(); columns.ColumnDefinitions.Add(new ColumnDefinition()); columns.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(285) });
        var left = new StackPanel { Margin = new Thickness(0, 0, 24, 0) }; left.Children.Add(state);
        var controls = new WrapPanel { Margin = new Thickness(0, 18, 0, 0) };
        controls.Children.Add(Action("启动并扫码登录", async () => { await engine.PostAsync("services/qq/start", new { }, lifetime.Token); await StatusAsync(); await QrcodeAsync(); }, true));
        controls.Children.Add(Action("刷新二维码", QrcodeAsync));
        controls.Children.Add(Action("停止服务", async () => { await engine.PostAsync("services/qq/stop", new { }, lifetime.Token); qr.Visibility = Visibility.Collapsed; hint.Text = "QQ 服务已停止"; await StatusAsync(); })); left.Children.Add(controls);
        error.SetResourceReference(TextBlock.ForegroundProperty, "ErrorText"); left.Children.Add(error);
        var diagnostics = new WrapPanel();
        diagnostics.Children.Add(Action("显示 QQ 诊断窗口", async () => { await engine.PostAsync("services/qq/show-window", new { }, lifetime.Token); }));
        diagnostics.Children.Add(Action("隐藏 QQ 窗口", async () => { await engine.PostAsync("services/qq/hide-window", new { }, lifetime.Token); }));
        left.Children.Add(new Expander { Header = "登录遇到问题", Content = diagnostics, Margin = new Thickness(0, 10, 0, 0) });
        var imagePanel = new StackPanel(); imagePanel.Children.Add(qr); imagePanel.Children.Add(hint);
        var frame = Theme.Bind(new Border { Child = imagePanel, CornerRadius = new CornerRadius(16), BorderThickness = new Thickness(1), Padding = new Thickness(16), VerticalAlignment = VerticalAlignment.Top }, (Border.BackgroundProperty, "Input"), (Border.BorderBrushProperty, "Line"));
        columns.Children.Add(left); Grid.SetColumn(frame, 1); columns.Children.Add(frame); root.Children.Add(columns);
        Content = new ScrollViewer { Content = root, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        SourceInitialized += (_, _) => Theme.ApplyWindowChrome(this);
        Loaded += async (_, _) =>
        {
            await PollAsync();
            if (requestQr && !closed)
            {
                busy = true;
                try { await QrcodeAsync(); }
                catch (OperationCanceledException) when (closed) { }
                catch (Exception exc) { if (!closed) error.Text = exc.Message; }
                finally { busy = false; }
            }
            if (!closed) timer.Start();
        };
        timer.Tick += async (_, _) => await PollAsync();
        Closed += (_, _) => { closed = true; timer.Stop(); lifetime.Cancel(); };
    }

    private Button Action(string text, Func<Task> work, bool primary = false)
    {
        var button = new Button { Content = text, Margin = new Thickness(0, 0, 8, 10) };
        if (primary) button.SetResourceReference(StyleProperty, "PrimaryButton");
        button.Click += async (_, _) =>
        {
            if (busy) return; busy = true; button.IsEnabled = false; button.Content = "正在处理…"; error.Text = "";
            try { await work(); }
            catch (OperationCanceledException) when (closed) { }
            catch (Exception exc) { if (!closed) error.Text = exc.Message; }
            finally { busy = false; if (!closed) { button.IsEnabled = true; button.Content = text; } }
        };
        return button;
    }

    private async Task PollAsync()
    {
        if (closed || busy || polling) return;
        if (DateTime.UtcNow - opened > TimeSpan.FromMinutes(5)) { timer.Stop(); return; }
        polling = true;
        try { await StatusAsync(); }
        catch (OperationCanceledException) when (closed) { }
        catch (Exception exc) { if (!closed) error.Text = exc.Message; }
        finally { polling = false; }
    }

    private async Task StatusAsync()
    {
        var s = await engine.GetAsync("qq/status", lifetime.Token); if (closed) return;
        string number = s.GetProperty("expected").GetString() ?? "";
        bool online = s.GetProperty("online").GetBoolean(), connected = s.GetProperty("connected").GetBoolean();
        string failure = s.GetProperty("error").GetString() ?? "";
        string phase = s.TryGetProperty("phase", out var p) ? p.GetString() ?? "" : "";
        string stage = s.TryGetProperty("stage", out var st) ? st.GetString() ?? "" : "";
        string actual = s.TryGetProperty("actual", out var a) ? a.GetString() ?? "" : "";
        state.Text = "指定账号：" + (number.Length == 0 ? "尚未填写，请返回设置保存" : number) + "\n" +
            (failure.Length > 0 ? failure : online ? connected ? "QQ 已登录，机器人已连接" : "QQ 已登录，等待机器人连接" : stage.Length > 0 ? stage : "QQ 服务未启动") +
            (actual.Length > 0 ? "\n当前账号：" + actual : "");
        if (online) { qr.Visibility = Visibility.Collapsed; hint.Text = failure.Length > 0 ? "账号不符，请停止服务后处理" : "已登录，无需重复扫码"; }
        else if (phase is "qrcode_failed" or "error" or "offline" or "starting_session" or "stopped") { qr.Visibility = Visibility.Collapsed; hint.Text = phase == "starting_session" ? "扫码已确认，正在准备消息接入" : "二维码不可用，请按左侧提示处理"; }
        else if (expires != default && DateTime.UtcNow > expires) { qr.Visibility = Visibility.Collapsed; hint.Text = "二维码已过期，请点击刷新二维码"; }
    }

    private async Task QrcodeAsync()
    {
        qr.Visibility = Visibility.Collapsed; expires = default; hint.Text = "正在获取二维码…";
        var data = await engine.PostAsync("qq/qrcode", new { }, lifetime.Token); if (closed) return;
        if (data.GetProperty("online").GetBoolean()) { await StatusAsync(); return; }
        byte[] bytes = Convert.FromBase64String(data.GetProperty("image").GetString()!);
        using var stream = new MemoryStream(bytes);
        var image = new BitmapImage(); image.BeginInit(); image.CacheOption = BitmapCacheOption.OnLoad; image.StreamSource = stream; image.EndInit(); image.Freeze();
        qr.Source = image; qr.Visibility = Visibility.Visible;
        int seconds = data.GetProperty("expires_in").GetInt32(); expires = DateTime.UtcNow.AddSeconds(seconds);
        hint.Text = "请用 " + data.GetProperty("expected").GetString() + " 的手机QQ扫码并确认\n二维码约 " + seconds + " 秒内有效";
    }
}
