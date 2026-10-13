using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BusyMeter.Plugins;

/// <summary>What <see cref="BusyMeterClient.WithdrawAsync"/> found.</summary>
public enum WithdrawOutcome
{
    /// <summary>Nothing was waiting (no request id saved): nothing was sent.</summary>
    None,

    /// <summary>The request was still waiting; its tray prompt closed. Nothing is saved for it any more.</summary>
    Withdrawn,

    /// <summary>The user had already clicked Allow; the token was collected and saved.</summary>
    Approved,

    /// <summary>The user had already clicked Deny (or the request expired).</summary>
    Denied,

    /// <summary>Busy Meter no longer knows the request (answered over 10 minutes ago, or already collected).</summary>
    NotFound,

    /// <summary>The poll after "already answered" still said pending (not expected).</summary>
    Pending,
}

/// <summary>The result of <see cref="BusyMeterClient.WithdrawAsync"/>.</summary>
/// <param name="Outcome">What happened.</param>
/// <param name="Token">The token, for <see cref="WithdrawOutcome.Approved"/>.</param>
/// <param name="Reason"><c>denied</c> or <c>expired</c>, for <see cref="WithdrawOutcome.Denied"/>.</param>
public sealed record WithdrawResult(WithdrawOutcome Outcome, string? Token = null, string? Reason = null)
{
    /// <summary>Never shows the token (a bearer secret): only whether there is one.</summary>
    public override string ToString() =>
        $"WithdrawResult {{ Outcome = {Outcome}, Token = {(Token is null ? "null" : "(hidden)")}, Reason = {Reason ?? "null"} }}";
}

/// <summary>
/// One plugin's connection to the Busy Meter app's local plugin API (spec: <c>plugins/spec/openapi.yaml</c>). A port of
/// the Python client (<c>busymeter.BusyMeter</c>) with the same rules and messages.
/// </summary>
/// <remarks>
/// <para>The app listens on 127.0.0.1 only, and only while the user has turned on Settings › Sources › External
/// connections. A program registers, the user clicks Allow in the Busy Meter tray, and the program gets a token, which
/// this client keeps in a per-user file (<see cref="TokenStore"/>). Nothing here can approve a request: only the user can.</para>
/// <para>Never sends an Origin or Cookie header, no query strings, JSON bodies only. One call at a time per instance.</para>
/// <code>
/// using var meter = new BusyMeterClient("com.example.cli");
/// await meter.ConnectAsync(name: "My script");   // registers and waits for Allow the first time
/// await meter.SetLevelAsync(5, ttlSeconds: 600);
/// </code>
/// </remarks>
public sealed class BusyMeterClient : IDisposable
{
    /// <summary>How long a connect may take: loopback connects are instant when something listens.</summary>
    public static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(1);

    /// <summary>The most of an answer this client reads.</summary>
    public const int MaxResponseBytes = 64 * 1024;

    private static readonly string[] DefaultWants = ["signals", "status"];
    private static readonly TimeSpan WithdrawPauseCap = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan DefaultApprovalTimeout = TimeSpan.FromSeconds(125);
    private static readonly HttpRequestOptionsKey<TimeSpan> ConnectTimeoutKey = new("busymeter.connectTimeout");

    private readonly int? _port;
    private readonly string? _discoveryFile;
    private readonly HttpClient _http;
    private string? _token;
    private ConnectArgs? _connectArgs;

