using Pangu.Overlay.Contracts;
Console.WriteLine("PANGU Overlay Host started in degraded native-contract mode.");
var scene = new DisplayScene("boot", 1, OverlayState.Hidden, "TextCardRenderer", "PANGU overlay status");
Console.WriteLine(new OverlayAcknowledgement(scene.Id, scene.Version, false, "Windows App SDK/WinUI workload is not installed."));
