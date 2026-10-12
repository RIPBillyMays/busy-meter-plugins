using System.Text;
using System.Text.Json.Nodes;

namespace BusyMeter.Plugins.Tests;

/// <summary>A fake API, a token folder and a client pointed at both.</summary>
public abstract class ClientTestBase : IAsyncLifetime
{
    protected FakeApi Api { get; } = new();
    protected TempDir Dir { get; } = new();
    protected TokenStore Store => new(Dir.Path, restrict: false);

    protected BusyMeterClient Meter(string id = "com.example.test", string? token = null, TimeSpan? timeout = null) =>
        new(id, port: Api.Port, token: token, store: Store, timeout: timeout);

    public Task InitializeAsync() => Task.CompletedTask;

    public async Task DisposeAsync()
    {
        await Api.DisposeAsync();
        Dir.Dispose();
    }
}

public sealed class WireTests : ClientTestBase
{
    [Fact]
    public async Task Requests_are_plain_loopback_json_without_origin_or_cookies()
    {
        Api.On("PUT /v1/signals", 200, """{"active":["level-5"],"expiresInSeconds":60}""");
        Api.On("GET /v1/status", 200, Support.StatusBody);
        Api.On("DELETE /v1/signals", 200, """{"active":[],"expiresInSeconds":0}""");
        Api.On("POST /v1/alert", 200, """{"shown":true}""");
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        using var meter = Meter(token: Support.Token);

        await meter.SetLevelAsync(5, 60);
        await meter.SetSignalsAsync(["deep-work"], 30);
        await meter.SetLevelAsync(MeterLevel.Flash, 10);
        await meter.StatusAsync();
        await meter.ClearAsync();
        await meter.AlertAsync("lip-pulse", "#F0C850", 200);
        await meter.RegisterAsync("Test", wants: ["signals"]);

        var requests = Api.Requests;
        Assert.Equal(7, requests.Count);
        foreach (var r in requests)
        {
            Assert.StartsWith(r.Method + " /v1/", r.RequestLine);           // origin-form, never absolute-form
            Assert.EndsWith(" HTTP/1.1", r.RequestLine);
            Assert.DoesNotContain("?", r.Target);
            Assert.Equal($"127.0.0.1:{Api.Port}", r.Header("Host"));
            Assert.Null(r.Header("Origin"));
            Assert.Null(r.Header("Cookie"));
            Assert.Null(r.Header("Proxy-Connection"));
            Assert.Null(r.Header("Proxy-Authorization"));
            Assert.Equal("application/json", r.Header("Accept"));
            if (r.Body.Length > 0)
                Assert.Equal("application/json", r.Header("Content-Type"));
        }
        Assert.Equal("""{"level":5,"ttlSeconds":60}""", Encoding.UTF8.GetString(requests[0].Body));
        Assert.Equal("""{"active":["deep-work"],"ttlSeconds":30}""", Encoding.UTF8.GetString(requests[1].Body));
        Assert.Equal("""{"level":"flash","ttlSeconds":10}""", Encoding.UTF8.GetString(requests[2].Body));
        Assert.Empty(requests[3].Body);
        Assert.Empty(requests[4].Body);
        Assert.Equal("""{"effect":"lip-pulse","color":"#F0C850","durationMs":200}""", Encoding.UTF8.GetString(requests[5].Body));
        Assert.Equal("""{"id":"com.example.test","signals":[],"wants":["signals"],"name":"Test"}""", Encoding.UTF8.GetString(requests[6].Body));
        Assert.Equal("Bearer " + Support.Token, requests[0].Header("Authorization"));
        Assert.Null(requests[6].Header("Authorization")); // registration never carries a token
    }

    [Fact]
    public async Task Answers_are_read_up_to_64_KB()
    {
        var big = "{\"level\":4,\"pad\":\"" + new string('x', 100_000) + "\"}";
        Api.On("GET /v1/status", 200, big);
        using var meter = Meter(token: Support.Token);
        Assert.Empty(await meter.StatusAsync()); // cut at 64 KB: not JSON any more, so nothing is read from it
    }

