using System.Diagnostics;
using System.Net;
using System.Net.Sockets;

namespace BusyMeter.Plugins.Tests;

/// <summary>A temporary folder, deleted afterwards.</summary>
public sealed class TempDir : IDisposable
{
    public string Path { get; } = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "bm-dotnet-" + Guid.NewGuid().ToString("N"));

    public TempDir() => Directory.CreateDirectory(Path);

    public string File(string name) => System.IO.Path.Combine(Path, name);

    public void Dispose()
    {
        try { Directory.Delete(Path, recursive: true); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }
}

public static class Support
{
    public const string Token = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"; // 43 characters, like the app's
    public const string RequestId = "rq_0123456789abcdefABCD";
    public const string Pending = """{"state":"pending"}""";
    public const string Approved = """{"state":"approved","pluginId":"com.example.test","token":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}""";
    public const string Accepted = """{"requestId":"rq_0123456789abcdefABCD","state":"pending","expiresInSeconds":120}""";
    public const string StatusBody = """{"level":4,"paused":false,"source":"teams"}""";

    public static string RegisterRoute => "POST /v1/register";
    public static string PollRoute => "GET /v1/register/" + RequestId;
    public static string WithdrawRoute => "DELETE /v1/register/" + RequestId;

    /// <summary>A port nothing listens on (bound, then released).</summary>
    public static int ClosedPort()
    {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        var port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        return port;
    }

    /// <summary>A Python 3 that can import the busymeter package next to this repo's dotnet folder, or null.</summary>
    public static string? FindPython()
    {
        foreach (var name in new[] { "python", "python3", "py" })
        {
            try
            {
                var start = new ProcessStartInfo(name) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
                start.ArgumentList.Add("-c");
                start.ArgumentList.Add("import sys; print(sys.version_info >= (3, 10))");
                using var process = Process.Start(start);
                if (process is null)
                    continue;
                var output = process.StandardOutput.ReadToEnd();
                if (!process.WaitForExit(15000))
                    continue;
                if (process.ExitCode == 0 && output.Trim() == "True")
                    return name;
            }
            catch (System.ComponentModel.Win32Exception)
            {
            }
        }
        return null;
    }

    /// <summary>The Python package folder (<c>python/</c> next to <c>dotnet/</c>), found above the test's folder.</summary>
    public static string? PythonPackageDir()
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = System.IO.Path.Combine(dir.FullName, "python", "busymeter", "tokens.py");
            if (System.IO.File.Exists(candidate))
                return System.IO.Path.Combine(dir.FullName, "python");
            candidate = System.IO.Path.Combine(dir.FullName, "plugins", "python", "busymeter", "tokens.py");
            if (System.IO.File.Exists(candidate))
                return System.IO.Path.Combine(dir.FullName, "plugins", "python");
        }
        return null;
    }

    /// <summary>Runs a Python snippet with the busymeter package importable; returns its standard output.</summary>
    public static string RunPython(string python, string packageDir, string code, params string[] args)
    {
        var start = new ProcessStartInfo(python)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = packageDir,
        };
        start.ArgumentList.Add("-c");
        start.ArgumentList.Add(code);
        foreach (var a in args)
            start.ArgumentList.Add(a);
        start.Environment["PYTHONPATH"] = packageDir;
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        Assert.True(process.WaitForExit(60000), "python timed out");
        Assert.True(process.ExitCode == 0, "python failed: " + stderr.Result);
        return stdout.Result;
    }
}
