using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json.Nodes;
using Xunit.Abstractions;

namespace BusyMeter.Plugins.Tests;

public sealed class TokenStoreTests(ITestOutputHelper output)
{
    // Exactly what Python's TokenStore writes (json.dump(indent=2)) after save_pending then save_token.
    private const string PythonApproved = """
        {
          "pluginId": "com.example.py",
          "signals": [
            {
              "name": "deep-work",
              "label": "Deep work \u2014 do not disturb",
              "suggest": 2
            }
          ],
          "wants": [
            "signals",
            "status"
          ],
          "token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
          "savedAt": 1791700000
        }
        """;

    private const string PythonPending = """
        {
          "pluginId": "com.example.py",
          "pendingRequestId": "rq_0123456789abcdefABCD",
          "requestedSignals": [],
          "requestedWants": [
            "signals"
          ],
          "requestedAt": 1791700000
        }
        """;

    private static bool RequirePython => Environment.GetEnvironmentVariable("BUSYMETER_REQUIRE_PYTHON") == "1";

    [Fact]
    public void Reads_files_in_the_Python_format()
    {
        using var dir = new TempDir();
        var store = new TokenStore(dir.Path, restrict: false);
        File.WriteAllText(dir.File("com.example.py.json"), PythonApproved);
        Assert.Equal(Support.Token, store.LoadToken("com.example.py"));
        Assert.Null(store.LoadPending("com.example.py"));
        Assert.Equal("Deep work \u2014 do not disturb", store.Load("com.example.py")!["signals"]![0]!["label"]!.GetValue<string>());

        File.WriteAllText(dir.File("com.example.py.json"), PythonPending);
        Assert.Null(store.LoadToken("com.example.py"));
        Assert.Equal(Support.RequestId, store.LoadPending("com.example.py"));
        Assert.Equal(["com.example.py"], store.PluginIds());
    }

    [Fact]
    public void Writes_the_Python_layout()
    {
        using var dir = new TempDir();
        var store = new TokenStore(dir.Path, restrict: false);
        store.SavePending("com.example.cs", Support.RequestId, Validation.SignalDeclarations([new SignalDeclaration("deep-work", "Deep work", 2)]), ["signals", "status"]);

        var pending = JsonNode.Parse(File.ReadAllText(dir.File("com.example.cs.json")))!.AsObject();
        Assert.Equal(["pluginId", "pendingRequestId", "requestedSignals", "requestedWants", "requestedAt"], pending.Select(p => p.Key));
        Assert.Equal(Support.RequestId, pending["pendingRequestId"]!.GetValue<string>());
        Assert.InRange(pending["requestedAt"]!.GetValue<long>(), DateTimeOffset.UtcNow.ToUnixTimeSeconds() - 60, DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 1);

        store.SaveToken("com.example.cs", Support.Token);
        var text = File.ReadAllText(dir.File("com.example.cs.json"));
        Assert.Contains("\n  \"pluginId\": \"com.example.cs\"", text.ReplaceLineEndings("\n")); // indented by 2, like Python
        var saved = JsonNode.Parse(text)!.AsObject();
        Assert.Equal(["pluginId", "signals", "wants", "token", "savedAt"], saved.Select(p => p.Key)); // Python's key order
        Assert.Equal("""[{"name":"deep-work","label":"Deep work","suggest":2}]""", saved["signals"]!.ToJsonString());
        Assert.Equal("""["signals","status"]""", saved["wants"]!.ToJsonString());
        Assert.Empty(Directory.GetFiles(dir.Path, ".tmp-*"));
    }