    [Fact]
    public async Task Duplicate_keys_in_an_answer_read_as_empty()
    {
        Api.OnSequence("GET /v1/status", FakeResponse.Json(200, """{"level":1,"level":2}"""), FakeResponse.Json(400, """{"error":"a","error":"b"}"""));
        using var meter = Meter(token: Support.Token);
        Assert.Empty(await meter.StatusAsync());
        var e = await Assert.ThrowsAsync<BusyMeterException>(() => meter.StatusAsync());
        Assert.Equal(("http_400", 400), (e.Code, e.Status));
    }

    [Fact]
    public async Task Non_object_answers_read_as_empty()
    {
        Api.On("GET /v1/status", 200, "[1,2,3]");
        Api.On("DELETE /v1/signals", _ => new FakeResponse(200, RawBody: [0xff, 0xfe, 0x00]));
        using var meter = Meter(token: Support.Token);
        Assert.Empty(await meter.StatusAsync());
        Assert.Empty(await meter.ClearAsync());
    }
}

public sealed class ErrorTests : ClientTestBase
{
    public static TheoryData<int, string, Type, string> Mapped => new()
    {
        { 401, "unauthorized", typeof(UnauthorizedException), "Busy Meter doesn't know this plugin's token" },
        { 403, "plugin_off", typeof(ForbiddenException), "This plugin is switched off in Busy Meter" },
        { 403, "not_allowed", typeof(ForbiddenException), "Register again asking for it in 'wants'" },
        { 403, "forbidden_origin", typeof(ForbiddenException), "carried an Origin header" },
        { 409, "already_pending", typeof(ConflictException), "already waiting for you in the Busy Meter tray" },
        { 409, "too_many_plugins", typeof(ConflictException), "the most plugins it takes (32)" },
        { 409, "ignored", typeof(ConflictException), "Busy Meter ignored the alert (paused)." },
        { 429, "cooldown", typeof(RateLimitedException), "denied in the last 10 minutes" },
        { 429, "too_many_pending", typeof(RateLimitedException), "Too many requests are waiting" },
        { 429, "withdraw_pause", typeof(RateLimitedException), "This plugin just withdrew a request; Busy Meter takes a new one after 5 seconds." },
        { 429, "rate_limited", typeof(RateLimitedException), "Too fast: paused" },
        { 400, "unknown_signal", typeof(BusyMeterException), "That signal wasn't declared" },
        { 400, "bad_ttl", typeof(BusyMeterException), "paused" },
        { 503, "busy", typeof(BusyMeterException), "paused" },
    };

    [Theory]
    [MemberData(nameof(Mapped))]
    public async Task Errors_map_to_types_and_friendly_messages(int status, string code, Type type, string message)
    {
        Api.On("PUT /v1/signals", _ => FakeResponse.Error(status, code, "paused"));
        using var meter = Meter(token: Support.Token);
        var e = await Assert.ThrowsAnyAsync<BusyMeterException>(() => meter.SetLevelAsync(3, 30));
        Assert.IsType(type, e);
        Assert.Equal(code, e.Code);
        Assert.Equal(status, e.Status);
        Assert.Contains(message, e.Message);
    }

    [Fact]
    public async Task Rate_limits_carry_retry_after()
    {
        Api.On("PUT /v1/signals", _ => new FakeResponse(429, """{"error":"rate_limited","message":"10 updates a second at most."}""",
            new Dictionary<string, string> { ["Retry-After"] = "1" }));
        using var meter = Meter(token: Support.Token);
        var e = await Assert.ThrowsAsync<RateLimitedException>(() => meter.SetLevelAsync(3, 30));
        Assert.Equal(TimeSpan.FromSeconds(1), e.RetryAfter);
        Assert.Equal("Too fast: 10 updates a second at most.", e.Message);
    }

