using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;

namespace Sol.Desktop;

/// <summary>Semantic colors. Every consumer uses DynamicResource so frozen brushes cannot leave stale colors.</summary>
public static class Theme
{
    [DllImport("dwmapi.dll")] private static extern int DwmSetWindowAttribute(IntPtr handle, int attribute, ref int value, int size);
    public static string Current { get; private set; } = "";
    public static bool IsDark => Current == "dark";
    public static event Action? Changed;

    // Each pair is warm light / dark. Never invert photographs or content previews.
    private static readonly Dictionary<string, (string Light, string Dark)> Palette = new()
    {
        ["Surface"] = ("#FAF8F3", "#212121"), ["Sidebar"] = ("#EFEAE0", "#171717"),
        ["Panel"] = ("#FFFDF9", "#292929"), ["Text"] = ("#302B25", "#ECECEC"),
        ["Muted"] = ("#685F54", "#C1BBB5"), ["Subtle"] = ("#70665A", "#B0A9A2"),
        ["Line"] = ("#E2DBCF", "#514B45"), ["Input"] = ("#F5F1E9", "#303030"),
        ["Button"] = ("#F2EDE4", "#333333"), ["ButtonHover"] = ("#E9E0D3", "#414141"),
        ["Accent"] = ("#B75D3D", "#F0A07A"), ["AccentHover"] = ("#A34E31", "#D88963"),
        ["AccentLabel"] = ("#97472D", "#F0A07A"),
        ["AccentText"] = ("#FFFFFF", "#211B17"), ["NavActive"] = ("#EADACB", "#39302B"),
        ["TableRow"] = ("#FFFDF9", "#252525"), ["TableAlternate"] = ("#F6F2EA", "#292929"),
        ["TableHeader"] = ("#EEE7DB", "#303030"), ["Selection"] = ("#EED7C6", "#544035"),
        ["Notice"] = ("#FAEDD6", "#3A3021"), ["NoticeText"] = ("#805321", "#F0D09A"),
        ["Error"] = ("#F8E5DF", "#442B2B"), ["ErrorText"] = ("#9B3C2D", "#F1B0A4"),
        ["Success"] = ("#E9EFE2", "#2B3A33"), ["SuccessText"] = ("#43643B", "#A8D4B7"),
        ["ScrollTrack"] = ("#F1ECE3", "#252321"), ["ScrollThumb"] = ("#B2A390", "#776F68"),
        ["ScrollHover"] = ("#8E7E6C", "#A3988D")
    };

    public static string ReadPreference(string dataRoot)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dataRoot, "settings.json")));
            return doc.RootElement.GetProperty("app").TryGetProperty("theme", out var value) && value.GetString() == "dark" ? "dark" : "light";
        }
        catch (Exception e) when (e is IOException or JsonException or KeyNotFoundException) { return "light"; }
    }

    public static void Apply(string name)
    {
        name = name == "dark" ? "dark" : "light";
        if (Current == name) return;
        Current = name;
        var resources = Application.Current.Resources;
        foreach (var (key, colors) in Palette)
        {
            var color = (Color)ColorConverter.ConvertFromString(IsDark ? colors.Dark : colors.Light);
            var brush = new SolidColorBrush(color);
            brush.Freeze();
            resources[key] = brush;
        }
        resources[SystemColors.WindowBrushKey] = resources["Surface"];
        resources[SystemColors.WindowTextBrushKey] = resources["Text"];
        resources[SystemColors.ControlBrushKey] = resources["Input"];
        resources[SystemColors.ControlTextBrushKey] = resources["Text"];
        resources[SystemColors.HighlightBrushKey] = resources["Selection"];
        resources[SystemColors.HighlightTextBrushKey] = resources["Text"];
        resources[SystemColors.InactiveSelectionHighlightBrushKey] = resources["Selection"];
        resources[SystemColors.InactiveSelectionHighlightTextBrushKey] = resources["Text"];
        resources[SystemColors.GrayTextBrushKey] = resources["Muted"];
        resources["SolLogo"] = resources[IsDark ? "SolLogoDark" : "SolLogoLight"];
        resources["SolWindowIcon"] = resources["SolAppIcon"];
        Changed?.Invoke();
    }

    public static T Bind<T>(T element, params (DependencyProperty Property, string Key)[] colors) where T : FrameworkElement
    {
        foreach (var (property, key) in colors) element.SetResourceReference(property, key);
        return element;
    }

    public static void ApplyWindowChrome(Window window)
    {
        var handle = new WindowInteropHelper(window).Handle;
        int dark = IsDark ? 1 : 0;
        if (handle != IntPtr.Zero) DwmSetWindowAttribute(handle, 20, ref dark, sizeof(int));
    }
}