    [Fact]
    public void Pending_and_token_lifecycle()
    {
        using var dir = new TempDir();
        var store = new TokenStore(dir.Path, restrict: false);
        const string id = "com.example.life";

        store.SavePending(id, Support.RequestId, [], ["signals"]);
        store.ClearPending(id);
        Assert.False(File.Exists(store.PathFor(id))); // nothing left: the file goes

        store.SaveToken(id, Support.Token);
        store.SavePending(id, "rq_second", [], ["signals"]);
        Assert.Equal(Support.Token, store.LoadToken(id));
        store.ClearPending(id);
        Assert.Equal(Support.Token, store.LoadToken(id)); // the token stays
        Assert.Null(store.LoadPending(id));

        store.SavePending(id, "rq_third", [], ["signals"]);
        store.DropToken(id);
        Assert.Null(store.LoadToken(id));
        Assert.Equal("rq_third", store.LoadPending(id)); // the waiting request stays
        Assert.False(store.Load(id)!.ContainsKey("savedAt"));
        store.ClearPending(id);
        Assert.False(File.Exists(store.PathFor(id)));

        store.SaveToken(id, Support.Token);
        store.DropToken(id);
        Assert.False(File.Exists(store.PathFor(id)));

        store.SaveToken(id, Support.Token);
        store.Delete(id);
        store.Delete(id); // twice is fine
        Assert.Null(store.Load(id));
    }

    [Fact]
    public void Ignores_files_for_another_id_or_garbage()
    {
        using var dir = new TempDir();
        var store = new TokenStore(dir.Path, restrict: false);
        File.WriteAllText(dir.File("com.example.a.json"), """{"pluginId":"com.example.b","token":"x"}""");
        File.WriteAllText(dir.File("com.example.c.json"), "{not json");
        File.WriteAllText(dir.File("com.example.d.json"), "[1,2]");
        File.WriteAllText(dir.File("Not An Id.json"), "{}");
        File.WriteAllText(dir.File("com.example.e.json"), """{"pluginId":"com.example.e","pluginId":"com.example.e","token":"x"}""");
        Assert.Null(store.Load("com.example.e")); // a duplicate key: as if there were no file
        Assert.Null(store.LoadToken("com.example.e"));
        Assert.Null(store.Load("com.example.a"));
        Assert.Null(store.Load("com.example.c"));
        Assert.Null(store.Load("com.example.d"));
        Assert.Null(store.LoadToken("com.example.missing"));
        Assert.Equal(["com.example.a", "com.example.c", "com.example.d", "com.example.e"], store.PluginIds());
        Assert.Throws<ArgumentException>(() => store.PathFor("..\\evil"));
        Assert.Empty(new TokenStore(dir.File("nope"), restrict: false).PluginIds());
    }

    [Fact]
    public void The_environment_variable_overrides_the_folder()
    {
        var old = Environment.GetEnvironmentVariable("BUSYMETER_TOKEN_DIR");
        try
        {
            Environment.SetEnvironmentVariable("BUSYMETER_TOKEN_DIR", @"X:\somewhere");
            Assert.Equal(@"X:\somewhere", TokenStore.DefaultDirectory());
            Environment.SetEnvironmentVariable("BUSYMETER_TOKEN_DIR", null);
            Assert.EndsWith(Path.Combine("busymeter-plugins", "tokens"), TokenStore.DefaultDirectory());
        }
        finally
        {
            Environment.SetEnvironmentVariable("BUSYMETER_TOKEN_DIR", old);
        }
    }

    [Fact]
    public void Writes_narrow_the_folder_to_the_current_user()
    {
        if (!OperatingSystem.IsWindows())
            return;
        using var dir = new TempDir();
        var folder = Path.Combine(dir.Path, "tokens");
        Directory.CreateDirectory(folder);
        File.WriteAllText(Path.Combine(folder, "com.example.old.json"), "{}"); // already there: reset to inherit
        Directory.CreateDirectory(Path.Combine(folder, "sub")); // folders too
        var warnings = new List<string>();
        var store = new TokenStore(folder) { Warning = warnings.Add };

        store.SaveToken("com.example.acl", Support.Token);

        Assert.Empty(warnings);
        CheckOnlyCurrentUser(folder);
    }

