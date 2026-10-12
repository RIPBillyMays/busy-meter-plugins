namespace BusyMeter.Plugins.Tests;

public sealed class DiscoveryTests
{
    [Fact]
    public void The_port_comes_from_the_file()
    {
        using var dir = new TempDir();
        File.WriteAllText(dir.File("plugin-api.json"), """{"port":50123,"baseUrl":"http://evil.example:1/v1/","apiVersion":1}""");
        var endpoint = Discovery.Discover(dir.File("plugin-api.json"));
        Assert.Equal(new Endpoint(50123, FromFile: true), endpoint);
        Assert.True(endpoint.Trusted);
        Assert.Equal("http://127.0.0.1:50123/v1/", endpoint.BaseUrl); // only the port is taken; the host is always loopback
    }

    [Theory]
    [InlineData(null)]
    [InlineData("not json")]
    [InlineData("[47821]")]
    [InlineData("""{"port":"47821"}""")]
    [InlineData("""{"port":47821.5}""")]
    [InlineData("""{"port":47821.0}""")]
    [InlineData("""{"port":true}""")]
    [InlineData("""{"port":80}""")]
    [InlineData("""{"port":65536}""")]
    [InlineData("""{"baseUrl":"http://127.0.0.1:47821/v1/"}""")]
    public void Without_a_good_file_it_is_the_default_port_and_untrusted(string? content)
    {
        using var dir = new TempDir();
        if (content is not null)
            File.WriteAllText(dir.File("plugin-api.json"), content);
        var endpoint = Discovery.Discover(dir.File("plugin-api.json"));
        Assert.Equal(Discovery.DefaultPort, endpoint.Port);
        Assert.False(endpoint.Trusted);
    }

    [Fact]
    public void The_environment_variable_overrides_the_path()
    {
        using var dir = new TempDir();
        var old = Environment.GetEnvironmentVariable("BUSYMETER_DISCOVERY_FILE");
        try
        {
            Environment.SetEnvironmentVariable("BUSYMETER_DISCOVERY_FILE", dir.File("x.json"));
            Assert.Equal(dir.File("x.json"), Discovery.DiscoveryPath());
            Environment.SetEnvironmentVariable("BUSYMETER_DISCOVERY_FILE", null);
            Assert.EndsWith(Path.Combine("BusyMeter", "plugin-api.json"), Discovery.DiscoveryPath());
        }
        finally
        {
            Environment.SetEnvironmentVariable("BUSYMETER_DISCOVERY_FILE", old);
        }
    }

    [Fact]
    public async Task No_token_goes_to_the_default_port_without_the_file()
    {
        using var dir = new TempDir();
        using var meter = new BusyMeterClient("com.example.test", token: Support.Token, discoveryFile: dir.File("missing.json"),
            store: new TokenStore(dir.Path, restrict: false));
        Assert.False(meter.Endpoint.Trusted);

        // Refused before any connection: whatever holds 47820 now isn't the app (it would have written the file).
        var e = await Assert.ThrowsAsync<NotListeningException>(() => meter.SetLevelAsync(4, 30));
        Assert.Equal("not_listening", e.Code);
        Assert.Equal(Messages.NotRunning, e.Message);
        await Assert.ThrowsAsync<NotListeningException>(() => meter.StatusAsync());
        await Assert.ThrowsAsync<NotListeningException>(() => meter.VerifyTokenAsync());
    }

    [Fact]
    public async Task A_token_goes_to_the_port_from_the_file()
    {
        await using var api = new FakeApi();
        api.On("GET /v1/status", 200, Support.StatusBody);
        using var dir = new TempDir();
        File.WriteAllText(dir.File("plugin-api.json"), $$"""{"port":{{api.Port}}}""");
        using var meter = new BusyMeterClient("com.example.test", token: Support.Token, discoveryFile: dir.File("plugin-api.json"),
            store: new TokenStore(dir.Path, restrict: false));

        Assert.Equal(4, (await meter.StatusAsync())["level"]!.GetValue<int>());
        Assert.Equal("Bearer " + Support.Token, api.Requests.Single().Header("Authorization"));
    }

    [Fact]
    public async Task An_explicit_port_is_trusted()
    {
        await using var api = new FakeApi();
        api.On("GET /v1/status", 200, Support.StatusBody);
        using var dir = new TempDir();
        using var meter = new BusyMeterClient("com.example.test", port: api.Port, token: Support.Token, discoveryFile: dir.File("missing.json"),
            store: new TokenStore(dir.Path, restrict: false));

        Assert.True(meter.Endpoint.Explicit);
        await meter.StatusAsync();
        Assert.Equal("Bearer " + Support.Token, api.Requests.Single().Header("Authorization"));
    }
}
