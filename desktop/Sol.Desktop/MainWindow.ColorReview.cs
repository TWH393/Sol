using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;

namespace Sol.Desktop;

public partial class MainWindow
{
    private readonly bool colorReview;

    public async Task OpenColorReviewAsync()
    {
        Title = "Sol · 配色预览（不连接机器人）";
        config = JsonSerializer.SerializeToElement(new
        {
            app = new { autostart = false, theme = Theme.Current },
            account = new { qq = "示例账号", admins = new[] { "示例管理员" }, enabled = false },
            models = new
            {
                selected = "color-preview",
                providers = new[] { new { id = "color-preview", name = "仅配色样例", model = "不调用模型", kind = "preview", effort_mode = "fixed", effort = "medium", efforts = new[] { "none", "low", "medium", "high" } } }
            },
            features = new Dictionary<string, bool> { ["chat"] = true, ["groups"] = true, ["memory"] = false, ["models"] = true, ["reuse"] = true, ["images"] = true, ["documents"] = true, ["python"] = true, ["video"] = true, ["tasks"] = true, ["usage"] = true, ["plugins"] = false }
        });
        state = JsonSerializer.SerializeToElement(new
        {
            online = false, model = "配色样例", account = new { qq = "示例账号", enabled = false },
            task_counts = new { running = 0, queued = 0 }, core = new { error = "" },
            resources = new { process_memory = 0, memory_used = 0, memory_total = 0 },
            file_count = new { n = 0 }, memory_count = 0,
            events = new[] { new { created = DateTimeOffset.Now.ToUnixTimeSeconds(), title = "界面颜色样例，不代表实际任务或用量" } }
        });
        SidebarStatus.Text = "仅配色 · 不连接引擎";
        Footer.Text = "仅检查颜色 · 样例数据 · 不连接QQ/模型 · 不保存配置";
        ReviewDialogButton.Visibility = Visibility.Visible;
        await NavigateAsync("overview");
    }

    private Task RenderColorReviewAsync(string page)
    {
        PageContent.Content = page switch
        {
            "overview" => Overview(), "models" => Models(), "features" => Features(), "settings" => Settings(),
            _ => Records(page, ColorRows())
        };
        Notice.Text = "配色预览：展示样例，只检查深浅色、选中、悬停和控件文字；不连接机器人或保存设置。";
        Notice.SetResourceReference(System.Windows.Controls.TextBlock.ForegroundProperty, "NoticeText");
        NoticeBox.SetResourceReference(System.Windows.Controls.Border.BackgroundProperty, "Notice");
        NoticeBox.Visibility = Visibility.Visible;
        return Task.CompletedTask;
    }

    private static JsonElement ColorRows()
    {
        var rows = new List<Dictionary<string, object>>();
        for (int i = 0; i < 2; i++)
        {
            rows.Add(new Dictionary<string, object>
            {
                ["id"] = "color-sample-" + i, ["title"] = i == 0 ? "普通文字与选中行样例" : "次行与长文本样例",
                ["text"] = "这是一条仅用于界面颜色检查的样例。Sample text 123。", ["owner"] = "示例用户", ["scope"] = "示例会话",
                ["bot"] = "样例", ["count"] = 0, ["status"] = i == 0 ? "running" : "failed", ["stage"] = "颜色样例",
                ["name"] = "配色样例.pdf", ["size"] = 0, ["task_id"] = "color-sample", ["source"] = "配色预览",
                ["created"] = DateTimeOffset.Now.ToUnixTimeSeconds(), ["updated"] = DateTimeOffset.Now.ToUnixTimeSeconds(), ["latest"] = DateTimeOffset.Now.ToUnixTimeSeconds(),
                ["kind"] = "样例", ["visibility"] = "配色预览", ["reason"] = "并非真实人格变化", ["dimensions"] = "{}",
                ["level"] = "info", ["detail"] = "仅颜色样例，没有执行操作", ["payload"] = "{}", ["error"] = "示例说明，不代表实际故障",
                ["model"] = "不调用模型", ["input"] = 0, ["output"] = 0, ["enabled"] = true
            });
        }
        return JsonSerializer.SerializeToElement(rows);
    }

    private void ReviewDialogClick(object sender, RoutedEventArgs e) => ThemedDialog.Show(this, "普通说明与按钮颜色样例。\n此弹窗不会执行任何操作。", "Sol · 弹窗配色", MessageBoxButton.OKCancel);
}