    [SupportedOSPlatform("windows")]
    private static void CheckOnlyCurrentUser(string folder)
    {
        using var me = WindowsIdentity.GetCurrent();
        var security = new DirectoryInfo(folder).GetAccessControl();
        Assert.True(security.AreAccessRulesProtected); // no inheritance from the parent
        var rules = security.GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>().ToList();
        var rule = Assert.Single(rules);
        Assert.Equal(me.User, rule.IdentityReference);
        Assert.Equal(AccessControlType.Allow, rule.AccessControlType);
        Assert.True(rule.FileSystemRights.HasFlag(FileSystemRights.FullControl));
        Assert.Equal(InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, rule.InheritanceFlags);

        foreach (var file in Directory.GetFiles(folder))
        {
            var fileRules = new FileInfo(file).GetAccessControl().GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>().ToList();
            var only = Assert.Single(fileRules);
            Assert.Equal(me.User, only.IdentityReference);
            Assert.True(only.IsInherited, file + " should only inherit the folder's grant");
        }
        foreach (var sub in Directory.GetDirectories(folder))
        {
            var subRules = new DirectoryInfo(sub).GetAccessControl().GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>().ToList();
            Assert.NotEmpty(subRules);
            Assert.All(subRules, r => Assert.True(r.IsInherited && r.IdentityReference == me.User, sub + " should only inherit the folder's grant"));
        }
    }

    [Fact]
    public void A_file_written_here_is_read_by_the_Python_client()
    {
        if (Python() is not (string python, string package))
            return;
        using var dir = new TempDir();
        var store = new TokenStore(dir.Path); // restricted, as in real use: Python must still read it
        store.SavePending("com.example.cs", Support.RequestId, Validation.SignalDeclarations([new SignalDeclaration("deep-work", "Deep work \u2014 140+", 2)]), ["signals"]);
        store.SaveToken("com.example.cs", Support.Token);

        var result = JsonNode.Parse(Support.RunPython(python, package, """
            import json, sys
            from busymeter.tokens import TokenStore
            data = TokenStore(sys.argv[1], restrict=False).load("com.example.cs")
            print(json.dumps(data))
            """, dir.Path))!.AsObject();
        Assert.Equal(Support.Token, result["token"]!.GetValue<string>());
        Assert.Equal("Deep work \u2014 140+", result["signals"]![0]!["label"]!.GetValue<string>());
        Assert.Equal(2, result["signals"]![0]!["suggest"]!.GetValue<int>());
    }

    [Fact]
    public void A_file_the_Python_client_wrote_is_read_here()
    {
        if (Python() is not (string python, string package))
            return;
        using var dir = new TempDir();
        Support.RunPython(python, package, """
            import sys
            from busymeter.tokens import TokenStore
            store = TokenStore(sys.argv[1])
            store.save_pending("com.example.py", "rq_fromPython", [{"name": "deep-work", "label": "Deep work", "suggest": "flash"}], ["signals", "effects"])
            store.save_token("com.example.py", "B" * 43)
            store.save_pending("com.example.py", "rq_again", [], ["signals"])
            """, dir.Path);

        var store = new TokenStore(dir.Path);
        Assert.Equal(new string('B', 43), store.LoadToken("com.example.py"));
        Assert.Equal("rq_again", store.LoadPending("com.example.py"));
        Assert.Equal("flash", store.Load("com.example.py")!["signals"]![0]!["suggest"]!.GetValue<string>());
        store.ClearPending("com.example.py");
        Assert.Equal(new string('B', 43), store.LoadToken("com.example.py"));
    }

    private (string Python, string Package)? Python()
    {
        var python = Support.FindPython();
        var package = Support.PythonPackageDir();
        if (python is null || package is null)
        {
            Assert.False(RequirePython, "BUSYMETER_REQUIRE_PYTHON=1 but no Python 3.10+ (or no python/ folder next to dotnet/) was found");
            output.WriteLine("SKIPPED: no Python 3.10+ on PATH or no python/ folder next to dotnet/.");
            return null;
        }
        return (python, package);
    }
}
