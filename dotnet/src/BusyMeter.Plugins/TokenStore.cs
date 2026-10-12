using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BusyMeter.Plugins;

/// <summary>
/// Where plugin tokens are kept: one small JSON file per plugin id, readable by the current user only. Same folder and
/// file format as the Python client (<c>busymeter.tokens</c>), so both can use the same plugin's token.
/// </summary>
/// <remarks>
/// <para>Default folder (override with the <c>BUSYMETER_TOKEN_DIR</c> environment variable):</para>
/// <list type="bullet">
/// <item>Windows: <c>%LOCALAPPDATA%\busymeter-plugins\tokens</c>. Before every write the folder (also one named by
/// <c>BUSYMETER_TOKEN_DIR</c>) loses its inherited permissions and grants only the current user (by SID) full control,
/// inherited by the files in it. If that fails, <see cref="Warning"/> says so and the file is still written.</item>
/// <item>macOS/Linux: <c>$XDG_CONFIG_HOME/busymeter-plugins/tokens</c> (else <c>~/.config/...</c>); the folder is set to
/// 0700 (also when it already existed with looser modes), files 0600.</item>
/// </list>
/// <para>A file looks like <c>{"pluginId": "...", "token": "...", "signals": [...], "wants": [...]}</c>, plus
/// <c>pendingRequestId</c> while a registration waits for the user. The token is a bearer secret: anyone who reads it can
/// drive this plugin's signals until the user revokes it under Settings › Sources › External connections.</para>
/// </remarks>
public sealed class TokenStore
{
    private static readonly JsonSerializerOptions FileJson = new()
    {
        WriteIndented = true, // Python writes indent=2
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping, // a local file, never HTML: keep labels readable
    };

    private static readonly string[] PendingKeys = ["pendingRequestId", "requestedSignals", "requestedWants", "requestedAt"];

    private readonly bool _restrict;

    /// <summary>A store in <paramref name="directory"/> (default <see cref="DefaultDirectory"/>).</summary>
    /// <param name="directory">The folder.</param>
    /// <param name="restrict">Narrow the folder to the current user before every write (tests may turn it off).</param>
    public TokenStore(string? directory = null, bool restrict = true)
    {
        Directory = directory ?? DefaultDirectory();
        _restrict = restrict;
    }

    /// <summary>The folder.</summary>
    public string Directory { get; }

    /// <summary>Called when the folder couldn't be narrowed to the current user (the file is still written).
    /// Default: the message goes to standard error, like Python's warning.</summary>
    public Action<string>? Warning { get; set; } = message => Console.Error.WriteLine("busymeter: " + message);

    /// <summary><c>BUSYMETER_TOKEN_DIR</c>, else the per-user default.</summary>
    public static string DefaultDirectory()
    {
        var overridePath = Environment.GetEnvironmentVariable("BUSYMETER_TOKEN_DIR");
        if (!string.IsNullOrEmpty(overridePath))
            return overridePath;
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (OperatingSystem.IsWindows())
        {
            var local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (string.IsNullOrEmpty(local))
                local = Path.Combine(home, "AppData", "Local");
            return Path.Combine(local, "busymeter-plugins", "tokens");
        }
        var config = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
        if (string.IsNullOrEmpty(config))
            config = Path.Combine(home, ".config");
        return Path.Combine(config, "busymeter-plugins", "tokens");
    }

    /// <summary>The file for <paramref name="pluginId"/>.</summary>
    /// <exception cref="ArgumentException">Not a plugin id.</exception>
    public string PathFor(string pluginId)
    {
        if (!Validation.IsValidPluginId(pluginId))
            throw new ArgumentException("Not a plugin id.", nameof(pluginId));
        return Path.Combine(Directory, pluginId + ".json");
    }