    [Fact]
    public async Task Unknown_errors_use_the_server_text_or_the_status()
    {
        Api.OnSequence("GET /v1/status", FakeResponse.Error(500, "internal", "Something broke."), new FakeResponse(418));
        using var meter = Meter(token: Support.Token);
        var first = await Assert.ThrowsAsync<BusyMeterException>(() => meter.StatusAsync());
        Assert.Equal(("internal", "Something broke."), (first.Code, first.Message));
        var second = await Assert.ThrowsAsync<BusyMeterException>(() => meter.StatusAsync());
        Assert.Equal(("http_418", "Busy Meter answered 418."), (second.Code, second.Message));
    }

    [Fact]
    public async Task A_401_forgets_the_saved_token_but_keeps_a_waiting_request()
    {
        Store.SaveToken("com.example.test", Support.Token);
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On("GET /v1/status", _ => FakeResponse.Error(401, "unauthorized"));
        using var meter = Meter();
        Assert.Equal(Support.Token, meter.Token);

        await Assert.ThrowsAsync<UnauthorizedException>(() => meter.StatusAsync());
        Assert.Null(meter.Token);
        Assert.Null(Store.LoadToken("com.example.test"));
        Assert.Equal(Support.RequestId, Store.LoadPending("com.example.test"));
    }

    [Fact]
    public async Task No_token_yet_is_unauthorized_without_a_request()
    {
        using var meter = Meter();
        var e = await Assert.ThrowsAsync<UnauthorizedException>(() => meter.SetLevelAsync(2, 30));
        Assert.Contains("Register first", e.Message);
        Assert.Empty(Api.Requests);
    }

    [Fact]
    public async Task Nothing_listening_says_turn_on_External_connections()
    {
        using var meter = new BusyMeterClient("com.example.test", port: Support.ClosedPort(), token: Support.Token, store: Store);
        Assert.False(await meter.IsListeningAsync());
        var e = await Assert.ThrowsAsync<NotListeningException>(() => meter.SetLevelAsync(3, 30));
        Assert.Equal("not_listening", e.Code);
        Assert.Contains("External connections", e.Message);
        Assert.Equal(Messages.TurnOn, e.Message);
    }

    [Fact]
    public async Task A_silent_app_is_not_responding()
    {
        Api.On("GET /v1/status", _ => new FakeResponse(200, Hang: true));
        using var meter = Meter(token: Support.Token, timeout: TimeSpan.FromMilliseconds(300));
        var e = await Assert.ThrowsAsync<AppNotRespondingException>(() => meter.StatusAsync());
        Assert.Equal(Messages.NotResponding, e.Message);
        Assert.False(await meter.IsListeningAsync());
    }

    [Fact]
    public async Task A_dropped_connection_is_not_responding()
    {
        Api.On("GET /v1/status", _ => new FakeResponse(200, Drop: true));
        using var meter = Meter(token: Support.Token);
        var e = await Assert.ThrowsAsync<AppNotRespondingException>(() => meter.StatusAsync());
        Assert.Equal("not_responding", e.Code);
        Assert.Equal(Messages.ConnectionBroke, e.Message);
    }

    [Fact]
    public async Task The_callers_cancellation_is_not_an_app_error()
    {
        Api.On("GET /v1/status", _ => new FakeResponse(200, Hang: true));
        using var meter = Meter(token: Support.Token);
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => meter.StatusAsync(cts.Token));
    }

    [Fact]
    public async Task Listening_and_verify()
    {
        Api.OnSequence("GET /v1/status",
            FakeResponse.Error(401, "unauthorized"),                  // IsListening: any answer counts
            new FakeResponse(200, Support.StatusBody),                  // verify: known
            FakeResponse.Error(403, "not_allowed"),                     // verify: known, no "status" in wants
            FakeResponse.Error(401, "unauthorized"));                   // verify: revoked
        using var meter = Meter(token: Support.Token);
        Assert.True(await meter.IsListeningAsync());
        Assert.Null(Api.Requests[0].Header("Authorization"));
        Assert.True(await meter.VerifyTokenAsync());
        Assert.True(await meter.VerifyTokenAsync());
        Assert.False(await meter.VerifyTokenAsync());
        Assert.False(await meter.VerifyTokenAsync()); // no token any more: no request
        Assert.Equal(4, Api.Requests.Count);
    }
}

