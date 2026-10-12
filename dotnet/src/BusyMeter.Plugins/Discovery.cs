using System.Text.Json;

namespace BusyMeter.Plugins;

/// <summary>Where the API is. The host is always 127.0.0.1; only the port varies.</summary>
/// <param name="Port">The TCP port.</param>
/// <param name="FromFile">The port came from the app's discovery file.</param>
/// <param name="Explicit">The caller named the port.</param>
public readonly record struct Endpoint(int Port, bool FromFile, bool Explicit = false)
{
    /// <summary>True when the port came from the app's discovery file (or the caller named it). Tokens go only there.</summary>
    public bool Trusted => FromFile || Explicit;

    /// <summary><c>http://127.0.0.1:&lt;port&gt;/v1/</c>.</summary>
    public string BaseUrl => $"http://127.0.0.1:{Port}/v1/";
}

/// <summary>Finds the port from <c>%LOCALAPPDATA%\BusyMeter\plugin-api.json</c> (written by the app only while it listens).</summary>
public static class Discovery
{
    /// <summary>The port used when there is no discovery file.</summary>
    public const int DefaultPort = 47820;

    /// <summary>The API version this client speaks.</summary>
    public const int ApiVersion = 1;

    /// <summary><c>%LOCALAPPDATA%\BusyMeter\plugin-api.json</c>, or <c>BUSYMETER_DISCOVERY_FILE</c> when set.</summary>
    public static string DiscoveryPath()
    {
        var overridePath = Environment.GetEnvironmentVariable("BUSYMETER_DISCOVERY_FILE");
        if (!string.IsNullOrEmpty(overridePath))
            return overridePath;
        var local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (string.IsNullOrEmpty(local))
            local = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "AppData", "Local");
        return Path.Combine(local, "BusyMeter", "plugin-api.json");
    }

    /// <summary>
    /// The port from the discovery file, or 47820 when there is none (the app may be off). Only the port is taken from
    /// the file (an integer 1024-65535); the host is always 127.0.0.1.
    /// </summary>
    public static Endpoint Discover(string? path = null)
    {
        try
        {
            var bytes = File.ReadAllBytes(path ?? DiscoveryPath());
            using var doc = JsonDocument.Parse(bytes);
            if (doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("port", out var port)
                && port.ValueKind == JsonValueKind.Number
                && port.TryGetInt32(out var value)
                && value is >= 1024 and <= 65535)
                return new Endpoint(value, FromFile: true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or ArgumentException or NotSupportedException)
        {
        }
        return new Endpoint(DefaultPort, FromFile: false);
    }
}
