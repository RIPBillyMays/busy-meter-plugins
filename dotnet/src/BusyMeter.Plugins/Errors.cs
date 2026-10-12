using System.Text.Json.Nodes;

namespace BusyMeter.Plugins;

/// <summary>
/// An error from the API (or the connection). <see cref="Code"/> is the API's error code, <see cref="Status"/> the
/// HTTP status. <see cref="Exception.Message"/> can be shown to the user as is.
/// </summary>
public class BusyMeterException : Exception
{
    /// <summary>Creates an error.</summary>
    public BusyMeterException(string message, string code = "error", int? status = null, TimeSpan? retryAfter = null, Exception? inner = null)
        : base(message, inner)
    {
        Code = code;
        Status = status;
        RetryAfter = retryAfter;
    }

    /// <summary>The API's error code (<c>unauthorized</c>, <c>plugin_off</c>, ...) or one of this client's
    /// (<c>not_listening</c>, <c>not_responding</c>, <c>cancelled</c>, ...).</summary>
    public string Code { get; }

    /// <summary>The HTTP status, when there was an answer.</summary>
    public int? Status { get; }

    /// <summary>How long to wait before trying again, when the app said (429).</summary>
    public TimeSpan? RetryAfter { get; }
}

/// <summary>Nothing answers on the port: the app isn't running or External connections is off.</summary>
public sealed class NotListeningException(string message, Exception? inner = null)
    : BusyMeterException(message, "not_listening", null, null, inner);

/// <summary>Something accepted the connection but didn't answer in time, or the connection broke mid-request.</summary>
public sealed class AppNotRespondingException(string message, Exception? inner = null)
    : BusyMeterException(message, "not_responding", null, null, inner);

/// <summary>401: no token, or Busy Meter doesn't know it (revoked or removed).</summary>
public sealed class UnauthorizedException(string message, string code = "unauthorized", int? status = 401)
    : BusyMeterException(message, code, status);

/// <summary>403: switched off, not allowed by the approval, or (never from this client) an Origin header.</summary>
public sealed class ForbiddenException(string message, string code, int? status = 403)
    : BusyMeterException(message, code, status);

/// <summary>409: a request is already waiting, too many plugins, or an alert was ignored.</summary>
public sealed class ConflictException(string message, string code, int? status = 409)
    : BusyMeterException(message, code, status);

/// <summary>429: too fast. <see cref="BusyMeterException.RetryAfter"/> is set when the app said.</summary>
public sealed class RateLimitedException(string message, string code, int? status = 429, TimeSpan? retryAfter = null)
    : BusyMeterException(message, code, status, retryAfter);

/// <summary>The user clicked Deny, or didn't answer within 2 minutes (<see cref="BusyMeterException.Code"/>
/// <c>denied</c> or <c>expired</c>).</summary>
public sealed class DeniedException(string message, string code)
    : BusyMeterException(message, code);

/// <summary>
/// Stopped waiting for approval because the caller's <see cref="CancellationToken"/> was cancelled. A request already
/// shown in the tray stays there until the user answers or it expires (2 minutes); its id stays saved, so the next
/// connect waits on it instead of asking again (or call <see cref="BusyMeterClient.WithdrawAsync"/> to close it).
/// </summary>
public sealed class CancelledException(string message, Exception? inner = null)
    : BusyMeterException(message, "cancelled", null, null, inner);

/// <summary>Still waiting for the user after the time this client was told to wait.</summary>
public sealed class ApprovalTimeoutException(string message)
    : BusyMeterException(message, "pending");

/// <summary>Messages, and the mapping of an error answer to the typed exceptions (same as the Python client).</summary>
public static class Messages
{
    /// <summary>No discovery file, so no token is sent anywhere.</summary>
    public const string NotRunning =
        "Busy Meter isn't running, or External connections is off (there's no %LOCALAPPDATA%\\BusyMeter\\"
        + "plugin-api.json, which the app writes while it listens). Turn on Settings → Sources → External "
        + "connections in the Busy Meter app.";

