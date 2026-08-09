using System.Diagnostics;
using System.Net.Http.Json;
using System.Security.Principal;

namespace Pangu.SessionAgent;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        var sid = WindowsIdentity.GetCurrent().User?.Value ?? Environment.UserName;
        using var mutex = new Mutex(true, $"Local\\PanguSessionAgent-{sid}", out var first);
        if (!first)
        {
            Console.Error.WriteLine("PANGU session agent already running.");
            return;
        }

        using var context = new SessionAgentContext();
        Application.Run(context);
    }
}

internal sealed class ManagedProcess : IDisposable
{
    private readonly Func<ProcessStartInfo?> _factory;
    private readonly string _name;
    private Process? _process;
    private DateTimeOffset _nextRestart = DateTimeOffset.MinValue;
    private int _failures;
    private bool _stopping;

    public ManagedProcess(string name, Func<ProcessStartInfo?> factory)
    {
        _name = name;
        _factory = factory;
    }

    public bool Running => _process is { HasExited: false };
    public int? ProcessId => Running ? _process!.Id : null;
    public string Status => Running ? $"running (PID {_process!.Id})" : "stopped";

    public bool EnsureRunning()
    {
        if (_stopping || Running || DateTimeOffset.UtcNow < _nextRestart)
            return Running;
        var info = _factory();
        if (info is null)
            return false;
        try
        {
            _process?.Dispose();
            _process = Process.Start(info);
            if (_process is null)
                throw new InvalidOperationException($"Failed to start {_name}.");
            _process.EnableRaisingEvents = true;
            _process.Exited += (_, _) => RecordFailure();
            _failures = 0;
            return true;
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            RecordFailure();
            return false;
        }
    }

    private void RecordFailure()
    {
        if (_stopping)
            return;
        _failures = Math.Min(_failures + 1, 6);
        var seconds = Math.Min(60, Math.Pow(2, _failures));
        _nextRestart = DateTimeOffset.UtcNow.AddSeconds(seconds);
    }

    public void Restart()
    {
        Stop(false);
        _stopping = false;
        _failures = 0;
        _nextRestart = DateTimeOffset.MinValue;
        EnsureRunning();
    }

    public void Stop(bool permanent = true)
    {
        _stopping = permanent;
        var process = _process;
        if (process is null)
            return;
        try
        {
            if (!process.HasExited)
            {
                if (process.CloseMainWindow() && process.WaitForExit(1500))
                    return;
                process.Kill(entireProcessTree: true);
                process.WaitForExit(2000);
            }
        }
        catch (InvalidOperationException)
        {
            // Already exited.
        }
        finally
        {
            process.Dispose();
            _process = null;
        }
    }

    public void Dispose() => Stop();
}

