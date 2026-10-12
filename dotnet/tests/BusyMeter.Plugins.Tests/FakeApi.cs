using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;

namespace BusyMeter.Plugins.Tests;

/// <summary>One request as it arrived on the wire.</summary>
public sealed record FakeRequest(string RequestLine, string Method, string Target, IReadOnlyDictionary<string, string> Headers, byte[] Body)
{
    public string Route => Method + " " + Target;

    public JsonObject Json => JsonNode.Parse(Body)!.AsObject();

    public string? Header(string name) => Headers.TryGetValue(name, out var v) ? v : null;
}

/// <summary>What the fake answers. <see cref="Hang"/> accepts and never answers; <see cref="Drop"/> closes without an answer.</summary>
public sealed record FakeResponse(int Status, string? Body = null, IReadOnlyDictionary<string, string>? Headers = null,
    bool Hang = false, bool Drop = false, byte[]? RawBody = null, TimeSpan? Delay = null)
{
    public static FakeResponse Json(int status, string body) => new(status, body);

    public static FakeResponse Error(int status, string code, string message = "server text") =>
        new(status, new JsonObject { ["error"] = code, ["message"] = message }.ToJsonString());
}

/// <summary>A tiny HTTP/1.1 server on 127.0.0.1 (raw sockets, so the tests see exactly what the client sends).</summary>
public sealed class FakeApi : IAsyncDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _stop = new();
    private readonly Dictionary<string, Func<FakeRequest, FakeResponse>> _routes = [];
    private readonly List<FakeRequest> _requests = [];
    private readonly Task _loop;

    public FakeApi()
    {
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _loop = Task.Run(AcceptLoopAsync);
    }

    public int Port { get; }

    public IReadOnlyList<FakeRequest> Requests
    {
        get { lock (_requests) return _requests.ToList(); }
    }

    public IReadOnlyList<string> Routes => Requests.Select(r => r.Route).ToList();

    /// <summary>Answer <paramref name="route"/> ("GET /v1/status") with <paramref name="answer"/>.</summary>
    public FakeApi On(string route, Func<FakeRequest, FakeResponse> answer)
    {
        lock (_routes)
            _routes[route] = answer;
        return this;
    }

    public FakeApi On(string route, int status, string body) => On(route, _ => FakeResponse.Json(status, body));

    /// <summary>Answers in order; the last one repeats.</summary>
    public FakeApi OnSequence(string route, params FakeResponse[] answers)
    {
        var i = 0;
        return On(route, _ => answers[Math.Min(i++, answers.Length - 1)]);
    }

    private async Task AcceptLoopAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(_stop.Token);
            }
            catch (Exception)
            {
                return;
            }
            _ = Task.Run(() => ServeAsync(client));
        }
    }

    private async Task ServeAsync(TcpClient client)
    {
        using var _ = client;
        try
        {
            var stream = client.GetStream();
            var head = new List<byte>();
            var one = new byte[1];
            while (!EndsWithBlankLine(head))
            {
                if (await stream.ReadAsync(one, _stop.Token) == 0)
                    return;
                head.Add(one[0]);
            }
            var lines = Encoding.ASCII.GetString(head.ToArray()).Split("\r\n");
            var requestLine = lines[0];
            var parts = requestLine.Split(' ');
            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var line in lines.Skip(1).Where(l => l.Length > 0))
            {
                var colon = line.IndexOf(':');
                headers[line[..colon].Trim()] = line[(colon + 1)..].Trim();
            }
            var length = headers.TryGetValue("Content-Length", out var cl) ? int.Parse(cl) : 0;
            var body = new byte[length];
            var read = 0;
            while (read < length)
            {
                var n = await stream.ReadAsync(body.AsMemory(read), _stop.Token);
                if (n == 0)
                    break;
                read += n;
            }
            var request = new FakeRequest(requestLine, parts[0], parts.Length > 1 ? parts[1] : "", headers, body);
            lock (_requests)
                _requests.Add(request);

            Func<FakeRequest, FakeResponse>? route;
            lock (_routes)
                _routes.TryGetValue(request.Route, out route);
            var response = route?.Invoke(request) ?? FakeResponse.Error(404, "not_found", "No such endpoint.");
            if (response.Hang)
            {
                await Task.Delay(Timeout.Infinite, _stop.Token);
                return;
            }
            if (response.Drop)
                return;
            if (response.Delay is TimeSpan delay)
                await Task.Delay(delay, _stop.Token);
            var payload = response.RawBody ?? Encoding.UTF8.GetBytes(response.Body ?? "");
            var text = new StringBuilder();
            text.Append($"HTTP/1.1 {response.Status} X\r\n");
            text.Append("Content-Type: application/json; charset=utf-8\r\n");
            text.Append($"Content-Length: {payload.Length}\r\n");
            text.Append("Connection: close\r\n");
            foreach (var (k, v) in response.Headers ?? new Dictionary<string, string>())
                text.Append($"{k}: {v}\r\n");
            text.Append("\r\n");
            await stream.WriteAsync(Encoding.ASCII.GetBytes(text.ToString()), _stop.Token);
            await stream.WriteAsync(payload, _stop.Token);
            await stream.FlushAsync(_stop.Token);
        }
        catch (Exception e) when (e is IOException or OperationCanceledException or SocketException or ObjectDisposedException)
        {
        }
    }

    private static bool EndsWithBlankLine(List<byte> b) =>
        b.Count >= 4 && b[^4] == '\r' && b[^3] == '\n' && b[^2] == '\r' && b[^1] == '\n';

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        _listener.Stop();
        try { await _loop; }
        catch (Exception) { }
        _stop.Dispose();
    }
}
