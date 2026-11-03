using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Data;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace Sol.Desktop;

public partial class MainWindow : Window
{
    private readonly EngineHost engine;
    private readonly App app;
    private readonly DispatcherTimer refreshTimer = new() { Interval = TimeSpan.FromSeconds(15) };
    private readonly List<(string Key, string Label)> routes = new()
    {
        ("overview", "运行总览"), ("conversations", "会话"), ("tasks", "任务中心"), ("files", "文件库"),
        ("memories", "统一记忆"), ("persona", "人格与成长"), ("models", "模型与思考"),
        ("usage", "用量与额度"), ("features", "功能中心"), ("extensions", "插件与技能"), ("events", "活动记录")
    };
    private readonly Dictionary<string, Button> navigation = new();
    private string route = "overview";
    private bool loading;
    private bool? appliedAutostart;
    private JsonElement state;
    private JsonElement config;
    private int generation;
    private string? pendingTheme;

    public MainWindow(EngineHost engine, App app, bool colorReview = false)
    {
        InitializeComponent();
        this.engine = engine; this.app = app;
        this.colorReview = colorReview;
        SourceInitialized += (_, _) => UpdateThemeChrome();
        Theme.Changed += UpdateThemeChrome;
        foreach (var (key, label) in routes)
        {
            if (key == "models") Navigation.Children.Add(Theme.Bind(new Border { Height = 1, Margin = new Thickness(10, 16, 10, 16) }, (Border.BackgroundProperty, "Line")));
            var button = new Button { Content = NavigationLabel(key, label), Style = (Style)FindResource("NavigationButton") };
            button.Click += async (_, _) => await NavigateAsync(key);
            navigation[key] = button; Navigation.Children.Add(button);
        }
        engine.Changed += () => Dispatcher.BeginInvoke(() =>
        {
            SidebarStatus.Text = engine.Status; Footer.Text = engine.Status;
            if (engine.Status.StartsWith("内部引擎已退出", StringComparison.Ordinal) && !app.Exiting)
            {
                refreshTimer.Stop(); PageContent.Content = EngineRecovery(); ShowError(engine.Status);
            }
        });
        Closing += OnClosing;
        Closed += (_, _) => { refreshTimer.Stop(); Theme.Changed -= UpdateThemeChrome; };
        refreshTimer.Tick += async (_, _) => await RefreshAsync(false);
        PageContent.Content = Paragraph("正在准备 Sol", "正在连接内部引擎，首次使用请先配置账号和主模型。");
    }

    public async Task StartEngineAsync()
    {
        try
        {
            await engine.StartAsync();
            await NavigateAsync(route);
            refreshTimer.Start();
        }
        catch (Exception e)
        {
            ShowError(e.Message);
            PageContent.Content = EngineRecovery();
            if (!IsVisible) App.BringForward(this);
        }
    }

