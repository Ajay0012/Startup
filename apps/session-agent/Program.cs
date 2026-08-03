using System.Security.Principal;
var sid = WindowsIdentity.GetCurrent().User?.Value ?? Environment.UserName;
using var mutex = new Mutex(true, $"Local\\PanguSessionAgent-{sid}", out var first);
if (!first) { Console.Error.WriteLine("PANGU session agent already running."); return; }
Console.WriteLine("PANGU session agent mutex acquired. Backend supervision is configured by packaging deployment.");