    /// <summary>A client for <paramref name="pluginId"/>.</summary>
    /// <param name="pluginId">3-64 of a-z, 0-9, '.' and '-' (e.g. com.example.heartrate).</param>
    /// <param name="port">A port to use instead of the discovery file's (tokens are sent to it).</param>
    /// <param name="token">A token to use instead of the saved one.</param>
    /// <param name="store">Where tokens are kept (default: the per-user folder).</param>
    /// <param name="timeout">How long to wait for an answer (default 10 s).</param>
    /// <param name="discoveryFile">The discovery file (default <see cref="Discovery.DiscoveryPath"/>).</param>
    /// <exception cref="ArgumentException">Not a plugin id.</exception>
    public BusyMeterClient(string pluginId, int? port = null, string? token = null, TokenStore? store = null,
        TimeSpan? timeout = null, string? discoveryFile = null)
    {
        if (!Validation.IsValidPluginId(pluginId))
            throw new ArgumentException("plugin_id must be 3-64 of a-z, 0-9, '.' and '-' (e.g. com.example.heartrate).", nameof(pluginId));
        if (port is not null and not (>= 1 and <= 65535))
            throw new ArgumentOutOfRangeException(nameof(port), port, "port must be 1-65535.");
        PluginId = pluginId;
        _port = port;
        _discoveryFile = discoveryFile;
        _token = token;
        Store = store ?? new TokenStore();
        Timeout = timeout ?? TimeSpan.FromSeconds(10);
        _http = new HttpClient(CreateHandler(), disposeHandler: true) { Timeout = System.Threading.Timeout.InfiniteTimeSpan };
    }

    /// <summary>This plugin's id.</summary>
    public string PluginId { get; }

    /// <summary>Where the token is kept.</summary>
    public TokenStore Store { get; }

    /// <summary>How long to wait for an answer.</summary>
    public TimeSpan Timeout { get; set; }

    /// <summary>Where the API is: the port named in the constructor, else the discovery file's, else 47820 (untrusted).</summary>
    public Endpoint Endpoint => _port is int port ? new Endpoint(port, FromFile: false, Explicit: true) : Discovery.Discover(_discoveryFile);

    /// <summary>The token in use (the saved one, loaded on first use), or null.</summary>
    public string? Token => _token ??= Store.LoadToken(PluginId);

    /// <summary>The id of a registration still waiting for the user (saved in the token file), or null.</summary>
    public string? PendingRequest() => Store.LoadPending(PluginId);

    // ----- connection -----

    /// <summary>True when the API answers (External connections is on), whether or not this plugin has a token.</summary>
    public async Task<bool> IsListeningAsync(CancellationToken ct = default)
    {
        try
        {
            await RequestAsync(HttpMethod.Get, "/v1/status", auth: false, raiseForStatus: false, ct: ct).ConfigureAwait(false);
            return true;
        }
        catch (Exception e) when (e is NotListeningException or AppNotRespondingException)
        {
            return false;
        }
    }

    /// <summary>True when Busy Meter still knows this plugin's token (a 401 also deletes the saved token).</summary>
    public async Task<bool> VerifyTokenAsync(CancellationToken ct = default)
    {
        if (string.IsNullOrEmpty(Token))
            return false;
        try
        {
            await RequestAsync(HttpMethod.Get, "/v1/status", ct: ct).ConfigureAwait(false);
            return true;
        }
        catch (UnauthorizedException)
        {
            return false;
        }
        catch (ForbiddenException)
        {
            return true; // known token: switched off, or no "status" in the approval
        }
    }

    /// <summary>
    /// Run <paramref name="call"/>; if Busy Meter no longer knows the token (revoked), register again with the arguments
    /// of the last connect (the user clicks Allow again), then run it once more.
    /// </summary>
    public async Task<T> ReconnectingAsync<T>(Func<CancellationToken, Task<T>> call, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(call);
        try
        {
            return await call(ct).ConfigureAwait(false);
        }
        catch (UnauthorizedException) when (_connectArgs is not null)
        {
            var a = _connectArgs;
            await ConnectAsync(a.Name, a.Signals, a.Wants, a.Version, a.Timeout, a.OnWait, ct).ConfigureAwait(false);
            return await call(ct).ConfigureAwait(false);
        }
    }

    /// <inheritdoc cref="ReconnectingAsync{T}(Func{CancellationToken, Task{T}}, CancellationToken)"/>
    public Task ReconnectingAsync(Func<CancellationToken, Task> call, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(call);
        return ReconnectingAsync(async c => { await call(c).ConfigureAwait(false); return true; }, ct);
    }

