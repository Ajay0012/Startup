using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Text.Json;
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
        var stateFile = Environment.GetEnvironmentVariable("PANGU_HUD_STATE_FILE");
        if (string.IsNullOrWhiteSpace(stateFile))
            stateFile = Path.Combine(Environment.CurrentDirectory, "runtime-data", "overlay", "state.json");

        var state = new HudStateProvider(stateFile);
        var forms = Screen.AllScreens.Select(screen => new OverlayForm(screen, interactive, state)).ToArray();
        if (forms.Length == 0)
        {
            Console.Error.WriteLine("No Windows display is available for the PANGU overlay.");
            return;
        }
        Application.Run(new OverlayApplicationContext(forms, state));
    }
}

internal sealed class OverlayApplicationContext : ApplicationContext
{
    private readonly OverlayForm[] _forms;
    private readonly HudStateProvider _state;
    private int _remaining;

    public OverlayApplicationContext(OverlayForm[] forms, HudStateProvider state)
    {
        _forms = forms;
        _state = state;
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
        _state.Dispose();
        base.ExitThreadCore();
    }
}

internal sealed record HudCard(string Title, string Value, string? Detail = null);
internal sealed record HudTarget(string Label, float X, float Y, float Width, float Height, float Confidence = 1f);
internal sealed record HudSnapshot(
    string Mode,
    string Status,
    string? Message,
    float AudioLevel,
    HudCard[] Cards,
    HudTarget? Target,
    DateTimeOffset UpdatedAt)
{
    public static HudSnapshot Ambient { get; } = new(
        "listening",
        "AMBIENT • LISTENING",
        null,
        0.08f,
        Array.Empty<HudCard>(),
        null,
        DateTimeOffset.UtcNow);
}

internal sealed class HudStateProvider : IDisposable
{
    private readonly string _path;
    private readonly System.Threading.Timer _timer;
    private readonly object _gate = new();
    private HudSnapshot _current = HudSnapshot.Ambient;
    private DateTime _lastWrite = DateTime.MinValue;

    public HudStateProvider(string path)
    {
        _path = path;
        _timer = new System.Threading.Timer(_ => Refresh(), null, TimeSpan.Zero, TimeSpan.FromMilliseconds(200));
    }

    public HudSnapshot Current
    {
        get { lock (_gate) return _current; }
    }

    private void Refresh()
    {
        try
        {
            if (!File.Exists(_path))
                return;
            var write = File.GetLastWriteTimeUtc(_path);
            if (write <= _lastWrite)
                return;
            var json = File.ReadAllText(_path);
            var snapshot = JsonSerializer.Deserialize<HudSnapshot>(json, new JsonSerializerOptions
            {
                PropertyNameCaseInsensitive = true
            });
            if (snapshot is null)
                return;
            snapshot = snapshot with
            {
                AudioLevel = Math.Clamp(snapshot.AudioLevel, 0f, 1f),
                Cards = snapshot.Cards?.Take(6).ToArray() ?? Array.Empty<HudCard>()
            };
            lock (_gate)
            {
                _current = snapshot;
                _lastWrite = write;
            }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        catch (JsonException) { }
    }

    public void Dispose() => _timer.Dispose();
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
    private readonly HudStateProvider _state;
    private readonly float[] _wave = new float[48];
    private readonly Random _random = new(17);
    private float _phase;

    public OverlayForm(Screen screen, bool interactive, HudStateProvider state)
    {
        _interactive = interactive;
        _state = state;
        _scene = new DisplayScene(
            $"ambient-{screen.DeviceName}",
            1,
            OverlayState.Ambient,
            "NativeWinFormsHudRenderer",
            "PANGU spatial status overlay");

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
            _phase = (_phase + 0.022f) % 1f;
            AdvanceWave(_state.Current.AudioLevel);
            Invalidate();
        };
        _animationTimer.Start();
    }

    private void AdvanceWave(float audioLevel)
    {
        Array.Copy(_wave, 1, _wave, 0, _wave.Length - 1);
        var oscillation = (float)(Math.Sin(_phase * Math.PI * 8) * 0.35 + Math.Sin(_phase * Math.PI * 3) * 0.2);
        var noise = (float)(_random.NextDouble() - 0.5) * 0.10f;
        _wave[^1] = Math.Clamp((oscillation + noise) * Math.Max(0.08f, audioLevel), -1f, 1f);
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
        var snapshot = _state.Current;
        var scale = DeviceDpi / 96f;
        DrawCorePanel(g, snapshot, scale);
        DrawContextCards(g, snapshot, scale);
        DrawGestureTarget(g, snapshot.Target, scale);
    }