public sealed class RegistrationTests : ClientTestBase
{
    [Fact]
    public async Task Register_saves_the_request_and_poll_saves_the_token()
    {
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();

        var requestId = await meter.RegisterAsync("Test", [new SignalDeclaration("deep-work", "Deep work", 2)], ["signals", "status"], "1.0");
        Assert.Equal(Support.RequestId, requestId);
        Assert.Equal(Support.RequestId, meter.PendingRequest());
        Assert.Equal("""{"id":"com.example.test","signals":[{"name":"deep-work","label":"Deep work","suggest":2}],"wants":["signals","status"],"name":"Test","version":"1.0"}""",
            Encoding.UTF8.GetString(Api.Requests[0].Body));

        var reply = await meter.PollAsync(requestId);
        Assert.Equal("approved", reply["state"]!.GetValue<string>());
        Assert.Equal(Support.Token, meter.Token);
        var saved = Store.Load("com.example.test")!;
        Assert.Equal(Support.Token, saved["token"]!.GetValue<string>());
        Assert.Equal("deep-work", saved["signals"]![0]!["name"]!.GetValue<string>());
        Assert.Null(meter.PendingRequest());
    }

    [Fact]
    public async Task A_registration_without_a_request_id_is_a_bad_reply()
    {
        Api.On(Support.RegisterRoute, 202, "{}");
        using var meter = Meter();
        Assert.Equal("bad_reply", (await Assert.ThrowsAsync<BusyMeterException>(() => meter.RegisterAsync())).Code);
    }

    [Fact]
    public async Task Wait_polls_until_allowed_and_says_once_that_it_waits()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.OnSequence(Support.PollRoute, FakeResponse.Json(200, Support.Pending), FakeResponse.Json(200, Support.Pending), FakeResponse.Json(200, Support.Approved));
        using var meter = Meter();
        var told = 0;
        var token = await meter.WaitForApprovalAsync(interval: TimeSpan.FromMilliseconds(10), onWait: () => told++);
        Assert.Equal(Support.Token, token);
        Assert.Equal(1, told);
        Assert.Equal(3, Api.Requests.Count);
    }

    [Theory]
    [InlineData("denied", "The user clicked Deny in the Busy Meter tray.")]
    [InlineData("expired", "The request expired: nobody clicked Allow in the Busy Meter tray within 2 minutes. Try again.")]
    public async Task Wait_reports_a_deny_or_an_expiry(string reason, string message)
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.PollRoute, 200, $$"""{"state":"denied","reason":"{{reason}}"}""");
        using var meter = Meter();
        var e = await Assert.ThrowsAsync<DeniedException>(() => meter.WaitForApprovalAsync());
        Assert.Equal(reason, e.Code);
        Assert.Equal(message, e.Message);
        Assert.Null(meter.PendingRequest());
    }

    [Fact]
    public async Task Wait_on_a_forgotten_request_clears_it()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        using var meter = Meter(); // no route: the fake answers 404 not_found
        var e = await Assert.ThrowsAsync<BusyMeterException>(() => meter.WaitForApprovalAsync());
        Assert.Equal(("not_found", 404), (e.Code, e.Status));
        Assert.Null(meter.PendingRequest());
    }

    [Fact]
    public async Task Wait_gives_up_after_its_timeout()
    {
        Api.On(Support.PollRoute, 200, Support.Pending);
        using var meter = Meter();
        var e = await Assert.ThrowsAsync<ApprovalTimeoutException>(() =>
            meter.WaitForApprovalAsync(Support.RequestId, TimeSpan.FromMilliseconds(100), TimeSpan.FromMilliseconds(20)));
        Assert.Equal("pending", e.Code);
    }

    [Fact]
    public async Task Wait_with_nothing_to_wait_for()
    {
        using var meter = Meter();
        Assert.Equal("no_request", (await Assert.ThrowsAsync<BusyMeterException>(() => meter.WaitForApprovalAsync())).Code);
    }

    [Fact]
    public async Task Cancelling_a_wait_keeps_the_request_saved()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.PollRoute, 200, Support.Pending);
        using var meter = Meter();
        using var cts = new CancellationTokenSource();
        var wait = meter.WaitForApprovalAsync(interval: TimeSpan.FromSeconds(30), onWait: cts.Cancel, ct: cts.Token);
        var e = await Assert.ThrowsAsync<CancelledException>(() => wait);
        Assert.Equal("cancelled", e.Code);
        Assert.Equal("Stopped waiting for Allow.", e.Message);
        Assert.Equal(Support.RequestId, meter.PendingRequest());
    }

    [Fact]
    public async Task Cancelling_during_a_slow_poll_stops_too()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.PollRoute, _ => new FakeResponse(200, Hang: true));
        using var meter = Meter();
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        await Assert.ThrowsAsync<CancelledException>(() => meter.WaitForApprovalAsync(ct: cts.Token));
        Assert.Equal(Support.RequestId, meter.PendingRequest());
    }
}