    // ----- registration -----

    /// <summary>Ask the user to allow this plugin. Returns the request id (also kept in the token file).</summary>
    /// <param name="name">What the tray prompt calls the plugin.</param>
    /// <param name="signals">Signals to declare (at most 32).</param>
    /// <param name="wants">Any of signals, status, taps, effects (default signals and status).</param>
    /// <param name="version">The plugin's version, shown in the prompt.</param>
    /// <param name="ct">Checked before sending only (a request the app may already show is never dropped).</param>
    public async Task<string> RegisterAsync(string? name = null, IEnumerable<SignalDeclaration>? signals = null,
        IEnumerable<string>? wants = null, string? version = null, CancellationToken ct = default)
    {
        var declared = Validation.SignalDeclarations(signals);
        var wantList = (wants ?? DefaultWants).ToArray();
        var body = new JsonObject
        {
            ["id"] = PluginId,
            ["signals"] = declared.DeepClone(),
            ["wants"] = new JsonArray(wantList.Select(w => (JsonNode?)JsonValue.Create(w)).ToArray()),
        };
        if (!string.IsNullOrEmpty(name))
            body["name"] = name;
        if (!string.IsNullOrEmpty(version))
            body["version"] = version;
        // The caller's token is checked only before sending: once the POST is out, the app may show a prompt, so the
        // answer (bounded by the read timeout) is always read and its request id saved, for a later wait or withdraw.
        ct.ThrowIfCancellationRequested();
        var (_, reply, _) = await RequestAsync(HttpMethod.Post, "/v1/register", body, auth: false, ct: CancellationToken.None).ConfigureAwait(false);
        if (Text(reply["requestId"]) is not string requestId)
            throw new BusyMeterException("Busy Meter answered the registration without a request id.", "bad_reply");
        Store.SavePending(PluginId, requestId, declared, wantList);
        return requestId;
    }

    /// <summary>The raw answer: <c>{"state": "pending" | "denied" | "approved", ...}</c>. Saves the token when approved.</summary>
    /// <exception cref="ArgumentException">Not a request id.</exception>
    public async Task<JsonObject> PollAsync(string requestId, CancellationToken ct = default)
    {
        CheckRequestId(requestId);
        var (_, reply, _) = await RequestAsync(HttpMethod.Get, "/v1/register/" + requestId, auth: false, ct: ct).ConfigureAwait(false);
        var state = Text(reply["state"]);
        if (state == "approved" && Text(reply["token"]) is string token)
        {
            _token = token;
            Store.SaveToken(PluginId, token);
        }
        else if (state == "denied")
        {
            Store.ClearPending(PluginId);
        }
        return reply;
    }

