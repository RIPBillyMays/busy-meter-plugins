namespace BusyMeter.Plugins.Tests;

public sealed class ValidationTests
{
    [Theory]
    [InlineData("com.example.heartrate", true)]
    [InlineData("abc", true)]
    [InlineData("a-b.c-1", true)]
    [InlineData("ab", false)]
    [InlineData("Com.Example", false)]
    [InlineData("com_example", false)]
    [InlineData("abc\n", false)]
    [InlineData("", false)]
    [InlineData(null, false)]
    public void Plugin_ids(string? id, bool valid) => Assert.Equal(valid, Validation.IsValidPluginId(id));

    [Fact]
    public void Plugin_id_length_is_3_to_64()
    {
        Assert.True(Validation.IsValidPluginId(new string('a', 64)));
        Assert.False(Validation.IsValidPluginId(new string('a', 65)));
    }

    [Fact]
    public void The_client_refuses_a_bad_id() =>
        Assert.Throws<ArgumentException>(() => new BusyMeterClient("Not An Id"));

    [Theory]
    [InlineData("rq_0123456789abcdefABCD", true)]
    [InlineData("a", true)]
    [InlineData("../x", false)]
    [InlineData("a b", false)]
    [InlineData("a?b=1", false)]
    [InlineData("abc\n", false)]
    [InlineData("", false)]
    public void Request_ids(string id, bool valid) => Assert.Equal(valid, Validation.IsValidRequestId(id));

    [Fact]
    public void Levels_are_0_to_6_or_flash()
    {
        for (var i = 0; i <= 6; i++)
        {
            MeterLevel level = i;
            Assert.Equal(i, level.Stars);
            Assert.Equal(i, level.ToJson().GetValue<int>());
        }
        Assert.Throws<ArgumentOutOfRangeException>(() => MeterLevel.FromStars(7));
        Assert.Throws<ArgumentOutOfRangeException>(() => (MeterLevel)(-1));
        Assert.True(MeterLevel.Flash.IsFlash);
        Assert.Null(MeterLevel.Flash.Stars);
        Assert.Equal("flash", MeterLevel.Flash.ToJson().GetValue<string>());
        Assert.Equal(default, MeterLevel.FromStars(0)); // default is Free (0 stars)
    }

    [Theory]
    [InlineData("flash", "flash")]
    [InlineData(" FLASH ", "flash")]
    [InlineData("0", "0")]
    [InlineData(" 6 ", "6")]
    public void Levels_parse(string text, string expected) => Assert.Equal(expected, MeterLevel.Parse(text).ToString());

    [Theory]
    [InlineData("7")]
    [InlineData("-1")]
    [InlineData("off")]
    [InlineData("")]
    [InlineData("3.0")]
    public void Bad_levels_do_not_parse(string text) => Assert.Throws<ArgumentException>(() => MeterLevel.Parse(text));

    [Theory]
    [InlineData("30", 30)]
    [InlineData("30s", 30)]
    [InlineData("10m", 600)]
    [InlineData(" 5 min ", 300)]
    [InlineData("1.5h", 5400)]
    [InlineData("2 hours", 7200)]
    [InlineData("1.5", 2)]   // Python's round: half to even
    [InlineData("2.5", 2)]
    [InlineData("3.5", 4)]
    [InlineData("10M", 600)]
    public void Durations(string text, int seconds) => Assert.Equal(seconds, Validation.ParseDuration(text));

    [Theory]
    [InlineData("abc")]
    [InlineData("0.4")]
    [InlineData("0")]
    [InlineData("10 days")]
    [InlineData("-5")]
    [InlineData("")]
    public void Bad_durations(string text) => Assert.Throws<ArgumentException>(() => Validation.ParseDuration(text));

    [Fact]
    public void Duration_from_seconds()
    {
        Assert.Equal(90, Validation.ParseDuration(90.0));
        Assert.Throws<ArgumentException>(() => Validation.ParseDuration(0.5));
        Assert.Throws<ArgumentException>(() => Validation.ParseDuration(double.NaN));
        Assert.Throws<ArgumentException>(() => Validation.ParseDuration(double.PositiveInfinity));
        Assert.Throws<ArgumentException>(() => Validation.ParseDuration(new string('9', 400) + "h"));
    }

    [Theory]
    [InlineData(1, true)]
    [InlineData(600, true)]
    [InlineData(0, false)]
    [InlineData(601, false)]
    [InlineData(-1, false)]
    public void Ttls(int ttl, bool valid)
    {
        if (valid)
            Assert.Equal(ttl, Validation.CheckTtl(ttl));
        else
            Assert.Contains("1-600", Assert.Throws<ArgumentOutOfRangeException>(() => Validation.CheckTtl(ttl)).Message);
    }

    [Fact]
    public void Signal_declarations_are_shaped_like_the_api_wants()
    {
        var json = Validation.SignalDeclarations([
            "plain",
            new SignalDeclaration("hr-high", "Heart rate high", 5),
            new SignalDeclaration("hr-max", Suggest: MeterLevel.Flash),
        ]);
        Assert.Equal("""[{"name":"plain"},{"name":"hr-high","label":"Heart rate high","suggest":5},{"name":"hr-max","suggest":"flash"}]""",
            json.ToJsonString());
    }

    [Theory]
    [InlineData("level-4")]
    [InlineData("Upper")]
    [InlineData("has space")]
    [InlineData("")]
    [InlineData("under_score")]
    public void Bad_signal_names(string name) =>
        Assert.Throws<ArgumentException>(() => Validation.SignalDeclarations([new SignalDeclaration(name)]));

    [Fact]
    public void Signal_names_are_at_most_48_and_at_most_32_signals()
    {
        Validation.SignalDeclarations([new SignalDeclaration(new string('a', 48))]);
        Assert.Throws<ArgumentException>(() => Validation.SignalDeclarations([new SignalDeclaration(new string('a', 49))]));
        Validation.SignalDeclarations(Enumerable.Range(0, 32).Select(i => new SignalDeclaration("s" + i)));
        Assert.Contains("32", Assert.Throws<ArgumentException>(() =>
            Validation.SignalDeclarations(Enumerable.Range(0, 33).Select(i => new SignalDeclaration("s" + i)))).Message);
    }

    [Fact]
    public async Task Bad_arguments_throw_before_anything_is_sent()
    {
        await using var api = new FakeApi();
        using var dir = new TempDir();
        using var meter = new BusyMeterClient("com.example.test", port: api.Port, token: Support.Token, store: new TokenStore(dir.Path, restrict: false));

        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => meter.SetLevelAsync(3, 0));
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => meter.SetSignalsAsync(["a"], 601));
        await Assert.ThrowsAsync<ArgumentException>(() => meter.AlertAsync("rainbow"));
        await Assert.ThrowsAsync<ArgumentException>(() => meter.PollAsync("../status"));
        await Assert.ThrowsAsync<ArgumentException>(() => meter.WithdrawAsync("a/b"));
        await Assert.ThrowsAsync<ArgumentException>(() => meter.RegisterAsync(signals: ["level-1"]));
        Assert.Empty(api.Requests);
    }
}