    /// <summary>Connected, but no answer in time.</summary>
    public const string NotResponding =
        "Busy Meter didn't answer in time (the app may be busy or stuck). Try again; if it keeps happening, restart the "
        + "Busy Meter app.";

    /// <summary>The connection broke before the answer.</summary>
    public const string ConnectionBroke =
        "The connection to Busy Meter broke before it answered. Try again; if it keeps happening, "
        + "restart the Busy Meter app.";

    /// <summary>Nothing listens on the port.</summary>
    public const string TurnOn =
        "Busy Meter isn't accepting connections. Turn on Settings → Sources → External connections "
        + "in the Busy Meter app (and make sure the app is running).";

    private static readonly Dictionary<string, string> Friendly = new()
    {
        ["unauthorized"] = "Busy Meter doesn't know this plugin's token (it was revoked, or never approved). "
            + "Register again; you'll be asked to click Allow in the Busy Meter tray.",
        ["plugin_off"] = "This plugin is switched off in Busy Meter. Turn it on under Settings → Sources → "
            + "External connections.",
        ["not_allowed"] = "Busy Meter didn't allow this plugin to do that. Register again asking for it in 'wants' "
            + "(the user approves the new request in the tray).",
        ["already_pending"] = "A request for this plugin id is already waiting for you in the Busy Meter tray. "
            + "Click Allow (or Deny) there.",
        ["too_many_plugins"] = "Busy Meter already has the most plugins it takes (32). Revoke one under Settings → "
            + "Sources → External connections.",
        ["cooldown"] = "This plugin was denied in the last 10 minutes; Busy Meter won't ask again until then.",
        ["withdraw_pause"] = "This plugin just withdrew a request; Busy Meter takes a new one after 5 seconds.",
        ["too_many_pending"] = "Too many requests are waiting in the Busy Meter tray; answer those first.",
        ["forbidden_origin"] = "Busy Meter refused the request because it carried an Origin header (web pages can't "
            + "use the API).",
        ["unknown_signal"] = "That signal wasn't declared when the plugin registered. Use a declared signal or a level.",
    };

    /// <summary>The exception for an error answer (<paramref name="status"/> 400 or more).</summary>
    public static BusyMeterException ToException(int status, JsonObject payload, string? retryAfter)
    {
        var code = Text(payload["error"]) is { Length: > 0 } c ? c : "http_" + status;
        var serverMessage = Text(payload["message"]) ?? "";
        var message = Friendly.TryGetValue(code, out var friendly)
            ? friendly
            : serverMessage.Length > 0 ? serverMessage : $"Busy Meter answered {status}.";
        if (code == "ignored")
            message = $"Busy Meter ignored the alert ({(serverMessage.Length > 0 ? serverMessage : "busy")}).";
        else if (code == "rate_limited")
            message = "Too fast: " + (serverMessage.Length > 0 ? serverMessage : "slow down.");

        TimeSpan? retry = null;
        if (retryAfter is not null
            && double.TryParse(retryAfter.Trim(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var seconds)
            && double.IsFinite(seconds) && seconds >= 0 && seconds < TimeSpan.MaxValue.TotalSeconds)
            retry = TimeSpan.FromSeconds(seconds);

        return status switch
        {
            401 => new UnauthorizedException(message, code, status),
            403 => new ForbiddenException(message, code, status),
            409 => new ConflictException(message, code, status),
            429 => new RateLimitedException(message, code, status, retry),
            _ => new BusyMeterException(message, code, status, retry),
        };
    }

    // Python's str(): a JSON string as is; anything else as its JSON text (rare: the app always sends strings).
    private static string? Text(JsonNode? node) => node switch
    {
        null => null,
        JsonValue v when v.TryGetValue<string>(out var s) => s,
        _ => node.ToJsonString(),
    };
}
