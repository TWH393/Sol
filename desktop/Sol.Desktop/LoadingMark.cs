using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;

namespace Sol.Desktop;

/// <summary>Local, theme-aware startup motion. One variant per desktop lifetime.</summary>
public sealed class LoadingMark : Grid
{
    private readonly bool orbit = Random.Shared.Next(2) == 0;
    private readonly Rectangle logo;
    private readonly RotateTransform rotation = new();
    private readonly ScaleTransform breath = new(1, 1);
    private readonly Ellipse[] dots = new Ellipse[3];
    private readonly TranslateTransform[] dotMotion = new TranslateTransform[3];
    private bool running;

    public LoadingMark()
    {
        Width = 148; Height = 148;
        IsHitTestVisible = false;
        var image = new BitmapImage(new Uri("pack://application:,,,/Assets/sol-loading.png"));
        logo = new Rectangle
        {
            Width = orbit ? 120 : 144, Height = orbit ? 90 : 108,
            OpacityMask = new ImageBrush(image) { Stretch = Stretch.Uniform },
            RenderTransform = breath, RenderTransformOrigin = new Point(.5, .5),
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center
        };
        logo.SetResourceReference(Shape.FillProperty, "Accent");
        if (orbit)
        {
            var track = new Ellipse { Width = 142, Height = 142, StrokeThickness = 1.5 };
            track.SetResourceReference(Shape.StrokeProperty, "Line");
            Children.Add(track);
            var arc = new Path
            {
                Width = 148, Height = 148, StrokeThickness = 1.5,
                StrokeStartLineCap = PenLineCap.Round, StrokeEndLineCap = PenLineCap.Round,
                Data = Geometry.Parse("M 74,3 A 71,71 0 0 1 135.49,38.5"),
                RenderTransform = rotation, RenderTransformOrigin = new Point(.5, .5)
            };
            arc.SetResourceReference(Shape.StrokeProperty, "Accent"); Children.Add(arc);
        }
        else
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Bottom };
            for (int i = 0; i < dots.Length; i++)
            {
                dotMotion[i] = new TranslateTransform();
                dots[i] = new Ellipse { Width = 5, Height = 5, Margin = new Thickness(3.5, 0, 3.5, 0), RenderTransform = dotMotion[i] };
                dots[i].SetResourceReference(Shape.FillProperty, "Accent"); row.Children.Add(dots[i]);
            }
            Children.Add(row);
        }
        Children.Add(logo);
        IsVisibleChanged += (_, _) => UpdateMotion();
        Loaded += (_, _) => UpdateMotion();
        Unloaded += (_, _) => StopMotion();
    }

    public void SetRunning(bool value) { if (running == value) return; running = value; UpdateMotion(); }

    private void StopMotion()
    {
        rotation.BeginAnimation(RotateTransform.AngleProperty, null);
        breath.BeginAnimation(ScaleTransform.ScaleXProperty, null);
        breath.BeginAnimation(ScaleTransform.ScaleYProperty, null);
        logo.BeginAnimation(OpacityProperty, null);
        for (int i = 0; i < dots.Length; i++)
            if (dotMotion[i] != null)
            {
                dotMotion[i].BeginAnimation(TranslateTransform.YProperty, null);
                dots[i].BeginAnimation(OpacityProperty, null);
            }
    }

    private void UpdateMotion()
    {
        StopMotion();
        if (!running || !IsVisible || !IsLoaded || !SystemParameters.ClientAreaAnimation) return;
        DoubleAnimation Breathe(double from, double to, double seconds) => new(from, to, TimeSpan.FromSeconds(seconds))
        {
            AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut }
        };
        breath.BeginAnimation(ScaleTransform.ScaleXProperty, Breathe(.98, 1.02, 1.9));
        breath.BeginAnimation(ScaleTransform.ScaleYProperty, Breathe(.98, 1.02, 1.9));
        logo.BeginAnimation(OpacityProperty, Breathe(.82, 1, 1.9));
        if (orbit) rotation.BeginAnimation(RotateTransform.AngleProperty, new DoubleAnimation(0, 360, TimeSpan.FromSeconds(4.8)) { RepeatBehavior = RepeatBehavior.Forever });
        else for (int i = 0; i < dots.Length; i++)
        {
            var bounce = Breathe(0, -2, .6); bounce.BeginTime = TimeSpan.FromSeconds(i * .2);
            dotMotion[i].BeginAnimation(TranslateTransform.YProperty, bounce);
            var fade = Breathe(.3, 1, .6); fade.BeginTime = TimeSpan.FromSeconds(i * .2);
            dots[i].BeginAnimation(OpacityProperty, fade);
        }
    }
}