public sealed class ConnectTests : ClientTestBase
{
    [Fact]
    public async Task A_known_saved_token_is_used_as_is()
    {
        Store.SaveToken("com.example.test", Support.Token);
        Api.On("GET /v1/status", 200, Support.StatusBody);
        using var meter = Meter();
        Assert.Equal(Support.Token, await meter.ConnectAsync("Test"));
        Assert.Equal(["GET /v1/status"], Api.Routes);
    }

    [Fact]
    public async Task A_switched_off_token_is_still_known()
    {
        Store.SaveToken("com.example.test", Support.Token);
        Api.On("GET /v1/status", _ => FakeResponse.Error(403, "plugin_off"));
        using var meter = Meter();
        Assert.Equal(Support.Token, await meter.ConnectAsync());
        Assert.Equal(["GET /v1/status"], Api.Routes);
    }

    [Fact]
    public async Task A_revoked_token_registers_again()
    {
        Store.SaveToken("com.example.test", "old" + Support.Token[3..]);
        Api.On("GET /v1/status", _ => FakeResponse.Error(401, "unauthorized"));
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();
        Assert.Equal(Support.Token, await meter.ConnectAsync("Test", ["deep-work"], ["signals", "status", "effects"]));
        Assert.Equal(["GET /v1/status", Support.RegisterRoute, Support.PollRoute], Api.Routes);
        Assert.Equal(Support.Token, Store.LoadToken("com.example.test"));
        Assert.Equal("""["signals","status","effects"]""", Store.Load("com.example.test")!["wants"]!.ToJsonString());
    }