    public void BeginShutdown()
    {
        refreshTimer.Stop();
        IsEnabled = false;
        Footer.Text = "正在停止机器人，请稍候…";
    }

    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (app.Exiting) return;
        if (colorReview) { Dispatcher.BeginInvoke(() => app.Shutdown()); return; }
        e.Cancel = true; Hide();
    }

    public void OpenBrowser(string? target = null)
    {
        if (colorReview) return;
        if (!engine.Ready) { ShowError("内部引擎尚未连接，请先启动引擎"); return; }
        string page = target ?? route;
        page = page switch { "memories" => "memory", "extensions" => "plugins", "events" => "activity", _ => page };
        OpenExternal(engine.Origin + "/#" + page);
    }

    private void OpenExternal(string target)
    {
        try { Process.Start(new ProcessStartInfo(target) { UseShellExecute = true }); }
        catch (Exception e) { ShowError("无法打开：" + e.Message); }
    }

    private void BrowserClick(object sender, RoutedEventArgs e) => OpenBrowser();
    private async void SettingsClick(object sender, RoutedEventArgs e) => await NavigateAsync("settings");
    private async void RefreshClick(object sender, RoutedEventArgs e) => await RefreshAsync(true);

    private async void ThemeClick(object sender, RoutedEventArgs e)
    {
        if (colorReview) { Theme.Apply(Theme.IsDark ? "light" : "dark"); await RenderColorReviewAsync(route); return; }
        pendingTheme = Theme.IsDark ? "light" : "dark";
        Theme.Apply(pendingTheme);
        generation++;
        if (engine.Ready) await RefreshAsync(true);
        else ShowMessage("外观已切换，引擎连接后会保存这个选择");
    }

    private void UpdateThemeChrome()
    {
        Theme.ApplyWindowChrome(this);
        ThemeButton.Content = Theme.IsDark ? "切换浅色" : "切换暗色";
    }

    private async Task NavigateAsync(string key)
    {
        route = key; generation++;
        PageTitle.Text = key == "settings" ? "设置与运行环境" : routes.First(r => r.Key == key).Label;
        BreadcrumbTitle.Text = PageTitle.Text;
        PageSubtitle.Text = PageDescription(key);
        foreach (var pair in navigation)
        {
            pair.Value.Tag = pair.Key == key ? "active" : "";
        }
        SettingsNavigation.Tag = key == "settings" ? "active" : "";
        if (colorReview) { await RenderColorReviewAsync(key); return; }
        if (!engine.Ready) { PageContent.Content = EngineRecovery(); return; }
        await RefreshAsync(true);
    }

    private async Task RefreshAsync(bool render)
    {
        if (colorReview) { await RenderColorReviewAsync(route); return; }
        if (loading || !engine.Ready || app.Exiting) return;
        loading = true;
        int revision = generation;
        string requested = route;
        try
        {
            // Local state only; never probes QQ logins, model APIs or Docker here.
            var stateRequest = engine.GetAsync("state");
            var configRequest = engine.GetAsync("settings");
            await Task.WhenAll(stateRequest, configRequest);
            state = await stateRequest; config = await configRequest;
            if (pendingTheme is { } requestedTheme)
            {
                var saved = await engine.PostAsync("settings", new { app = new { theme = requestedTheme } });
                if (pendingTheme == requestedTheme) pendingTheme = null;
                config = saved;
            }
            Theme.Apply(pendingTheme ?? Text(config.GetProperty("app"), "theme", "light"));
            bool autostart = config.GetProperty("app").GetProperty("autostart").GetBoolean();
            if (appliedAutostart != autostart)
            {
                App.ConfigureAutostart(autostart, engine.Options);
                appliedAutostart = autostart;
            }
            Timestamp.Text = "更新于 " + DateTime.Now.ToString("HH:mm:ss");
            SidebarStatus.Text = state.GetProperty("online").GetBoolean() ? "QQ 在线" : "QQ 未连接";
            SidebarIdentity.Text = "Sol" + (Text(state.GetProperty("account"), "qq") is { Length: > 0 } qq ? " · " + qq : " · 未配置账号");
            Footer.Text = engine.Status + " · 关闭窗口后在托盘运行";
            if (revision != generation) return;
            if (!render && (!IsVisible || requested != "overview")) return;
            await RenderAsync(requested, revision);
            if (revision == generation) NoticeBox.Visibility = Visibility.Collapsed;
        }
        catch (Exception e) { ShowError(e.Message); }
        finally
        {
            loading = false;
            // Navigation that occurred during a local read gets its own render.
            if (revision != generation && !app.Exiting) await RefreshAsync(true);
        }
    }

    private async Task RenderAsync(string page, int revision)
    {
        FrameworkElement content;
        switch (page)
        {
            case "overview": content = Overview(); break;
            case "models": content = Models(); break;
            case "settings": content = Settings(); break;
            case "features": content = Features(); break;
            case "usage":
                var usage = await engine.GetAsync("usage");
                if (revision != generation) return;
                content = Records("usage", usage.GetProperty("calls"));
                break;
            default:
                var rows = await engine.GetAsync(page);
                if (revision != generation) return;
                content = Records(page, rows);
                break;
        }
        if (revision == generation) PageContent.Content = content;
    }

    private FrameworkElement Overview()
    {
        var stack = new StackPanel();
        if (colorReview) stack.Children.Add(Caption("这是隔离的颜色样例，不代表真实连接、任务或用量。"));
        var cards = new System.Windows.Controls.Primitives.UniformGrid { Columns = 3, Margin = new Thickness(0, 0, 0, 20) };
        cards.Children.Add(Card("机器人连接", state.GetProperty("online").GetBoolean() ? "在线" : "未连接", Text(state.GetProperty("account"), "qq", "尚未配置 QQ")));
        cards.Children.Add(Card("主模型", Text(state, "model", "尚未选择"), "模型与思考强度在 Sol 中统一设置"));
        var counts = state.GetProperty("task_counts");
        cards.Children.Add(Card("正在处理", Number(counts, "running").ToString(), "等待中 " + Number(counts, "queued") + " 项"));
        var resources = state.GetProperty("resources");
        cards.Children.Add(Card("引擎内存", Size(Number(resources, "process_memory")), "仅内部引擎进程，不包括 GPU 与容器"));
        cards.Children.Add(Card("文件", Number(state.GetProperty("file_count"), "n").ToString(), "已整理记忆 " + Number(state, "memory_count") + " 条"));
        cards.Children.Add(Card("系统内存", Size(Number(resources, "memory_used")), "总量 " + Size(Number(resources, "memory_total"))));
        stack.Children.Add(cards);
        string coreError = Text(state.GetProperty("core"), "error");
        if (coreError.Length > 0) stack.Children.Add(Paragraph("核心需要处理", coreError));
        if (!state.GetProperty("account").GetProperty("enabled").GetBoolean())
            stack.Children.Add(Paragraph("开始使用", "先在设置中填写机器人和管理员账号，再配置主模型。账号接入默认关闭，不会猜测或登录其他 QQ。"));
        stack.Children.Add(new TextBlock { Text = "最近活动", FontSize = 18, Margin = new Thickness(0, 18, 0, 12) });
        foreach (var row in state.GetProperty("events").EnumerateArray().Take(7))
            stack.Children.Add(Theme.Bind(new TextBlock { Text = When(row, "created") + "   " + Text(row, "title", Text(row, "text")), Margin = new Thickness(0, 6, 0, 6) }, (TextBlock.ForegroundProperty, "Muted")));
        return Scroll(stack);
    }

    private FrameworkElement Records(string page, JsonElement rows)
    {
        if (rows.ValueKind != JsonValueKind.Array) return Paragraph("当前记录", rows.ToString());
        string[] keys = page switch
        {
            "tasks" => new[] { "id", "title", "status", "owner", "scope", "created", "error" },
            "files" => new[] { "id", "name", "owner", "status", "size", "created", "task_id" },
            "conversations" => new[] { "bot", "scope", "count", "latest" },
            "memories" => new[] { "id", "text", "owner", "scope", "kind", "visibility", "updated" },
            "persona" => new[] { "created", "reason", "text", "dimensions" },
            "events" => new[] { "created", "level", "kind", "title", "detail" },
            "extensions" => new[] { "id", "name", "enabled", "status", "source" },
            "usage" => new[] { "owner", "model", "source", "input", "output", "created" },
            _ => Array.Empty<string>()
        };
        var grid = BuildTable(rows, keys);
        var panel = new DockPanel();
        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 14) };
        toolbar.Children.Add(Theme.Bind(new TextBlock { Text = $"{rows.GetArrayLength()} 条记录", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 18, 0) }, (TextBlock.ForegroundProperty, "Muted")));
        toolbar.Children.Add(ActionButton("完整管理页 ↗", () => { OpenBrowser(); return Task.CompletedTask; }));
        if (page == "tasks") toolbar.Children.Add(ActionButton("取消选中任务", async () =>
        {
            string id = Selected(grid, "id");
            if (ThemedDialog.Show(this, "取消这个任务？已完成的步骤不会撤销。", "Sol", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
            await engine.PostAsync("tasks/" + Uri.EscapeDataString(id) + "/cancel", new { });
            await RefreshAsync(true);
        }));
        if (page == "files") toolbar.Children.Add(ActionButton("保存选中文件", async () =>
        {
            string id = Selected(grid, "id");
            var dialog = new Microsoft.Win32.SaveFileDialog { FileName = Path.GetFileName(Selected(grid, "name")) };
            if (dialog.ShowDialog(this) == true) await engine.DownloadAsync(id, dialog.FileName);
        }));
        if (page == "conversations") toolbar.Children.Add(ActionButton("查看选中会话", async () =>
        {
            var messages = await engine.GetAsync("messages?scope=" + Uri.EscapeDataString(Selected(grid, "scope")));
            var dialog = new Window { Title = "Sol · 会话（只读）", Width = 900, Height = 700, Owner = this, WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Content = BuildTable(messages, new[] { "created", "name", "sender", "text", "quote_id", "topic" }) };
            dialog.SourceInitialized += (_, _) => Theme.ApplyWindowChrome(dialog);
            dialog.ShowDialog();
        }));
        DockPanel.SetDock(toolbar, Dock.Top); panel.Children.Add(toolbar);
        var preview = new TextBox { IsReadOnly = true, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Height = 130, Text = "选择一条记录查看完整内容", Margin = new Thickness(0, 16, 0, 0) };
        grid.SelectionChanged += (_, _) =>
        {
            if (grid.SelectedItem is DataRowView item)
                preview.Text = string.Join(Environment.NewLine, item.Row.Table.Columns.Cast<DataColumn>().Select(c => c.ColumnName + "：" + item[c.ColumnName]));
        };
        DockPanel.SetDock(preview, Dock.Bottom); panel.Children.Add(preview); panel.Children.Add(grid);
        return Panel(panel);
    }

    private FrameworkElement Models()
    {
        var stack = new StackPanel();
        stack.Children.Add(ModelSources());
        stack.Children.Add(Caption("更改影响新请求；已开始的任务保留原模型配置。保存设置不发起模型测试。"));
        var providers = config.GetProperty("models").GetProperty("providers");
        if (providers.GetArrayLength() == 0) stack.Children.Add(Panel(Paragraph("尚未添加模型", "选择上方的服务商添加模型。模型和思考强度在此统一管理，保存不消耗模型额度。")));
        foreach (var p in providers.EnumerateArray())
        {
            string id = Text(p, "id");
            bool selected = id == Text(config.GetProperty("models"), "selected");
            var body = new StackPanel();
            body.Children.Add(new TextBlock { Text = Text(p, "name", id) + (selected ? "  · 当前主模型" : ""), FontSize = 19, FontWeight = FontWeights.SemiBold });
            body.Children.Add(Caption(Text(p, "model") + " · " + Text(p, "kind")));
            var modes = new ComboBox { ItemsSource = new[] { new Choice("fixed", "固定强度"), new Choice("auto", "自动调整") }, DisplayMemberPath = "Label", SelectedValuePath = "Value", SelectedValue = Text(p, "effort_mode", "fixed"), Width = 120, Margin = new Thickness(0, 0, 10, 0) };
            var efforts = new List<string> { "default" };
            if (p.TryGetProperty("efforts", out var supported)) efforts.AddRange(supported.EnumerateArray().Select(x => x.GetString()!).Where(x => x != "default"));
            var strengthNames = new Dictionary<string, string> { ["default"]="模型默认", ["none"]="关闭", ["low"]="低", ["medium"]="中", ["high"]="高", ["max"]="最高", ["xhigh"]="极高" };
            var effort = new ComboBox { ItemsSource = efforts.Select(x => new Choice(x, strengthNames.GetValueOrDefault(x, x))).ToList(), DisplayMemberPath = "Label", SelectedValuePath = "Value", SelectedValue = Text(p, "effort", "default"), Width = 125, Margin = new Thickness(0, 0, 10, 0) };
            var controls = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 15, 0, 0) };
            controls.Children.Add(modes); controls.Children.Add(effort);
            controls.Children.Add(ActionButton("保存强度", async () =>
            {
                var fresh = JsonNode.Parse((await engine.GetAsync("settings")).GetRawText())!;
                var list = fresh["models"]!["providers"]!.AsArray();
                var provider = list.FirstOrDefault(x => x?["id"]?.GetValue<string>() == id) ?? throw new InvalidOperationException("该模型已被删除，请刷新");
                provider["effort_mode"] = modes.SelectedValue?.ToString() ?? "fixed";
                provider["effort"] = effort.SelectedValue?.ToString() ?? "default";
                await engine.PostAsync("settings", new { models = new { providers = list.DeepClone() } });
                ShowMessage("思考强度已保存，下次请求生效");
            }));
            if (!selected) controls.Children.Add(ActionButton("用于全部会话", async () =>
            {
                await engine.PostAsync("settings", new { models = new { selected = id } }); await RefreshAsync(true);
            }));
            body.Children.Add(controls); stack.Children.Add(Panel(body));
        }
        stack.Children.Add(ActionButton("添加模型与配置密钥 ↗", () => { OpenBrowser("models"); return Task.CompletedTask; }));
        return Scroll(stack);
    }

    private FrameworkElement Features()
    {
        var stack = new StackPanel(); stack.Children.Add(Caption("功能开关保存到同一套引擎配置。基础服务保持启用，具体执行权限在完整管理页调整。"));
        var controls = new Dictionary<string, CheckBox>();
        var labels = new Dictionary<string, string> { ["chat"] = "自然聊天", ["groups"] = "群聊", ["memory"] = "记忆", ["models"] = "模型网关", ["reuse"] = "结果复用", ["images"] = "图片处理", ["documents"] = "文档与 PDF", ["python"] = "代码执行", ["video"] = "视频总结", ["tasks"] = "任务调度", ["usage"] = "额度统计", ["plugins"] = "插件与技能" };
        foreach (var feature in config.GetProperty("features").EnumerateObject())
        {
            var box = new CheckBox { Content = labels.GetValueOrDefault(feature.Name, feature.Name), IsChecked = feature.Value.GetBoolean(), IsEnabled = feature.Name is not ("models" or "tasks" or "usage") };
            controls[feature.Name] = box; stack.Children.Add(box);
        }
        stack.Children.Add(ActionButton("保存功能开关", async () => { await engine.PostAsync("settings", new { features = controls.ToDictionary(p => p.Key, p => p.Value.IsChecked == true) }); ShowMessage("功能开关已保存"); }));
        return Scroll(stack);
    }

    private FrameworkElement EngineRecovery()
    {
        var stack = new StackPanel { VerticalAlignment = VerticalAlignment.Center, MaxWidth = 650 };
        stack.Children.Add(Paragraph("内部引擎未连接", engine.Status));
        var controls = new WrapPanel { Margin = new Thickness(0, 15, 0, 0) };
        controls.Children.Add(ActionButton("启动内部引擎", StartEngineAsync));
        controls.Children.Add(ActionButton("查看引擎日志", () => { OpenLog(); return Task.CompletedTask; }));
        controls.Children.Add(ActionButton("退出 Sol", app.ExitAsync)); stack.Children.Add(controls);
        return stack;
    }

    private void OpenLog()
    {
        if (File.Exists(engine.LogPath)) OpenExternal(engine.LogPath); else ShowMessage("尚未创建引擎日志。启动失败原因显示在窗口上方。");
    }

    private void ShowError(string message) { Notice.Text = message; Notice.SetResourceReference(TextBlock.ForegroundProperty, "ErrorText"); NoticeBox.SetResourceReference(Border.BackgroundProperty, "Error"); NoticeBox.Visibility = Visibility.Visible; }
    private void ShowMessage(string message) { Notice.Text = message; Notice.SetResourceReference(TextBlock.ForegroundProperty, "SuccessText"); NoticeBox.SetResourceReference(Border.BackgroundProperty, "Success"); NoticeBox.Visibility = Visibility.Visible; }
    private Button ActionButton(string text, Func<Task> action)
    {
        var button = new Button { Content = text };
        if (text.StartsWith("保存", StringComparison.Ordinal) || text == "用于全部会话") button.SetResourceReference(StyleProperty, "PrimaryButton");
        button.Click += async (_, _) => { if (colorReview) return; button.IsEnabled = false; try { await action(); } catch (Exception e) { ShowError(e.Message); } finally { button.IsEnabled = true; } };
        return button;
    }

    private static DataGrid BuildTable(JsonElement rows, string[] keys)
    {
        var table = new DataTable(); foreach (string key in keys) table.Columns.Add(key);
        foreach (var row in rows.EnumerateArray()) table.Rows.Add(keys.Select(k => (object)(k is "created" or "updated" or "latest" ? When(row, k) : Text(row, k))).ToArray());
        var grid = new DataGrid { ItemsSource = table.DefaultView, ColumnWidth = new DataGridLength(1, DataGridLengthUnitType.Star) };
        grid.AutoGeneratingColumn += (_, e) => { e.Column.Header = Labels.GetValueOrDefault(e.PropertyName, e.PropertyName); e.Column.MinWidth = 90; e.Column.MaxWidth = e.PropertyName is "text" or "title" or "payload" ? 420 : 230; };
        return grid;
    }
    private static readonly Dictionary<string, string> Labels = new()
    {
        ["detail"]="详情",
        ["id"]="编号", ["title"]="内容", ["text"]="正文", ["status"]="状态", ["owner"]="归属用户", ["scope"]="来源会话", ["created"]="时间", ["updated"]="更新时间", ["latest"]="最近消息", ["name"]="名称", ["count"]="消息数", ["size"]="字节", ["kind"]="类型", ["visibility"]="可见范围", ["level"]="级别", ["model"]="模型", ["source"]="调用来源", ["input"]="输入 Token", ["output"]="输出 Token", ["reason"]="变化原因", ["error"]="失败原因", ["payload"]="详情", ["task_id"]="关联任务", ["bot"]="机器人", ["sender"]="发言人", ["quote_id"]="引用消息", ["topic"]="话题", ["dimensions"]="人格维度", ["enabled"]="启用"
    };
    public sealed record Choice(string Value, string Label) { public override string ToString() => Label; }
    private static string Selected(DataGrid grid, string key) => grid.SelectedItem is DataRowView row ? row[key]?.ToString() ?? "" : throw new InvalidOperationException("请先选中一条记录");
    private static TextBlock Caption(string text) => Theme.Bind(new TextBlock { Text = text, Margin = new Thickness(0, 6, 0, 18), LineHeight = 24 }, (TextBlock.ForegroundProperty, "Muted"));
    private static ScrollViewer Scroll(FrameworkElement content) => new() { Content = content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
    private static Border Panel(FrameworkElement content) => Theme.Bind(new Border { Child = content, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(16), Padding = new Thickness(24), Margin = new Thickness(0, 0, 12, 18) }, (Border.BackgroundProperty, "Panel"), (Border.BorderBrushProperty, "Line"));
    private static FrameworkElement Paragraph(string title, string body)
    {
        var stack = new StackPanel(); stack.Children.Add(new TextBlock { Text = title, FontSize = 20, FontWeight = FontWeights.SemiBold }); stack.Children.Add(Caption(body)); return stack;
    }
    private static FrameworkElement Card(string label, string value, string hint)
    {
        var stack = new StackPanel(); stack.Children.Add(Caption(label)); stack.Children.Add(new TextBlock { Text = value, FontSize = 24, FontWeight = FontWeights.SemiBold }); stack.Children.Add(Caption(hint)); return Panel(stack);
    }
    private static string Text(JsonElement row, string key, string fallback = "") => row.TryGetProperty(key, out var value) && value.ValueKind != JsonValueKind.Null && value.ToString().Length > 0 ? value.ToString() : fallback;
    private static long Number(JsonElement row, string key) => row.TryGetProperty(key, out var value) && value.TryGetInt64(out long n) ? n : 0;
    private static string Size(long bytes) => bytes >= 1073741824 ? $"{bytes / 1073741824.0:F1} GB" : $"{bytes / 1048576.0:F0} MB";
    private static string When(JsonElement row, string key)
    {
        if (!row.TryGetProperty(key, out var value) || !value.TryGetDouble(out double seconds)) return "";
        try { return DateTimeOffset.FromUnixTimeMilliseconds((long)(seconds * 1000)).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss"); }
        catch (ArgumentOutOfRangeException) { return ""; }
    }
}