internal sealed class SessionAgentContext : ApplicationContext, IDisposable
{
    private readonly NotifyIcon _tray;
    private readonly System.Windows.Forms.Timer _timer;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(1) };
    private readonly ManagedProcess _backend;
    private readonly ManagedProcess _overlay;
    private readonly string _root;
    private bool _disposed;
    private bool _backendHealthy;

    public SessionAgentContext()
    {
        _root = ResolveRoot();
        _backend = new ManagedProcess("backend", BackendStartInfo);
        _overlay = new ManagedProcess("overlay", OverlayStartInfo);

        var menu = new ContextMenuStrip();
        var status = new ToolStripMenuItem("Status") { Enabled = false };
        menu.Items.Add(status);
        menu.Items.Add("Open PANGU folder", null, (_, _) => OpenRoot());
        menu.Items.Add("Restart PANGU", null, (_, _) => RestartAll());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Exit PANGU", null, (_, _) => ExitPangu());

        _tray = new NotifyIcon
        {
            Text = "PANGU Session Agent",
            Icon = SystemIcons.Information,
            Visible = true,
            ContextMenuStrip = menu,
        };
        _tray.DoubleClick += (_, _) => ShowStatus();

        _timer = new System.Windows.Forms.Timer { Interval = 2000 };
        _timer.Tick += async (_, _) =>
        {
            await SuperviseAsync();
            status.Text = StatusText();
            _tray.Text = TrimTrayText($"PANGU · {( _backendHealthy ? "ready" : "starting" )}");
        };
        _timer.Start();
        _ = SuperviseAsync();
    }

    private static string TrimTrayText(string text) => text.Length <= 63 ? text : text[..63];

    private string ResolveRoot()
    {
        var configured = Environment.GetEnvironmentVariable("PANGU_ROOT");
        if (!string.IsNullOrWhiteSpace(configured) && Directory.Exists(configured))
            return Path.GetFullPath(configured);
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; current is not null && i < 8; i++, current = current.Parent)
        {
            if (File.Exists(Path.Combine(current.FullName, "pyproject.toml")) &&
                Directory.Exists(Path.Combine(current.FullName, "src", "pangu")))
                return current.FullName;
        }
        return AppContext.BaseDirectory;
    }

    private ProcessStartInfo? BackendStartInfo()
    {
        var configured = Environment.GetEnvironmentVariable("PANGU_BACKEND_EXECUTABLE");
        var python = !string.IsNullOrWhiteSpace(configured)
            ? configured
            : Path.Combine(_root, ".venv", "Scripts", "python.exe");
        if (!File.Exists(python))
            return null;
        return new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-m uvicorn apps.backend.main:app --host 127.0.0.1 --port 8765",
            WorkingDirectory = _root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = false,
            RedirectStandardError = false,
        };
    }

    private ProcessStartInfo? OverlayStartInfo()
    {
        var configured = Environment.GetEnvironmentVariable("PANGU_OVERLAY_EXECUTABLE");
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured))
            return new ProcessStartInfo(configured) { UseShellExecute = false, WorkingDirectory = _root };

        var packaged = Path.Combine(AppContext.BaseDirectory, "Pangu.OverlayHost.exe");
        if (File.Exists(packaged))
            return new ProcessStartInfo(packaged) { UseShellExecute = false, WorkingDirectory = _root };

        var project = Path.Combine(_root, "apps", "overlay-host", "Pangu.OverlayHost.csproj");
        if (!File.Exists(project))
            return null;
        return new ProcessStartInfo
        {
            FileName = "dotnet",
            Arguments = $"run --no-launch-profile --project \"{project}\"",
            WorkingDirectory = _root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
    }

    private async Task<bool> BackendHealthyAsync()
    {
        try
        {
            using var response = await _http.GetAsync("http://127.0.0.1:8765/health");
            return response.IsSuccessStatusCode;
        }
        catch (HttpRequestException)
        {
            return false;
        }
        catch (TaskCanceledException)
        {
            return false;
        }
    }

    private async Task SuperviseAsync()
    {
        if (_disposed)
            return;
        _backendHealthy = await BackendHealthyAsync();
        if (!_backendHealthy)
            _backend.EnsureRunning();
        _overlay.EnsureRunning();
    }

    private string StatusText() =>
        $"Backend: {(_backendHealthy ? "healthy" : _backend.Status)} · Overlay: {_overlay.Status}";

    private void ShowStatus()
    {
        _tray.ShowBalloonTip(
            2500,
            "PANGU status",
            $"{StatusText()}\nRoot: {_root}",
            _backendHealthy ? ToolTipIcon.Info : ToolTipIcon.Warning);
    }

    private void OpenRoot()
    {
        if (!Directory.Exists(_root))
            return;
        Process.Start(new ProcessStartInfo("explorer.exe", $"\"{_root}\"") { UseShellExecute = true });
    }

    private void RestartAll()
    {
        _backend.Restart();
        _overlay.Restart();
        _ = SuperviseAsync();
    }

    private void ExitPangu()
    {
        _timer.Stop();
        _backend.Dispose();
        _overlay.Dispose();
        _tray.Visible = false;
        ExitThread();
    }

    protected override void ExitThreadCore()
    {
        Dispose();
        base.ExitThreadCore();
    }

    public new void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        _timer.Stop();
        _timer.Dispose();
        _backend.Dispose();
        _overlay.Dispose();
        _http.Dispose();
        _tray.Visible = false;
        _tray.Dispose();
        base.Dispose();
    }
}