    [Fact]
    public async Task A_saved_waiting_request_is_reused()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();
        Assert.Equal(Support.Token, await meter.ConnectAsync());
        Assert.Equal([Support.PollRoute], Api.Routes); // no new registration
    }

    [Fact]
    public async Task An_expired_or_forgotten_request_registers_again()
    {
        Store.SavePending("com.example.test", "rq_expired", [], ["signals"]);
        Api.On("GET /v1/register/rq_expired", 200, """{"state":"denied","reason":"expired"}""");
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();
        Assert.Equal(Support.Token, await meter.ConnectAsync());
        Assert.Equal(["GET /v1/register/rq_expired", Support.RegisterRoute, Support.PollRoute], Api.Routes);

        Store.Delete("com.example.test");
        Store.SavePending("com.example.test", "rq_gone", [], ["signals"]); // 404: unknown
        using var again = Meter();
        Assert.Equal(Support.Token, await again.ConnectAsync());
        Assert.Equal(["GET /v1/register/rq_gone", Support.RegisterRoute, Support.PollRoute], Api.Routes.Skip(3));
    }

    [Fact]
    public async Task A_deny_on_the_saved_request_is_not_retried()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.PollRoute, 200, """{"state":"denied","reason":"denied"}""");
        using var meter = Meter();
        await Assert.ThrowsAsync<DeniedException>(() => meter.ConnectAsync());
        Assert.Equal([Support.PollRoute], Api.Routes);
    }

    [Fact]
    public async Task Cancelled_before_asking_makes_no_request()
    {
        using var meter = Meter();
        using var cts = new CancellationTokenSource();
        cts.Cancel();
        var e = await Assert.ThrowsAsync<CancelledException>(() => meter.ConnectAsync(ct: cts.Token));
        Assert.Equal("Stopped before asking for Allow.", e.Message);
        Assert.Empty(Api.Requests);
    }

    [Fact]
    public async Task Cancelled_while_waiting_keeps_the_request()
    {
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        Api.On(Support.PollRoute, 200, Support.Pending);
        using var meter = Meter();
        using var cts = new CancellationTokenSource();
        await Assert.ThrowsAsync<CancelledException>(() => meter.ConnectAsync(onWait: cts.Cancel, ct: cts.Token));
        Assert.Equal(Support.RequestId, meter.PendingRequest());
    }

    private static FakeResponse WithdrawPause(string retryAfter) =>
        new(429, """{"error":"withdraw_pause","message":"x"}""", new Dictionary<string, string> { ["Retry-After"] = retryAfter });

    [Fact]
    public async Task A_withdraw_pause_is_waited_out_once()
    {
        Api.OnSequence(Support.RegisterRoute, WithdrawPause("1"), FakeResponse.Json(202, Support.Accepted));
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();
        var clock = System.Diagnostics.Stopwatch.StartNew();
        Assert.Equal(Support.Token, await meter.ConnectAsync());
        Assert.InRange(clock.Elapsed, TimeSpan.FromMilliseconds(900), TimeSpan.FromSeconds(4.5));
        Assert.Equal([Support.RegisterRoute, Support.RegisterRoute, Support.PollRoute], Api.Routes);
    }

    [Fact]
    public async Task A_second_withdraw_pause_is_thrown()
    {
        Api.On(Support.RegisterRoute, _ => WithdrawPause("1"));
        using var meter = Meter();
        var e = await Assert.ThrowsAsync<RateLimitedException>(() => meter.ConnectAsync());
        Assert.Equal("withdraw_pause", e.Code);
        Assert.Equal(2, Api.Requests.Count);
    }

    [Fact]
    public async Task Cancelling_the_withdraw_pause_wait_stops()
    {
        Api.On(Support.RegisterRoute, _ => WithdrawPause("5"));
        using var meter = Meter();
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        var clock = System.Diagnostics.Stopwatch.StartNew();
        await Assert.ThrowsAsync<CancelledException>(() => meter.ConnectAsync(ct: cts.Token));
        Assert.True(clock.Elapsed < TimeSpan.FromSeconds(3));
        Assert.Single(Api.Requests);
    }

    [Fact]
    public async Task Cancelling_during_the_register_post_still_saves_the_request()
    {
        Api.On(Support.RegisterRoute, _ => new FakeResponse(202, Support.Accepted, Delay: TimeSpan.FromMilliseconds(500)));
        Api.On(Support.PollRoute, 200, Support.Pending);
        using var meter = Meter();
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(100));
        await Assert.ThrowsAsync<CancelledException>(() => meter.ConnectAsync(ct: cts.Token));
        Assert.Equal(Support.RequestId, meter.PendingRequest()); // the prompt may be showing: keep its id for a withdraw
        Assert.Equal([Support.RegisterRoute], Api.Routes);
    }

    [Fact]
    public async Task Register_checks_the_cancellation_before_sending()
    {
        using var meter = Meter();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => meter.RegisterAsync(ct: new CancellationToken(canceled: true)));
        Assert.Empty(Api.Requests);
    }

    [Fact]
    public async Task Reconnecting_registers_again_once_after_a_revoke()
    {
        Store.SaveToken("com.example.test", Support.Token);
        Api.OnSequence("GET /v1/status", new FakeResponse(200, Support.StatusBody), FakeResponse.Error(401, "unauthorized"));
        Api.OnSequence("PUT /v1/signals", FakeResponse.Error(401, "unauthorized"), new FakeResponse(200, """{"active":["level-4"],"expiresInSeconds":60}"""));
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();
        await meter.ConnectAsync("Test", wants: ["signals"]);

        var reply = await meter.ReconnectingAsync(ct => meter.SetLevelAsync(4, 60, ct));
        Assert.Equal("level-4", reply["active"]![0]!.GetValue<string>());
        Assert.Equal(["GET /v1/status", "PUT /v1/signals", Support.RegisterRoute, Support.PollRoute, "PUT /v1/signals"], Api.Routes);
        Assert.Equal("""{"id":"com.example.test","signals":[],"wants":["signals"],"name":"Test"}""",
            Encoding.UTF8.GetString(Api.Requests[2].Body));
    }

    [Fact]
    public async Task Reconnecting_without_a_connect_just_throws()
    {
        Api.On("PUT /v1/signals", _ => FakeResponse.Error(401, "unauthorized"));
        using var meter = Meter(token: Support.Token);
        await Assert.ThrowsAsync<UnauthorizedException>(() => meter.ReconnectingAsync(ct => meter.SetLevelAsync(4, 60, ct)));
        Assert.Equal(["PUT /v1/signals"], Api.Routes);
    }

    [Fact]
    public void Forget_and_forget_token()
    {
        Store.SaveToken("com.example.test", Support.Token);
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        using var meter = Meter();
        Assert.Equal(Support.Token, meter.Token);
        meter.ForgetToken();
        Assert.Null(meter.Token);
        Assert.Equal(Support.RequestId, meter.PendingRequest());
        meter.Forget();
        Assert.Null(Store.Load("com.example.test"));
    }
}

