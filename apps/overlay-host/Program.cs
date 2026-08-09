using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using Pangu.Overlay.Contracts;

namespace Pangu.OverlayHost;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        var interactive = string.Equals(
            Environment.GetEnvironmentVariable("PANGU_OVERLAY_INTERACTIVE"),
            "true",
            StringComparison.OrdinalIgnoreCase);
        var forms = Screen.AllScreens.Select(screen => new OverlayForm(screen, interactive)).ToArray();
        if (forms.Length == 0)
        {
            Console.Error.WriteLine("No Windows display is available for the PANGU overlay.");
            return;
        }
        Application.Run(new OverlayApplicationContext(forms));
    }
}

internal sealed class OverlayApplicationContext : ApplicationContext
{
    private readonly OverlayForm[] _forms;
    private int _remaining;

    public OverlayApplicationContext(OverlayForm[] forms)
    {
        _forms = forms;
        _remaining = forms.Length;
        foreach (var form in _forms)
        {
            form.FormClosed += (_, _) =>
            {
                if (Interlocked.Decrement(ref _remaining) == 0)
                    ExitThread();
            };
            form.Show();
        }
    }

    protected override void ExitThreadCore()
    {
        foreach (var form in _forms)
        {
            if (!form.IsDisposed)
                form.Close();
        }
        base.ExitThreadCore();
    }
}

internal sealed class OverlayForm : Form
{
    private const int WsExTransparent = 0x00000020;
    private const int WsExToolWindow = 0x00000080;
    private const int WsExNoActivate = 0x08000000;
    private const int WsExLayered = 0x00080000;
    private readonly bool _interactive;
    private readonly System.Windows.Forms.Timer _animationTimer;
    private readonly DisplayScene _scene;
    private float _phase;

    public OverlayForm(Screen screen, bool interactive)
    {
        _interactive = interactive;
        _scene = new DisplayScene(
            $"ambient-{screen.DeviceName}",
            1,
            OverlayState.Ambient,
            "NativeWinFormsHudRenderer",
            "PANGU ambient status overlay");

        Name = $"PanguOverlay-{screen.DeviceName}";
        Text = "PANGU Overlay";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        Bounds = screen.Bounds;
        TopMost = true;
        ShowInTaskbar = false;
        BackColor = Color.Magenta;
        TransparencyKey = Color.Magenta;
        DoubleBuffered = true;
        AutoScaleMode = AutoScaleMode.Dpi;

        _animationTimer = new System.Windows.Forms.Timer { Interval = 33 };
        _animationTimer.Tick += (_, _) =>
        {
            _phase = (_phase + 0.025f) % 1f;
            Invalidate();
        };
        _animationTimer.Start();
    }

    protected override bool ShowWithoutActivation => !_interactive;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.ExStyle |= WsExToolWindow | WsExLayered;
            if (!_interactive)
                cp.ExStyle |= WsExTransparent | WsExNoActivate;
            return cp;
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;

        var scale = DeviceDpi / 96f;
        var margin = (int)(24 * scale);
        var panelWidth = (int)(360 * scale);
        var panelHeight = (int)(132 * scale);
        var panel = new Rectangle(ClientSize.Width - panelWidth - margin, margin, panelWidth, panelHeight);
        using var panelPath = RoundedRect(panel, 18 * scale);
        using var panelBrush = new SolidBrush(Color.FromArgb(205, 8, 18, 26));
        using var borderPen = new Pen(Color.FromArgb(210, 90, 220, 255), Math.Max(1f, 1.5f * scale));
        g.FillPath(panelBrush, panelPath);
        g.DrawPath(borderPen, panelPath);

        var center = new PointF(panel.Left + 54 * scale, panel.Top + panel.Height / 2f);
        var pulse = 18f * scale + 4f * scale * (float)Math.Sin(_phase * Math.PI * 2);
        using var haloPen = new Pen(Color.FromArgb(110, 80, 220, 255), Math.Max(1f, 2f * scale));
        using var coreBrush = new SolidBrush(Color.FromArgb(235, 105, 235, 255));
        g.DrawEllipse(haloPen, center.X - pulse, center.Y - pulse, pulse * 2, pulse * 2);
        g.FillEllipse(coreBrush, center.X - 7 * scale, center.Y - 7 * scale, 14 * scale, 14 * scale);

        using var titleFont = new Font("Segoe UI Semibold", 16 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var bodyFont = new Font("Segoe UI", 12 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var titleBrush = new SolidBrush(Color.FromArgb(245, 225, 250, 255));
        using var bodyBrush = new SolidBrush(Color.FromArgb(225, 165, 215, 232));
        g.DrawString("PANGU", titleFont, titleBrush, panel.Left + 92 * scale, panel.Top + 28 * scale);
        g.DrawString(
            _interactive ? "INTERACTION MODE" : "AMBIENT • LISTENING",
            bodyFont,
            bodyBrush,
            panel.Left + 92 * scale,
            panel.Top + 58 * scale);
        g.DrawString(
            $"HUD · {DeviceDpi} DPI · {_scene.Renderer}",
            bodyFont,
            bodyBrush,
            panel.Left + 92 * scale,
            panel.Top + 82 * scale);
    }

    protected override void WndProc(ref Message m)
    {
        const int WmMouseActivate = 0x0021;
        const int MaNoActivate = 3;
        if (!_interactive && m.Msg == WmMouseActivate)
        {
            m.Result = new IntPtr(MaNoActivate);
            return;
        }
        base.WndProc(ref m);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            _animationTimer.Dispose();
        base.Dispose(disposing);
    }

    private static GraphicsPath RoundedRect(RectangleF rect, float radius)
    {
        var path = new GraphicsPath();
        var diameter = radius * 2;
        path.AddArc(rect.Left, rect.Top, diameter, diameter, 180, 90);
        path.AddArc(rect.Right - diameter, rect.Top, diameter, diameter, 270, 90);
        path.AddArc(rect.Right - diameter, rect.Bottom - diameter, diameter, diameter, 0, 90);
        path.AddArc(rect.Left, rect.Bottom - diameter, diameter, diameter, 90, 90);
        path.CloseFigure();
        return path;
    }
}
