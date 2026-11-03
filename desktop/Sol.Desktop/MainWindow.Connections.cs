using System;
using System.Globalization;
using System.Linq;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Sol.Desktop;

public partial class MainWindow
{
    private static string PageDescription(string key) => key switch
    {
        "overview" => "你的机器人、任务与记忆，一处掌握。",
        "settings" => "账号、聊天策略与运行环境，统一管理。",
        "models" => "GPT、DeepSeek 与 Gemini，在这里选择和切换。",
        "tasks" => "聊天照常，任务各自运行。",
        "files" => "保留原件与产物，随时找回。",
        "memories" => "记住重要的事，也记得它来自哪里。",
        "persona" => "有自己的性格，也保留成长的空间。",
        "usage" => "查看真实调用、消耗与额度。",
        "features" => "按需要启用能力。",
        "extensions" => "管理插件、技能与兼容状态。",
        "events" => "查看设置变化、执行记录与具体错误。",
        _ => "保留发言人、引用与话题的会话记录。"
    };

    private static FrameworkElement NavigationLabel(string key, string label)
    {
        string shape = key switch
        {
            "overview" or "features" => "M3,3 L10,3 10,10 3,10Z M14,3 L21,3 21,10 14,10Z M3,14 L10,14 10,21 3,21Z M14,14 L21,14 21,21 14,21Z",
            "tasks" => "M6,4 L19,4 19,21 4,21 4,4 6,4 M8,2 L15,2 15,6 8,6Z M8,11 L15,11 M8,16 L15,16",
            "files" => "M3,6 L9,6 12,9 21,9 21,21 3,21Z",
            "models" => "M2,8 L12,2 22,8 12,14Z M3,13 L12,18 21,13 M3,18 L12,23 21,18",
            "usage" => "M3,21 L22,21 M7,17 L7,11 M12,17 L12,3 M18,17 L18,8",
            "persona" => "M12,2 L15,9 22,12 15,15 12,22 9,15 2,12 9,9Z",
            "events" => "M2,12 L7,12 10,4 14,20 17,12 22,12",
            "memories" => "M8,4 L8,21 M16,4 L16,21 M8,5 C0,2 0,18 8,18 M16,5 C24,2 24,18 16,18 M8,10 L16,10 M8,15 L16,15",
            "extensions" => "M4,4 L10,4 10,8 15,8 15,4 21,4 21,10 17,10 17,15 21,15 21,21 15,21 15,17 10,17 10,21 4,21Z",
            _ => "M3,4 L21,4 21,18 8,18 3,22Z M7,9 L17,9 M7,13 L14,13"
        };
        var panel = new StackPanel { Orientation = Orientation.Horizontal };
        var icon = new System.Windows.Shapes.Path { Data = Geometry.Parse(shape), StrokeThickness = 1.3, Width = 17, Height = 17, Stretch = Stretch.Uniform, Margin = new Thickness(0, 0, 12, 0), VerticalAlignment = VerticalAlignment.Center };
        icon.SetBinding(System.Windows.Shapes.Shape.StrokeProperty, new System.Windows.Data.Binding("Foreground") { RelativeSource = new System.Windows.Data.RelativeSource(System.Windows.Data.RelativeSourceMode.FindAncestor, typeof(Button), 1) });
        panel.Children.Add(icon); panel.Children.Add(new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center });
        return panel;
    }

    private static StackPanel Field(string label, FrameworkElement control)
    {
        var body = new StackPanel(); body.Children.Add(new TextBlock { Text = label, FontSize = 13 }); body.Children.Add(control); return body;
    }

    private FrameworkElement ModelSources()
    {
        var grid = new System.Windows.Controls.Primitives.UniformGrid { Columns = 3 };
        foreach (var preset in new[] { "plan", "deepseek", "gemini" })
        {
            string name = preset == "plan" ? "GPT 套餐" : preset == "deepseek" ? "DeepSeek" : "Gemini";
            var body = new StackPanel();
            body.Children.Add(new TextBlock { Text = name, FontSize = 19, FontWeight = FontWeights.SemiBold });
            body.Children.Add(Caption(preset == "plan" ? "套餐账号接入，由 Sol 管理桥。" : "使用 API Key 与账号可用的模型。"));
            var buttons = new WrapPanel();
            buttons.Children.Add(ActionButton("添加模型", () => { ProviderDialog(preset); return Task.CompletedTask; }));
            if (preset == "plan") buttons.Children.Add(ActionButton("登录账号 ↗", async () =>
            {
                var data = await engine.PostAsync("plan/login", new { });
                OpenExternal(Text(data, "url"));
            }));
            body.Children.Add(buttons); grid.Children.Add(Panel(body));
        }
        return grid;
    }

    private void ProviderDialog(string preset)
    {
        string name = preset == "plan" ? "GPT 套餐" : preset == "deepseek" ? "DeepSeek" : "Gemini";
        string kind = preset == "plan" ? "plan" : preset == "gemini" ? "gemini" : "openai";
        var body = new StackPanel { Margin = new Thickness(28) };
        var window = new Window { Title = "Sol · 添加 " + name, Width = 610, Height = 700, MinHeight = 500, Owner = this, WindowStartupLocation = WindowStartupLocation.CenterOwner, Content = Scroll(body) };
        window.SourceInitialized += (_, _) => Theme.ApplyWindowChrome(window);
        body.Children.Add(Paragraph("添加 " + name, "获取可用型号后下拉选择，也可手动填写。GPT 套餐请先登录账号。"));
        var label = new TextBox { Text = name }; body.Children.Add(Field("显示名称", label));
        var model = new ComboBox { IsEditable = true, IsTextSearchEnabled = true }; body.Children.Add(Field("模型型号", model));
        var ident = new TextBox(); body.Children.Add(Field("配置 ID（留空自动生成）", ident));
        var address = new TextBox { Text = preset == "plan" ? "http://127.0.0.1:24762/v1" : preset == "deepseek" ? "https://api.deepseek.com/v1" : "https://generativelanguage.googleapis.com" }; body.Children.Add(Field("API 地址", address));
        var key = new PasswordBox();
        if (preset != "plan") body.Children.Add(Field("API Key", key));
        var efforts = new TextBox(); body.Children.Add(Field("支持的思考强度（逗号分隔，可留空）", efforts));
        body.Children.Add(Caption("按服务支持的参数填写，例如 low,medium,high；未知时留空使用模型默认。"));
        var error = Theme.Bind(new TextBlock { Margin = new Thickness(0, 0, 0, 12) }, (TextBlock.ForegroundProperty, "ErrorText")); body.Children.Add(error);
        var catalogStatus = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 12) }; body.Children.Add(catalogStatus);
        var fetch = new Button { Content = "获取模型列表" }; body.Children.Add(fetch);
        body.Children.Add(Caption("只读取可选型号，不发送测试问题。"));
        JsonArray? catalog = null;
        using var catalogLifetime = new System.Threading.CancellationTokenSource();
        window.Closed += (_, _) => catalogLifetime.Cancel();
        model.SelectionChanged += (_, _) =>
        {
            string chosen = model.SelectedItem as string ?? "";
            var match = catalog?.FirstOrDefault(m => m?["id"]?.GetValue<string>() == chosen);
            if (match != null) efforts.Text = string.Join(",", match["efforts"]!.AsArray().Select(e => e!.GetValue<string>()));
        };
        fetch.Click += async (_, _) =>
        {
            fetch.IsEnabled = false; error.Text = ""; catalogStatus.Text = "正在获取模型列表…";
            string requestedAddress = address.Text.Trim(), requestedKey = key.Password.Trim();
            try
            {
                var data = await engine.PostAsync("models/catalog", new { kind, base_url = requestedAddress, key = requestedKey }, catalogLifetime.Token);
                if (!window.IsVisible) return;
                if (address.Text.Trim() != requestedAddress || key.Password.Trim() != requestedKey) { catalogStatus.Text = "接口或密钥已变化，请重新获取模型列表。"; return; }
                catalog = JsonNode.Parse(data.GetRawText())!["models"]!.AsArray();
                string current = model.Text; model.Items.Clear();
                foreach (var item in catalog) model.Items.Add(item!["id"]!.GetValue<string>());
                model.Text = current;
                catalogStatus.Text = "已获取 " + catalog.Count + " 个型号，请在上方下拉选择。";
            }
            catch (OperationCanceledException) when (catalogLifetime.IsCancellationRequested) { }
            catch (Exception e) { error.Text = e.Message; catalogStatus.Text = "未取得列表，也可手动填写型号。"; }
            finally { fetch.IsEnabled = true; }
        };
        var save = new Button { Content = "保存模型", Style = (Style)FindResource("PrimaryButton") }; body.Children.Add(save);
        save.Click += async (_, _) =>
        {
            save.IsEnabled = false; error.Text = "";
            try
            {
                if (string.IsNullOrWhiteSpace(model.Text)) throw new InvalidOperationException("请填写准确的模型型号");
                if (preset != "plan" && string.IsNullOrWhiteSpace(key.Password)) throw new InvalidOperationException("请填写 API Key");
                var fresh = JsonNode.Parse((await engine.GetAsync("settings")).GetRawText())!;
                var list = fresh["models"]!["providers"]!.AsArray();
                string id = ident.Text.Trim(); if (id.Length == 0) id = preset + "-" + Regex.Replace(model.Text.Trim(), "[^a-zA-Z0-9_-]", "-");
                if (list.Any(p => p?["id"]?.GetValue<string>() == id)) throw new InvalidOperationException("配置 ID 已存在，请换一个名称");
                var p = new JsonObject { ["id"] = id, ["name"] = label.Text.Trim(), ["model"] = model.Text.Trim(), ["kind"] = kind, ["base_url"] = address.Text.Trim(), ["enabled"] = true, ["effort"] = "default", ["effort_mode"] = "fixed", ["efforts"] = new JsonArray(efforts.Text.Split(new[] { ',', '，', ' ' }, StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries).Select(s => (JsonNode?)JsonValue.Create(s)).ToArray()) };
                if (preset != "plan") p["key"] = key.Password.Trim();
                list.Add(p);
                string selected = fresh["models"]!["selected"]?.GetValue<string>() ?? "";
                await engine.PostAsync("settings", new { models = new { providers = list, selected = selected.Length == 0 ? id : selected } });
                key.Clear(); window.Close(); await RefreshAsync(true); ShowMessage("模型配置已保存；选择它即可用于新请求，尚未进行连接测试。");
            }
            catch (Exception e) { error.Text = e.Message; }
            finally { save.IsEnabled = true; }
        };
        window.ShowDialog(); key.Clear();
    }

    private FrameworkElement Settings()
    {
        var stack = new StackPanel(); var account = config.GetProperty("account");
        var identity = new StackPanel(); identity.Children.Add(Paragraph("机器人身份", "指定机器人与管理员；只接管这个账号。"));
        var qq = new TextBox { Text = Text(account, "qq"), MaxLength = 15 };
        var admins = new TextBox { Text = string.Join(", ", account.GetProperty("admins").EnumerateArray().Select(x => x.GetString())) };
        var fields = new System.Windows.Controls.Primitives.UniformGrid { Columns = 2 };
        var qqField = Field("机器人 QQ", qq); qqField.Margin = new Thickness(0, 0, 16, 0); fields.Children.Add(qqField); fields.Children.Add(Field("管理员 QQ（逗号分隔）", admins)); identity.Children.Add(fields);
        var enabled = new CheckBox { Content = "启用 QQ 接入", IsChecked = account.GetProperty("enabled").GetBoolean() };
        var autostart = new CheckBox { Content = "登录 Windows 后启动 Sol（收在托盘）", IsChecked = config.GetProperty("app").GetProperty("autostart").GetBoolean() };
        identity.Children.Add(enabled); identity.Children.Add(autostart);
        identity.Children.Add(ActionButton("保存账号与启动设置", async () =>
        {
            string number = qq.Text.Trim();
            string[] owners = admins.Text.Split(new[] { ',', '，', ';', '；', ' ' }, StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries);
            if ((number.Length > 0 && !number.All(char.IsAsciiDigit)) || owners.Any(x => !x.All(char.IsAsciiDigit)) || (enabled.IsChecked == true && number.Length == 0)) throw new InvalidOperationException("请填写有效的 QQ 数字账号");
            await engine.PostAsync("settings", new { account = new { qq = number, admins = owners, enabled = enabled.IsChecked == true }, app = new { autostart = autostart.IsChecked == true } });
            App.ConfigureAutostart(autostart.IsChecked == true, engine.Options); appliedAutostart = autostart.IsChecked == true;
            ShowMessage("账号已保存；点击登录 QQ 时会自动应用接入设置。");
        })); stack.Children.Add(Panel(identity));
        var login = new StackPanel(); login.Children.Add(Paragraph("QQ 登录与连接", "Sol自研接入，无需第三方授权Token。客户端适配完成后在此扫码和查看状态。"));
        login.Children.Add(ActionButton("登录机器人 QQ", () => { new QqLoginWindow(engine, this).ShowDialog(); return Task.CompletedTask; })); stack.Children.Add(Panel(login));
        if (config.TryGetProperty("chat", out var chat)) stack.Children.Add(ChatSettings(chat));
        var environment = new StackPanel(); environment.Children.Add(Paragraph("运行环境", "查看数据、日志或管理内部引擎。"));
        var controls = new WrapPanel();
        controls.Children.Add(ActionButton("打开数据目录", () => { OpenExternal(engine.Options.DataRoot); return Task.CompletedTask; }));
        controls.Children.Add(ActionButton("查看引擎日志", () => { OpenLog(); return Task.CompletedTask; }));
        controls.Children.Add(ActionButton("完整设置 ↗", () => { OpenBrowser("settings"); return Task.CompletedTask; })); environment.Children.Add(controls);
        var lifecycle = new WrapPanel { Margin = new Thickness(0, 16, 0, 0) };
        lifecycle.Children.Add(ActionButton("停止内部引擎", async () => { refreshTimer.Stop(); await engine.StopAsync(); PageContent.Content = EngineRecovery(); }));
        lifecycle.Children.Add(ActionButton("重启内部引擎", async () => { refreshTimer.Stop(); await engine.StopAsync(); await StartEngineAsync(); }));
        lifecycle.Children.Add(ActionButton("退出 Sol", app.ExitAsync)); environment.Children.Add(lifecycle);
        environment.Children.Add(Caption("数据目录：" + engine.Options.DataRoot)); stack.Children.Add(Panel(environment));
        return Scroll(stack);
    }

    private FrameworkElement ChatSettings(System.Text.Json.JsonElement chat)
    {
        var body = new StackPanel(); body.Children.Add(Paragraph("消息与气泡", "所有会话共用发送队列，按这里的间隔依次发送。"));
        var count = new TextBox { Text = Text(chat, "max_bubbles") };
        var intervals = chat.GetProperty("send_interval").EnumerateArray().ToArray();
        var minimum = new TextBox { Text = intervals[0].ToString() }; var maximum = new TextBox { Text = intervals[1].ToString() };
        var fields = new System.Windows.Controls.Primitives.UniformGrid { Columns = 3 };
        foreach (var field in new[] { Field("最多气泡数（0=不限）", count), Field("最短发送间隔（秒）", minimum), Field("最长发送间隔（秒）", maximum) }) { field.Margin = new Thickness(0, 0, 16, 0); fields.Children.Add(field); }
        body.Children.Add(fields); body.Children.Add(Caption("数量可以超过10；最短与最长相同即为固定间隔。"));
        body.Children.Add(ActionButton("保存气泡设置", async () =>
        {
            if (!System.Numerics.BigInteger.TryParse(count.Text, NumberStyles.None, CultureInfo.InvariantCulture, out var bubbles) || bubbles < 0) throw new InvalidOperationException("气泡数必须为非负整数，0表示不限");
            if (!double.TryParse(minimum.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out double min) || !double.TryParse(maximum.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out double max)) throw new InvalidOperationException("发送间隔请填写数字，小数使用点号");
            if (!double.IsFinite(min) || !double.IsFinite(max) || min < 0 || min > max || max > 60) throw new InvalidOperationException("间隔需为0～60秒，最短不能大于最长");
            var bubbleValue = JsonNode.Parse(bubbles.ToString(CultureInfo.InvariantCulture));
            await engine.PostAsync("settings", new { chat = new { max_bubbles = bubbleValue, send_interval = new[] { min, max } } }); ShowMessage("气泡设置已保存，后续发送按新设置执行。");
        })); return Panel(body);
    }
}