    /// <summary>
    /// Poll until the user answers. Returns the token; throws <see cref="DeniedException"/> or
    /// <see cref="ApprovalTimeoutException"/>, or <see cref="CancelledException"/> as soon as <paramref name="ct"/> is
    /// cancelled (the request stays saved; <see cref="WithdrawAsync"/> closes its prompt).
    /// </summary>
    /// <param name="requestId">The request (default: the saved waiting one).</param>
    /// <param name="timeout">How long to wait (default 125 s: the app expires a request after 2 minutes).</param>
    /// <param name="interval">Time between polls (default 1 s).</param>
    /// <param name="onWait">Called once, after the first poll that says pending (e.g. to say "click Allow").</param>
    /// <param name="ct">Stops waiting.</param>
    public async Task<string> WaitForApprovalAsync(string? requestId = null, TimeSpan? timeout = null, TimeSpan? interval = null,
        Action? onWait = null, CancellationToken ct = default)
    {
        requestId ??= PendingRequest();
        if (string.IsNullOrEmpty(requestId))
            throw new BusyMeterException("Nothing to wait for: register first.", "no_request");
        var limit = timeout ?? DefaultApprovalTimeout;
        var pause = interval ?? TimeSpan.FromSeconds(1);
        var clock = Stopwatch.StartNew();
        var told = false;
        try
        {
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                JsonObject reply;
                try
                {
                    reply = await PollAsync(requestId, ct).ConfigureAwait(false);
                }
                catch (BusyMeterException e) when (e.Status == 404)
                {
                    Store.ClearPending(PluginId);
                    throw new BusyMeterException("Busy Meter no longer knows this request (answered over 10 minutes ago, "
                        + "or its token was already collected). Register again.", "not_found", 404, inner: e);
                }
                var state = Text(reply["state"]);
                if (state == "approved")
                    return _token!;
                if (state == "denied")
                {
                    var reason = Text(reply["reason"]) ?? "denied";
                    throw new DeniedException(reason == "expired"
                        ? "The request expired: nobody clicked Allow in the Busy Meter tray within 2 minutes. Try again."
                        : "The user clicked Deny in the Busy Meter tray.", reason);
                }
                if (!told && onWait is not null)
                {
                    onWait();
                    told = true;
                }
                if (clock.Elapsed >= limit)
                    throw new ApprovalTimeoutException("Still waiting for you to click Allow in the Busy Meter tray.");
                await Task.Delay(pause, ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException e) when (ct.IsCancellationRequested)
        {
            throw new CancelledException("Stopped waiting for Allow.", e);
        }
    }

    /// <summary>
    /// A token for this plugin: the saved one if Busy Meter still knows it, else wait on a saved request, else register
    /// and wait. A revoked token is deleted and the plugin registers again. <paramref name="ct"/> stops the wait
    /// (<see cref="CancelledException"/>), and no new request is made once it is cancelled.
    /// </summary>
    public async Task<string> ConnectAsync(string? name = null, IEnumerable<SignalDeclaration>? signals = null,
        IEnumerable<string>? wants = null, string? version = null, TimeSpan? timeout = null, Action? onWait = null,
        CancellationToken ct = default)
    {
        var signalList = signals?.ToList() ?? [];
        var wantList = (wants ?? DefaultWants).ToArray();
        Remember(name, signalList, wantList, version, timeout, onWait);
        try
        {
            if (!string.IsNullOrEmpty(Token) && await VerifyTokenAsync(ct).ConfigureAwait(false))
                return Token!;
            if (PendingRequest() is not null)
            {
                try
                {
                    return await WaitForApprovalAsync(timeout: timeout, onWait: onWait, ct: ct).ConfigureAwait(false);
                }
                catch (BusyMeterException e) when (e.Code is "not_found" or "expired")
                {
                }
            }
            if (ct.IsCancellationRequested)
                throw new CancelledException("Stopped before asking for Allow.");
            string requestId;
            try
            {
                requestId = await RegisterAsync(name, signalList, wantList, version, ct).ConfigureAwait(false);
            }
            catch (RateLimitedException e) when (e.Code == "withdraw_pause")
            {
                // This id withdrew a request a moment ago: the app takes a new one after 5 s. Wait that out once.
                var pause = e.RetryAfter is TimeSpan after && after > TimeSpan.Zero && after < WithdrawPauseCap ? after : WithdrawPauseCap;
                await Task.Delay(pause, ct).ConfigureAwait(false);
                requestId = await RegisterAsync(name, signalList, wantList, version, ct).ConfigureAwait(false);
            }
            return await WaitForApprovalAsync(requestId, timeout, onWait: onWait, ct: ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException e) when (ct.IsCancellationRequested)
        {
            throw new CancelledException("Stopped before asking for Allow.", e);
        }
    }

    /// <summary>
    /// Set what <see cref="ReconnectingAsync{T}"/> registers with after a revoke. <see cref="ConnectAsync"/> sets it;
    /// call it after a new registration (e.g. new signals) was approved through <see cref="RegisterAsync"/> and
    /// <see cref="PollAsync"/>.
    /// </summary>
    public void Remember(string? name = null, IEnumerable<SignalDeclaration>? signals = null, IEnumerable<string>? wants = null,
        string? version = null, TimeSpan? timeout = null, Action? onWait = null) =>
        _connectArgs = new ConnectArgs(name, signals?.ToList() ?? [], (wants ?? DefaultWants).ToArray(), version, timeout, onWait);

    /// <summary>
    /// Withdraw a registration that is still waiting (default: the saved one), so its tray prompt closes. Returns
    /// <see cref="WithdrawOutcome.Withdrawn"/>; when the user already answered, the poll's answer
    /// (<see cref="WithdrawOutcome.Approved"/> with the token saved as usual, or <see cref="WithdrawOutcome.Denied"/>);
    /// <see cref="WithdrawOutcome.NotFound"/> when Busy Meter no longer knows it; <see cref="WithdrawOutcome.None"/>
    /// when nothing is waiting. A withdrawal isn't a Deny, but the same id can't register again for 5 s, and a 3rd
    /// withdrawal in 10 minutes counts as a Deny.
    /// </summary>
    /// <exception cref="BusyMeterException">Another error from the app (with <see cref="BusyMeterException.RetryAfter"/>
    /// when it said).</exception>
    public async Task<WithdrawResult> WithdrawAsync(string? requestId = null, CancellationToken ct = default)
    {
        requestId ??= PendingRequest();
        if (string.IsNullOrEmpty(requestId))
            return new WithdrawResult(WithdrawOutcome.None);
        CheckRequestId(requestId);
        var (status, reply, retryAfter) = await RequestAsync(HttpMethod.Delete, "/v1/register/" + requestId, auth: false,
            raiseForStatus: false, ct: ct).ConfigureAwait(false);
        if (status == 200)
        {
            ClearPendingIf(requestId);
            return new WithdrawResult(WithdrawOutcome.Withdrawn);
        }
        if (status == 409 && Text(reply["error"]) == "already_answered")
        {
            try
            {
                var answer = await PollAsync(requestId, ct).ConfigureAwait(false); // collects the token once, or clears a denied request
                return Text(answer["state"]) switch
                {
                    "approved" => new WithdrawResult(WithdrawOutcome.Approved, Token: Text(answer["token"])),
                    "denied" => new WithdrawResult(WithdrawOutcome.Denied, Reason: Text(answer["reason"]) ?? "denied"),
                    _ => new WithdrawResult(WithdrawOutcome.Pending),
                };
            }
            catch (BusyMeterException e) when (e.Status == 404)
            {
            }
        }
        else if (status != 404)
        {
            throw Messages.ToException(status, reply, retryAfter);
        }
        ClearPendingIf(requestId);
        return new WithdrawResult(WithdrawOutcome.NotFound);
    }

    /// <summary>Drop the token (memory and file) but keep a waiting request.</summary>
    public void ForgetToken()
    {
        _token = null;
        Store.DropToken(PluginId);
    }

    /// <summary>Delete this plugin's saved token (the user can also Revoke it in the app).</summary>
    public void Forget()
    {
        _token = null;
        Store.Delete(PluginId);
    }

    // ----- signals, status, effects -----

    /// <summary>Replace this plugin's active signals for <paramref name="ttlSeconds"/> (1-600). An empty list clears.</summary>
    public async Task<JsonObject> SetSignalsAsync(IEnumerable<string> active, int ttlSeconds, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(active);
        var body = new JsonObject
        {
            ["active"] = new JsonArray(active.Select(a => (JsonNode?)JsonValue.Create(a)).ToArray()),
            ["ttlSeconds"] = Validation.CheckTtl(ttlSeconds),
        };
        return (await RequestAsync(HttpMethod.Put, "/v1/signals", body, ct: ct).ConfigureAwait(false)).Payload;
    }

    /// <summary>Show a level (0-6 stars or <see cref="MeterLevel.Flash"/>) for <paramref name="ttlSeconds"/> (1-600),
    /// through the user's level map.</summary>
    public async Task<JsonObject> SetLevelAsync(MeterLevel level, int ttlSeconds, CancellationToken ct = default)
    {
        var body = new JsonObject { ["level"] = level.ToJson(), ["ttlSeconds"] = Validation.CheckTtl(ttlSeconds) };
        return (await RequestAsync(HttpMethod.Put, "/v1/signals", body, ct: ct).ConfigureAwait(false)).Payload;
    }

    /// <summary>Clear this plugin's signals.</summary>
    public async Task<JsonObject> ClearAsync(CancellationToken ct = default) =>
        (await RequestAsync(HttpMethod.Delete, "/v1/signals", ct: ct).ConfigureAwait(false)).Payload;

    /// <summary><c>{"level": 0-6 | "flash" | "off", "paused": bool, "source": kind, ["plugin": own id]}</c>. Needs "status".</summary>
    public async Task<JsonObject> StatusAsync(CancellationToken ct = default) =>
        (await RequestAsync(HttpMethod.Get, "/v1/status", ct: ct).ConfigureAwait(false)).Payload;

    /// <summary>One short effect (lip-pulse, star-sweep, flash-short). Needs "effects"; one every 10 s.</summary>
    /// <param name="effect">lip-pulse, star-sweep or flash-short.</param>
    /// <param name="color"><c>#RRGGBB</c> (Gold by default; not used by flash-short).</param>
    /// <param name="durationMs">200-3000 (default 1500).</param>
    /// <param name="ct">Cancels the request.</param>
    public async Task<JsonObject> AlertAsync(string effect, string? color = null, int? durationMs = null, CancellationToken ct = default)
    {
        if (effect is null || !Validation.Effects.Contains(effect))
            throw new ArgumentException("effect must be lip-pulse, star-sweep or flash-short.", nameof(effect));
        var body = new JsonObject { ["effect"] = effect };
        if (color is not null)
            body["color"] = color;
        if (durationMs is int ms)
            body["durationMs"] = ms;
        var timeout = Timeout > TimeSpan.FromSeconds(10) ? Timeout : TimeSpan.FromSeconds(10); // answers when the effect is over
        return (await RequestAsync(HttpMethod.Post, "/v1/alert", body, timeout: timeout, ct: ct).ConfigureAwait(false)).Payload;
    }

    /// <inheritdoc/>
    public void Dispose() => _http.Dispose();

    // ----- HTTP -----

    internal async Task<(int Status, JsonObject Payload, string? RetryAfter)> RequestAsync(HttpMethod method, string path, JsonObject? body = null,
        bool auth = true, bool raiseForStatus = true, TimeSpan? timeout = null, CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(method, path)
        {
            Version = HttpVersion.Version11,
            VersionPolicy = HttpVersionPolicy.RequestVersionExact,
        };
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json")); // never an Origin header
        request.Headers.ConnectionClose = true; // a fresh connection per call, like the Python client
        if (auth)
        {
            var token = Token;
            if (string.IsNullOrEmpty(token))
                throw new UnauthorizedException($"No token for {PluginId} yet. Register first; you'll be asked to click Allow in the "
                    + "Busy Meter tray.");
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }
        if (body is not null)
        {
            var content = new ByteArrayContent(Encoding.UTF8.GetBytes(body.ToJsonString())); // no reflection: Native AOT safe
            content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            request.Content = content;
        }
        var endpoint = Endpoint;
        if (auth && !endpoint.Trusted)
        {
            // No discovery file: the app isn't listening. Whatever holds the default port isn't getting a token.
            throw new NotListeningException(Messages.NotRunning);
        }
        request.RequestUri = new Uri($"http://127.0.0.1:{endpoint.Port}{path}", UriKind.Absolute);
        var readTimeout = timeout ?? Timeout;
        request.Options.Set(ConnectTimeoutKey, readTimeout < ConnectTimeout ? readTimeout : ConnectTimeout);

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(readTimeout);
        int status;
        byte[] raw;
        string? retryAfter;
        try
        {
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token).ConfigureAwait(false);
            status = (int)response.StatusCode;
            retryAfter = response.Headers.TryGetValues("Retry-After", out var values) ? values.FirstOrDefault() : null;
            raw = await ReadCappedAsync(response, deadline.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException e)
        {
            throw new AppNotRespondingException(Messages.NotResponding, e);
        }
        catch (HttpRequestException e) when (e.InnerException is ConnectFailedException)
        {
            throw new NotListeningException(Messages.TurnOn, e);
        }
        catch (Exception e) when (e is HttpRequestException or IOException)
        {
            throw new AppNotRespondingException(Messages.ConnectionBroke, e);
        }

        JsonObject payload;
        try
        {
            payload = raw.Length > 0 && JsonNode.Parse(raw) is JsonObject o ? o : [];
            _ = payload.Count; // builds the object now, so a duplicate key throws here (ArgumentException), not later
        }
        catch (Exception e) when (e is JsonException or ArgumentException or InvalidOperationException)
        {
            payload = [];
        }
        if (auth && status == 401)
            ForgetToken(); // revoked or unknown: the next connect registers again
        if (raiseForStatus && status >= 400)
            throw Messages.ToException(status, payload, retryAfter);
        return (status, payload, retryAfter);
    }

    private static async Task<byte[]> ReadCappedAsync(HttpResponseMessage response, CancellationToken ct)
    {
        using var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        var buffer = new byte[MaxResponseBytes];
        var total = 0;
        while (total < buffer.Length)
        {
            var n = await stream.ReadAsync(buffer.AsMemory(total), ct).ConfigureAwait(false);
            if (n == 0)
                break;
            total += n;
        }
        return buffer.AsSpan(0, total).ToArray();
    }

    private static SocketsHttpHandler CreateHandler() => new()
    {
        UseProxy = false,
        Proxy = null,
        UseCookies = false,
        AllowAutoRedirect = false,
        AutomaticDecompression = DecompressionMethods.None,
        PreAuthenticate = false,
        Credentials = null,
        PooledConnectionLifetime = TimeSpan.Zero,
        MaxResponseHeadersLength = 64, // KB
        // Always 127.0.0.1 (never a name lookup), and a short connect: don't sit through Windows' SYN retries.
        ConnectCallback = async (context, ct) =>
        {
            var limit = context.InitialRequestMessage.Options.TryGetValue(ConnectTimeoutKey, out var t) ? t : ConnectTimeout;
            var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp) { NoDelay = true };
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(limit);
            try
            {
                await socket.ConnectAsync(new IPEndPoint(IPAddress.Loopback, context.DnsEndPoint.Port), cts.Token).ConfigureAwait(false);
                return new NetworkStream(socket, ownsSocket: true);
            }
            catch (Exception e) when (e is SocketException || (e is OperationCanceledException && !ct.IsCancellationRequested))
            {
                socket.Dispose();
                throw new ConnectFailedException(e);
            }
            catch
            {
                socket.Dispose();
                throw;
            }
        },
    };

    private void ClearPendingIf(string requestId)
    {
        if (PendingRequest() == requestId)
            Store.ClearPending(PluginId);
    }

    private static void CheckRequestId(string requestId)
    {
        if (!Validation.IsValidRequestId(requestId))
            throw new ArgumentException("Not a request id.", nameof(requestId));
    }

    private static string? Text(JsonNode? node) => node is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;

    private sealed record ConnectArgs(string? Name, List<SignalDeclaration> Signals, string[] Wants, string? Version, TimeSpan? Timeout, Action? OnWait);

    private sealed class ConnectFailedException(Exception inner) : IOException("Couldn't connect to 127.0.0.1.", inner);
}
