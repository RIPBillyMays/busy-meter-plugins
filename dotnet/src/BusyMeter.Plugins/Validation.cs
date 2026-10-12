using System.Globalization;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace BusyMeter.Plugins;

/// <summary>A level the API takes: 0-6 stars (0 = lip accent only) or Flash (the red/blue look).</summary>
public readonly struct MeterLevel : IEquatable<MeterLevel>
{
    private readonly int _value; // 0-6, or 7 for Flash

    private MeterLevel(int value) => _value = value;

    /// <summary>The red/blue Flash look.</summary>
    public static MeterLevel Flash { get; } = new(7);

    /// <summary>True for Flash.</summary>
    public bool IsFlash => _value == 7;

    /// <summary>The star count (0-6), or null for Flash.</summary>
    public int? Stars => IsFlash ? null : _value;

    /// <summary>0-6 stars.</summary>
    /// <exception cref="ArgumentOutOfRangeException">Not 0-6.</exception>
    public static MeterLevel FromStars(int stars) =>
        stars is >= 0 and <= 6
            ? new MeterLevel(stars)
            : throw new ArgumentOutOfRangeException(nameof(stars), stars, "level must be 0-6 (stars; 0 = lip accent only) or \"flash\".");

    /// <summary>"flash" or "0".."6" (case and surrounding white space ignored).</summary>
    /// <exception cref="ArgumentException">Anything else.</exception>
    public static MeterLevel Parse(string text)
    {
        var t = (text ?? "").Trim().ToLowerInvariant();
        if (t == "flash")
            return Flash;
        if (t.Length > 0 && t.All(char.IsAsciiDigit) && int.TryParse(t, NumberStyles.None, CultureInfo.InvariantCulture, out var n) && n is >= 0 and <= 6)
            return new MeterLevel(n);
        throw new ArgumentException("level must be 0-6 (stars; 0 = lip accent only) or \"flash\".", nameof(text));
    }

    /// <summary>0-6 stars.</summary>
    public static implicit operator MeterLevel(int stars) => FromStars(stars);

    /// <summary>The JSON the API takes: a number 0-6 or the string "flash".</summary>
    public JsonNode ToJson() => IsFlash ? JsonValue.Create("flash") : JsonValue.Create(_value);

    /// <inheritdoc/>
    public override string ToString() => IsFlash ? "flash" : _value.ToString(CultureInfo.InvariantCulture);

    /// <inheritdoc/>
    public bool Equals(MeterLevel other) => _value == other._value;

    /// <inheritdoc/>
    public override bool Equals(object? obj) => obj is MeterLevel other && Equals(other);

    /// <inheritdoc/>
    public override int GetHashCode() => _value;

    /// <summary>Equal levels.</summary>
    public static bool operator ==(MeterLevel a, MeterLevel b) => a.Equals(b);

    /// <summary>Different levels.</summary>
    public static bool operator !=(MeterLevel a, MeterLevel b) => !a.Equals(b);
}

/// <summary>A signal the plugin declares when it registers.</summary>
/// <param name="Name">1-48 of a-z, 0-9 and '-', not starting with "level-".</param>
/// <param name="Label">What the user sees in the tray prompt and on Settings › Levels.</param>
/// <param name="Suggest">The level it maps to by default (none = ignored until the user maps it).</param>
public sealed record SignalDeclaration(string Name, string? Label = null, MeterLevel? Suggest = null)
{
    /// <summary>A signal with only a name.</summary>
    public static implicit operator SignalDeclaration(string name) => new(name);
}

/// <summary>The checks this client makes before it sends anything (same rules and messages as the Python client).</summary>
public static partial class Validation
{
    /// <summary>The longest TTL the API takes (10 minutes): refresh to keep a level longer.</summary>
    public const int MaxTtlSeconds = 600;

    /// <summary>What a plugin may ask for.</summary>
    public static IReadOnlyList<string> Wants { get; } = ["signals", "status", "taps", "effects"];