    /// <summary>The saved data, or null (no file, unreadable, or written for another id).</summary>
    public JsonObject? Load(string pluginId)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllBytes(PathFor(pluginId)));
            if (node is not JsonObject data)
                return null;
            _ = data.Count; // builds the object now, so a duplicate key throws here (ArgumentException), not later
            return data["pluginId"] is JsonValue id && id.TryGetValue<string>(out var saved) && saved == pluginId ? data : null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException
            or ArgumentException) // ArgumentException: a duplicate key; treated as no file
        {
            return null;
        }
    }

    /// <summary>The saved token, or null.</summary>
    public string? LoadToken(string pluginId) => Text(Load(pluginId)?["token"]);

    /// <summary>The saved waiting request id, or null.</summary>
    public string? LoadPending(string pluginId) => Text(Load(pluginId)?["pendingRequestId"]);

    /// <summary>Keep a waiting request (and what it asked for) next to any saved token.</summary>
    public void SavePending(string pluginId, string requestId, JsonArray signals, IEnumerable<string> wants)
    {
        var data = Load(pluginId) ?? new JsonObject { ["pluginId"] = pluginId };
        data["pendingRequestId"] = requestId;
        data["requestedSignals"] = signals.DeepClone();
        data["requestedWants"] = new JsonArray(wants.Select(w => (JsonNode?)JsonValue.Create(w)).ToArray());
        data["requestedAt"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        Write(pluginId, data);
    }

    /// <summary>Forget a waiting request; the file goes when no token is left in it.</summary>
    public void ClearPending(string pluginId)
    {
        var data = Load(pluginId);
        if (data is null)
            return;
        foreach (var key in PendingKeys)
            data.Remove(key);
        if (IsTruthy(data["token"]))
            Write(pluginId, data);
        else
            Delete(pluginId);
    }

    /// <summary>Save an approved token; what the request asked for becomes <c>signals</c> and <c>wants</c>.</summary>
    public void SaveToken(string pluginId, string token)
    {
        var data = Load(pluginId) ?? new JsonObject { ["pluginId"] = pluginId };
        if (data.ContainsKey("requestedSignals"))
        {
            var signals = data["requestedSignals"];
            data.Remove("requestedSignals");
            data["signals"] = signals;
        }
        if (data.ContainsKey("requestedWants"))
        {
            var wants = data["requestedWants"];
            data.Remove("requestedWants");
            data["wants"] = wants;
        }
        data.Remove("pendingRequestId");
        data.Remove("requestedAt");
        data["token"] = token;
        data["savedAt"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        Write(pluginId, data);
    }

    /// <summary>Remove the token but keep a waiting request (and what it asked for).</summary>
    public void DropToken(string pluginId)
    {
        var data = Load(pluginId);
        if (data is null || !data.ContainsKey("token"))
            return;
        data.Remove("token");
        data.Remove("savedAt");
        if (IsTruthy(data["pendingRequestId"]))
            Write(pluginId, data);
        else
            Delete(pluginId);
    }

    /// <summary>Delete the plugin's file.</summary>
    public void Delete(string pluginId)
    {
        try
        {
            File.Delete(PathFor(pluginId));
        }
        catch (DirectoryNotFoundException)
        {
        }
    }

    /// <summary>The ids that have a file, sorted.</summary>
    public IReadOnlyList<string> PluginIds()
    {
        try
        {
            return System.IO.Directory.EnumerateFiles(Directory, "*.json")
                .Select(Path.GetFileNameWithoutExtension)
                .Where(id => Validation.IsValidPluginId(id))
                .Select(id => id!)
                .Order(StringComparer.Ordinal)
                .ToList();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return [];
        }
    }

    /// <summary>Create the folder if needed and narrow it to the current user, every time (it may have been loosened).</summary>
    public void EnsureDirectory()
    {
        System.IO.Directory.CreateDirectory(Directory);
        if (!_restrict)
            return;
        if (OperatingSystem.IsWindows())
        {
            RestrictWindowsDirectory(Directory, Warning);
        }
        else
        {
            const UnixFileMode Private = UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute;
            if (File.GetUnixFileMode(Directory) != Private)
                File.SetUnixFileMode(Directory, Private);
        }
    }

    /// <summary>
    /// Remove inherited entries from <paramref name="path"/> and grant only the current user (by SID) full control,
    /// inherited by files and folders in it. Files and folders already there are reset so they inherit exactly that. False (and a
    /// warning) on failure.
    /// </summary>
    [SupportedOSPlatform("windows")]
    public static bool RestrictWindowsDirectory(string path, Action<string>? warning = null)
    {
        try
        {
            using var identity = WindowsIdentity.GetCurrent();
            var sid = identity.User ?? throw new InvalidOperationException("no user SID");
            var folder = new DirectorySecurity();
            folder.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
            folder.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            new DirectoryInfo(path).SetAccessControl(folder);

            // Files and folders already there: no explicit entries, not protected, so they inherit only the folder's
            // grant (like icacls path\* /reset).
            foreach (var entry in new DirectoryInfo(path).EnumerateFileSystemInfos())
            {
                if (entry is DirectoryInfo sub)
                {
                    var reset = new DirectorySecurity();
                    reset.SetAccessRuleProtection(isProtected: false, preserveInheritance: false);
                    sub.SetAccessControl(reset);
                }
                else if (entry is FileInfo file)
                {
                    var reset = new FileSecurity();
                    reset.SetAccessRuleProtection(isProtected: false, preserveInheritance: false);
                    file.SetAccessControl(reset);
                }
            }
            return true;
        }
        catch (SystemException e) // IO, access, privilege and identity errors
        {
            warning?.Invoke($"Couldn't narrow the permissions of {path}: {e.Message}");
            return false;
        }
    }

    private void Write(string pluginId, JsonObject data)
    {
        EnsureDirectory();
        var target = PathFor(pluginId);
        var temp = Path.Combine(Directory, ".tmp-" + Guid.NewGuid().ToString("N"));
        try
        {
            var options = new FileStreamOptions { Mode = FileMode.CreateNew, Access = FileAccess.Write, Share = FileShare.None };
            if (!OperatingSystem.IsWindows())
                options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
            using (var stream = new FileStream(temp, options))
            {
                JsonSerializer.Serialize(stream, data, FileJson);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temp, target, overwrite: true);
        }
        catch
        {
            try { File.Delete(temp); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
            throw;
        }
    }

    private static string? Text(JsonNode? node) => node is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;

    // Python's truthiness for the values these files hold.
    private static bool IsTruthy(JsonNode? node) => node switch
    {
        null => false,
        JsonValue v when v.TryGetValue<string>(out var s) => s.Length > 0,
        JsonValue v when v.TryGetValue<bool>(out var b) => b,
        JsonValue v when v.TryGetValue<double>(out var d) => d != 0,
        JsonArray a => a.Count > 0,
        JsonObject o => o.Count > 0,
        _ => true,
    };
}
