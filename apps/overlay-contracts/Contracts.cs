namespace Pangu.Overlay.Contracts;
public enum OverlayState { Hidden, Ambient, Listening, Thinking, Executing, Result, Approval, Error, Degraded }
public sealed record DisplayRequest(string Id, string Title, string ContentType, string Payload, string PrivacyClassification);
public sealed record DisplayScene(string Id, int Version, OverlayState State, string Renderer, string AccessibilityLabel);
public sealed record OverlayAcknowledgement(string SceneId, int Version, bool Visible, string? Error = null);