    /// <summary>The alert effects.</summary>
    public static IReadOnlyList<string> Effects { get; } = ["lip-pulse", "star-sweep", "flash-short"];

    [GeneratedRegex("^[a-z0-9.-]{3,64}$")]
    private static partial Regex IdPattern();

    [GeneratedRegex("^[a-z0-9-]{1,48}$")]
    private static partial Regex SignalPattern();

    [GeneratedRegex("^[A-Za-z0-9_-]{1,64}$")]
    private static partial Regex RequestIdPattern();

    [GeneratedRegex(@"^\s*([0-9]+(?:\.[0-9]+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)?\s*$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex DurationPattern();

    /// <summary>3-64 of a-z, 0-9, '.' and '-' (e.g. com.example.heartrate).</summary>
    public static bool IsValidPluginId(string? pluginId) => pluginId is not null && IdPattern().IsMatch(pluginId) && !pluginId.EndsWith('\n');

    /// <summary>A request id as the app makes them (base64url).</summary>
    public static bool IsValidRequestId(string? requestId) => requestId is not null && RequestIdPattern().IsMatch(requestId) && !requestId.EndsWith('\n');

    /// <summary>Seconds from "30", "30s", "10m", "1.5h". At least 1 second.</summary>
    /// <exception cref="ArgumentException">Unreadable, or under 1 second.</exception>
    public static int ParseDuration(string value)
    {
        var match = DurationPattern().Match(value ?? "");
        if (!match.Success)
            throw new ArgumentException($"Can't read the duration '{value}'; use e.g. 30s, 10m or 1h.", nameof(value));
        var number = double.Parse(match.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture);
        var unit = match.Groups[2].Success ? match.Groups[2].Value.ToLowerInvariant() : "s";
        return ParseDuration(number * (unit.StartsWith('h') ? 3600 : unit.StartsWith('m') ? 60 : 1));
    }

    /// <summary>Whole seconds (rounded half to even, like Python) from a number of seconds. At least 1 second.</summary>
    /// <exception cref="ArgumentException">Under 1 second (or not a number).</exception>
    public static int ParseDuration(double seconds)
    {
        if (!double.IsFinite(seconds) || seconds < 1)
            throw new ArgumentException("A duration must be at least 1 second.", nameof(seconds));
        var rounded = Math.Round(seconds, MidpointRounding.ToEven);
        return rounded >= int.MaxValue ? int.MaxValue : (int)rounded;
    }

    /// <summary>1-600 seconds.</summary>
    /// <exception cref="ArgumentOutOfRangeException">Outside 1-600.</exception>
    public static int CheckTtl(int ttlSeconds) =>
        ttlSeconds is >= 1 and <= MaxTtlSeconds
            ? ttlSeconds
            : throw new ArgumentOutOfRangeException(nameof(ttlSeconds), ttlSeconds, "ttl_seconds must be 1-600 (refresh before it runs out to keep a level longer).");

    /// <summary>Signals as the API wants them: <c>[{"name", "label"?, "suggest"?}]</c>.</summary>
    /// <exception cref="ArgumentException">A bad name, or more than 32.</exception>
    public static JsonArray SignalDeclarations(IEnumerable<SignalDeclaration>? signals)
    {
        var list = new JsonArray();
        foreach (var signal in signals ?? [])
        {
            var name = signal?.Name;
            if (name is null || !SignalPattern().IsMatch(name) || name.EndsWith('\n') || name.StartsWith("level-", StringComparison.Ordinal))
                throw new ArgumentException($"Signal name '{name}': use 1-48 of a-z, 0-9 and '-', not starting with 'level-'.", nameof(signals));
            var entry = new JsonObject { ["name"] = name };
            if (signal!.Label is not null)
                entry["label"] = signal.Label;
            if (signal.Suggest is MeterLevel suggest)
                entry["suggest"] = suggest.ToJson();
            list.Add(entry);
        }
        if (list.Count > 32)
            throw new ArgumentException("At most 32 signals.", nameof(signals));
        return list;
    }
}