    private void DrawCorePanel(Graphics g, HudSnapshot snapshot, float scale)
    {
        var margin = (int)(24 * scale);
        var panelWidth = (int)(430 * scale);
        var panelHeight = (int)(166 * scale);
        var panel = new Rectangle(ClientSize.Width - panelWidth - margin, margin, panelWidth, panelHeight);
        using var panelPath = RoundedRect(panel, 20 * scale);
        using var panelBrush = new SolidBrush(Color.FromArgb(218, 6, 15, 24));
        using var borderPen = new Pen(ModeColor(snapshot.Mode, 215), Math.Max(1f, 1.6f * scale));
        g.FillPath(panelBrush, panelPath);
        g.DrawPath(borderPen, panelPath);

        var center = new PointF(panel.Left + 58 * scale, panel.Top + 58 * scale);
        var pulse = 19f * scale + 5f * scale * (float)Math.Sin(_phase * Math.PI * 2);
        using var haloPen = new Pen(ModeColor(snapshot.Mode, 115), Math.Max(1f, 2f * scale));
        using var coreBrush = new SolidBrush(ModeColor(snapshot.Mode, 245));
        g.DrawEllipse(haloPen, center.X - pulse, center.Y - pulse, pulse * 2, pulse * 2);
        g.FillEllipse(coreBrush, center.X - 7 * scale, center.Y - 7 * scale, 14 * scale, 14 * scale);

        using var titleFont = new Font("Segoe UI Semibold", 18 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var bodyFont = new Font("Segoe UI", 12 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var smallFont = new Font("Segoe UI", 10 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var titleBrush = new SolidBrush(Color.FromArgb(248, 226, 250, 255));
        using var bodyBrush = new SolidBrush(Color.FromArgb(230, 166, 216, 235));
        using var dimBrush = new SolidBrush(Color.FromArgb(190, 125, 174, 196));

        g.DrawString("PANGU", titleFont, titleBrush, panel.Left + 98 * scale, panel.Top + 25 * scale);
        g.DrawString(snapshot.Status ?? "LISTENING", bodyFont, bodyBrush, panel.Left + 98 * scale, panel.Top + 57 * scale);
        if (!string.IsNullOrWhiteSpace(snapshot.Message))
        {
            var messageRect = new RectangleF(panel.Left + 98 * scale, panel.Top + 82 * scale, 302 * scale, 35 * scale);
            g.DrawString(snapshot.Message, smallFont, titleBrush, messageRect);
        }
        g.DrawString(
            $"{snapshot.Mode.ToUpperInvariant()} · {DeviceDpi} DPI · {_scene.Renderer}",
            smallFont,
            dimBrush,
            panel.Left + 98 * scale,
            panel.Top + 126 * scale);

        DrawWaveform(g, panel.Left + 22 * scale, panel.Bottom - 27 * scale, panelWidth - 44 * scale, 18 * scale, snapshot.Mode, scale);
    }

    private void DrawWaveform(Graphics g, float x, float centerY, float width, float height, string mode, float scale)
    {
        if (_wave.Length < 2)
            return;
        using var pen = new Pen(ModeColor(mode, 180), Math.Max(1f, 1.2f * scale));
        var step = width / (_wave.Length - 1);
        var points = new PointF[_wave.Length];
        for (var i = 0; i < _wave.Length; i++)
            points[i] = new PointF(x + i * step, centerY + _wave[i] * height);
        g.DrawLines(pen, points);
    }

    private void DrawContextCards(Graphics g, HudSnapshot snapshot, float scale)
    {
        if (snapshot.Cards.Length == 0)
            return;
        var margin = 24 * scale;
        var cardWidth = 205 * scale;
        var cardHeight = 72 * scale;
        var startX = ClientSize.Width - margin - cardWidth;
        var startY = margin + 182 * scale;
        using var titleFont = new Font("Segoe UI Semibold", 11 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var valueFont = new Font("Segoe UI", 13 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var detailFont = new Font("Segoe UI", 9 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var titleBrush = new SolidBrush(Color.FromArgb(190, 130, 190, 215));
        using var valueBrush = new SolidBrush(Color.FromArgb(245, 225, 249, 255));
        using var detailBrush = new SolidBrush(Color.FromArgb(170, 120, 170, 190));
        for (var i = 0; i < snapshot.Cards.Length; i++)
        {
            var row = i % 3;
            var column = i / 3;
            var rect = new RectangleF(startX - column * (cardWidth + 10 * scale), startY + row * (cardHeight + 10 * scale), cardWidth, cardHeight);
            using var path = RoundedRect(rect, 12 * scale);
            using var fill = new SolidBrush(Color.FromArgb(185, 7, 18, 27));
            using var border = new Pen(Color.FromArgb(100, 82, 200, 230), Math.Max(1f, scale));
            g.FillPath(fill, path);
            g.DrawPath(border, path);
            var card = snapshot.Cards[i];
            g.DrawString(card.Title, titleFont, titleBrush, rect.Left + 12 * scale, rect.Top + 8 * scale);
            g.DrawString(card.Value, valueFont, valueBrush, rect.Left + 12 * scale, rect.Top + 27 * scale);
            if (!string.IsNullOrWhiteSpace(card.Detail))
                g.DrawString(card.Detail, detailFont, detailBrush, rect.Left + 12 * scale, rect.Top + 51 * scale);
        }
    }

    private void DrawGestureTarget(Graphics g, HudTarget? target, float scale)
    {
        if (target is null || target.Confidence < 0.45f)
            return;
        var x = target.X * ClientSize.Width;
        var y = target.Y * ClientSize.Height;
        var width = Math.Max(30 * scale, target.Width * ClientSize.Width);
        var height = Math.Max(30 * scale, target.Height * ClientSize.Height);
        var rect = new RectangleF(x, y, width, height);
        using var pen = new Pen(Color.FromArgb((int)(120 + target.Confidence * 120), 100, 235, 255), Math.Max(1f, 1.8f * scale));
        using var font = new Font("Segoe UI Semibold", 10 * scale, FontStyle.Regular, GraphicsUnit.Pixel);
        using var brush = new SolidBrush(Color.FromArgb(235, 210, 248, 255));
        g.DrawRectangle(pen, rect.X, rect.Y, rect.Width, rect.Height);
        g.DrawString(target.Label, font, brush, rect.Left, Math.Max(0, rect.Top - 18 * scale));
    }

    private static Color ModeColor(string? mode, int alpha)
    {
        return mode?.ToLowerInvariant() switch
        {
            "speaking" => Color.FromArgb(alpha, 110, 240, 195),
            "thinking" => Color.FromArgb(alpha, 175, 145, 255),
            "error" => Color.FromArgb(alpha, 255, 105, 120),
            "warning" => Color.FromArgb(alpha, 255, 205, 105),
            _ => Color.FromArgb(alpha, 90, 220, 255),
        };
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