public sealed class WithdrawTests : ClientTestBase
{
    [Fact]
    public async Task A_waiting_request_is_withdrawn_and_forgotten()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, 200, """{"state":"withdrawn"}""");
        using var meter = Meter();
        var result = await meter.WithdrawAsync();
        Assert.Equal(WithdrawOutcome.Withdrawn, result.Outcome);
        Assert.Null(meter.PendingRequest());
        Assert.Null(Store.Load("com.example.test")); // nothing else was saved: the file goes
        var request = Assert.Single(Api.Requests);
        Assert.Equal("DELETE /v1/register/" + Support.RequestId + " HTTP/1.1", request.RequestLine);
        Assert.Null(request.Header("Authorization"));
        Assert.Empty(request.Body);
    }

    [Fact]
    public async Task Withdrawing_keeps_an_older_token()
    {
        Store.SaveToken("com.example.test", Support.Token);
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, 200, """{"state":"withdrawn"}""");
        using var meter = Meter();
        await meter.WithdrawAsync(Support.RequestId);
        Assert.Null(meter.PendingRequest());
        Assert.Equal(Support.Token, Store.LoadToken("com.example.test"));
    }

    [Fact]
    public async Task Already_allowed_collects_and_saves_the_token()
    {
        Store.SavePending("com.example.test", Support.RequestId, [new JsonObject { ["name"] = "deep-work" }], ["signals", "status"]);
        Api.On(Support.WithdrawRoute, _ => new FakeResponse(409, """{"error":"already_answered","state":"approved","message":"The user already answered."}"""));
        Api.On(Support.PollRoute, 200, Support.Approved);
        using var meter = Meter();
        var result = await meter.WithdrawAsync();
        Assert.Equal(new WithdrawResult(WithdrawOutcome.Approved, Token: Support.Token), result);
        Assert.Equal(Support.Token, meter.Token);
        Assert.Equal(Support.Token, Store.LoadToken("com.example.test"));
        Assert.Equal("""["signals","status"]""", Store.Load("com.example.test")!["wants"]!.ToJsonString());
        Assert.Null(meter.PendingRequest());
        Assert.Equal([Support.WithdrawRoute, Support.PollRoute], Api.Routes);
    }

    [Theory]
    [InlineData("denied")]
    [InlineData("expired")]
    public async Task Already_denied_clears_the_request(string reason)
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, _ => new FakeResponse(409, """{"error":"already_answered","state":"denied","message":"The user already answered."}"""));
        Api.On(Support.PollRoute, 200, $$"""{"state":"denied","reason":"{{reason}}"}""");
        using var meter = Meter();
        Assert.Equal(new WithdrawResult(WithdrawOutcome.Denied, Reason: reason), await meter.WithdrawAsync());
        Assert.Null(meter.PendingRequest());
        Assert.Null(meter.Token);
    }

    [Fact]
    public async Task Already_answered_but_collected_since_is_not_found()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, _ => new FakeResponse(409, """{"error":"already_answered","state":"approved","message":"x"}"""));
        using var meter = Meter(); // the poll gets 404
        Assert.Equal(WithdrawOutcome.NotFound, (await meter.WithdrawAsync()).Outcome);
        Assert.Null(meter.PendingRequest());
    }

    [Fact]
    public async Task An_unknown_request_is_forgotten()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        using var meter = Meter(); // no route: 404 not_found
        Assert.Equal(WithdrawOutcome.NotFound, (await meter.WithdrawAsync()).Outcome);
        Assert.Null(meter.PendingRequest());
    }

    [Fact]
    public async Task Withdrawing_another_id_leaves_the_saved_one()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On("DELETE /v1/register/rq_other", 200, """{"state":"withdrawn"}""");
        using var meter = Meter();
        Assert.Equal(WithdrawOutcome.Withdrawn, (await meter.WithdrawAsync("rq_other")).Outcome);
        Assert.Equal(Support.RequestId, meter.PendingRequest());
    }

    [Fact]
    public async Task Nothing_to_withdraw()
    {
        using var meter = Meter();
        Assert.Equal(new WithdrawResult(WithdrawOutcome.None), await meter.WithdrawAsync());
        Assert.Empty(Api.Requests);
    }

    [Fact]
    public async Task Any_200_is_withdrawn()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, 200, "{}");
        using var meter = Meter();
        Assert.Equal(WithdrawOutcome.Withdrawn, (await meter.WithdrawAsync()).Outcome);
        Assert.Null(meter.PendingRequest());
    }

    [Fact]
    public async Task Errors_keep_retry_after()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, _ => new FakeResponse(429, """{"error":"rate_limited","message":"slow down"}""",
            new Dictionary<string, string> { ["Retry-After"] = "2" }));
        using var meter = Meter();
        var e = await Assert.ThrowsAsync<RateLimitedException>(() => meter.WithdrawAsync());
        Assert.Equal(TimeSpan.FromSeconds(2), e.RetryAfter);
        Assert.Equal(Support.RequestId, meter.PendingRequest());
    }

    [Fact]
    public void The_token_never_shows_in_ToString()
    {
        var text = new WithdrawResult(WithdrawOutcome.Approved, Token: Support.Token).ToString();
        Assert.DoesNotContain(Support.Token, text);
        Assert.Contains("Approved", text);
        Assert.Contains("(hidden)", text);
        Assert.Contains("Token = null", new WithdrawResult(WithdrawOutcome.Withdrawn).ToString());
    }

    [Fact]
    public async Task Other_errors_are_thrown_and_keep_the_request()
    {
        Store.SavePending("com.example.test", Support.RequestId, [], ["signals"]);
        Api.On(Support.WithdrawRoute, _ => FakeResponse.Error(503, "busy", "Too many requests in flight."));
        using var meter = Meter();
        var e = await Assert.ThrowsAsync<BusyMeterException>(() => meter.WithdrawAsync());
        Assert.Equal(("busy", 503), (e.Code, e.Status));
        Assert.Equal(Support.RequestId, meter.PendingRequest());
    }

    [Fact]
    public async Task After_a_cancelled_connect_withdraw_closes_the_prompt()
    {
        Api.On(Support.RegisterRoute, 202, Support.Accepted);
        Api.On(Support.PollRoute, 200, Support.Pending);
        Api.On(Support.WithdrawRoute, 200, """{"state":"withdrawn"}""");
        using var meter = Meter();
        using var cts = new CancellationTokenSource();
        await Assert.ThrowsAsync<CancelledException>(() => meter.ConnectAsync(onWait: cts.Cancel, ct: cts.Token));
        Assert.Equal(WithdrawOutcome.Withdrawn, (await meter.WithdrawAsync()).Outcome);
        Assert.Null(meter.PendingRequest());
    }
}
