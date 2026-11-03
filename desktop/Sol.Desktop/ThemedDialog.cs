using System;
using System.Windows;
using System.Windows.Controls;

namespace Sol.Desktop;

public static class ThemedDialog
{
    public static MessageBoxResult Show(Window? owner, string message, string title, MessageBoxButton buttons = MessageBoxButton.OK)
    {
        if (Theme.Current.Length == 0) Theme.Apply("light");
        var body = new DockPanel { Margin = new Thickness(24) };
        var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 22, 0, 0) };
        DockPanel.SetDock(actions, Dock.Bottom); body.Children.Add(actions);
        body.Children.Add(new ScrollViewer { MaxHeight = 380, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = new TextBlock { Text = message, LineHeight = 24 } });
        var dialog = new Window { Title = title, Width = 520, SizeToContent = SizeToContent.Height, ResizeMode = ResizeMode.NoResize, Content = body, WindowStartupLocation = owner is { IsVisible: true } ? WindowStartupLocation.CenterOwner : WindowStartupLocation.CenterScreen };
        if (owner is { IsVisible: true }) dialog.Owner = owner;
        dialog.SourceInitialized += (_, _) => Theme.ApplyWindowChrome(dialog);
        void UpdateChrome() => Theme.ApplyWindowChrome(dialog);
        MessageBoxResult result = MessageBoxResult.None;
        void Add(string label, MessageBoxResult value, bool primary, bool cancel = false)
        {
            var button = new Button { Content = label, MinWidth = 88, IsDefault = primary, IsCancel = cancel };
            if (primary) button.SetResourceReference(FrameworkElement.StyleProperty, "PrimaryButton");
            button.Click += (_, _) => { result = value; dialog.Close(); };
            actions.Children.Add(button);
        }
        if (buttons is MessageBoxButton.YesNo or MessageBoxButton.YesNoCancel) { Add("是", MessageBoxResult.Yes, true); Add("否", MessageBoxResult.No, false); }
        else Add("确定", MessageBoxResult.OK, true);
        if (buttons is MessageBoxButton.OKCancel or MessageBoxButton.YesNoCancel) Add("取消", MessageBoxResult.Cancel, false, true);
        Theme.Changed += UpdateChrome;
        try { dialog.ShowDialog(); }
        finally { Theme.Changed -= UpdateChrome; }
        return result;
    }
}
