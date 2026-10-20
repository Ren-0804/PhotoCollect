using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("PhotoCollect Transition Bridge")]
[assembly: AssemblyProduct("PhotoCollect")]
[assembly: AssemblyFileVersion("__FILE_VERSION__")]

internal static class Program
{
    private const string ProductVersion = "__VERSION__";
    private const string PayloadResourceName = "PhotoCollect.RealPayload";
    private const long PayloadSize = __PAYLOAD_SIZE__L;
    private const string PayloadSha256 = "__PAYLOAD_SHA256__";
    private const string WorkerResourceName = "PhotoCollect.TransitionWorker";
    private const long WorkerSize = __WORKER_SIZE__L;
    private const string WorkerSha256 = "__WORKER_SHA256__";

    private sealed class HealthArguments
    {
        internal bool Present;
        internal string MarkerPath;
        internal string Token;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        HealthArguments outer = null;
        string logPath = null;
        string preparedUpdateRoot = null;
        Exception failure = null;
        int exitCode = 1;
        if (args.Length == 2 && args[0] == "--bridge-verify-payload")
        {
            try
            {
                ExtractResource(
                    PayloadResourceName,
                    Path.GetFullPath(args[1]),
                    PayloadSize,
                    PayloadSha256);
                return 0;
            }
            catch { return 1; }
        }

        try
        {
            preparedUpdateRoot = GetUpdateRoot();
            Directory.CreateDirectory(preparedUpdateRoot);
            logPath = Path.Combine(
                preparedUpdateRoot,
                "transition-v" + ProductVersion + ".log");
            Log(
                logPath,
                "Bridge process entered with "
                    + args.Length.ToString(CultureInfo.InvariantCulture)
                    + " startup arguments.");
        }
        catch (Exception preparationError)
        {
            failure = preparationError;
            return 1;
        }

        Thread bootstrapThread = null;
        Form host = new Form();
        host.ShowInTaskbar = false;
        host.FormBorderStyle = FormBorderStyle.FixedToolWindow;
        host.StartPosition = FormStartPosition.Manual;
        host.Left = -32000;
        host.Top = -32000;
        host.Width = 1;
        host.Height = 1;
        host.Opacity = 0;
        host.Load += delegate
        {
            bootstrapThread = new Thread(delegate()
            {
                try
                {
                    outer = ParseHealthArguments(args);
                    RunBootstrap(outer, preparedUpdateRoot, logPath);
                    exitCode = 0;
                }
                catch (Exception error)
                {
                    failure = error;
                    if (!String.IsNullOrEmpty(logPath))
                        Log(logPath, "Bridge bootstrap failed: " + error.Message);
                }
                finally
                {
                    try
                    {
                        host.BeginInvoke((MethodInvoker)delegate { host.Close(); });
                    }
                    catch
                    {
                        Application.Exit();
                    }
                }
            });
            bootstrapThread.IsBackground = true;
            bootstrapThread.Start();
        };
        Application.Run(host);
        if (bootstrapThread != null)
            bootstrapThread.Join();
        if (failure != null && (outer == null || !outer.Present))
        {
            MessageBox.Show(
                "更新未能完成，原版本仍可从备份恢复。\r\n\r\n" + failure.Message,
                "集装箱照片收集工具",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
        }
        return exitCode;
    }

    private static void RunBootstrap(
        HealthArguments outer,
        string updateRoot,
        string logPath)
    {
        Process current = Process.GetCurrentProcess();
        string target = Path.GetFullPath(current.MainModule.FileName);
        string originalBackup = target + ".previous";
        if (!File.Exists(originalBackup))
            throw new FileNotFoundException(
                "找不到旧版本回滚备份。",
                originalBackup);
        if (outer.Present)
            ValidateOuterHealth(outer, updateRoot);

        string scope = outer.Present ? outer.Token : RandomToken();
        string prefix = "transition-v" + ProductVersion + "-" + scope;
        string payload = Path.Combine(updateRoot, prefix + ".payload.exe");
        string worker = Path.Combine(updateRoot, prefix + ".worker.exe");
        string state = Path.Combine(updateRoot, prefix + ".state");
        string handoff = Path.Combine(updateRoot, prefix + ".handoff");
        string finalToken = RandomToken();
        string finalHealth = Path.Combine(
            updateRoot,
            "health-" + finalToken + ".json");
        string bridgeBackup = target + ".bridge";

        DeleteIfExists(payload);
        DeleteIfExists(worker);
        DeleteIfExists(state);
        DeleteIfExists(handoff);
        DeleteIfExists(finalHealth);
        ExtractResource(PayloadResourceName, payload, PayloadSize, PayloadSha256);
        ExtractResource(WorkerResourceName, worker, WorkerSize, WorkerSha256);
        string bridgeHash = Sha256File(target);
        string originalHash = Sha256File(originalBackup);

        string[] values = new string[]
        {
            "1",
            ProductVersion,
            target,
            payload,
            originalBackup,
            originalHash,
            bridgeBackup,
            finalHealth,
            finalToken,
            current.Id.ToString(CultureInfo.InvariantCulture),
            current.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture),
            bridgeHash,
            worker,
            logPath,
            handoff,
            updateRoot
        };
        WriteState(state, values);
        Log(logPath, "Bridge artifacts and rollback state verified.");
        if (outer.Present)
        {
            WriteHealth(outer.MarkerPath, outer.Token, current.Id);
            Log(logPath, "Original updater accepted the durable bridge handoff.");
        }

        using (Process workerProcess = StartWorker(worker, state, updateRoot))
        {
            Thread.Sleep(150);
            if (workerProcess.HasExited)
                throw new InvalidOperationException(
                    "更新桥接工作进程启动失败，退出码 "
                        + workerProcess.ExitCode.ToString(CultureInfo.InvariantCulture) + "。");
            Log(logPath, "Bridge worker started.");

            DateTime deadline = DateTime.UtcNow.AddSeconds(150);
            while (DateTime.UtcNow < deadline)
            {
                if (File.Exists(handoff))
                {
                    Log(logPath, "Bridge handoff received; bootstrap will exit.");
                    return;
                }
                if (workerProcess.HasExited)
                    throw new InvalidOperationException(
                        "更新桥接工作进程在接管前退出。");
                Thread.Sleep(200);
            }
            throw new TimeoutException("等待更新桥接接管超时。");
        }
    }

    private static HealthArguments ParseHealthArguments(string[] args)
    {
        HealthArguments result = new HealthArguments();
        if (args.Length == 0)
            return result;
        if (args.Length != 4)
            throw new InvalidDataException("更新健康参数不完整。");
        for (int index = 0; index < args.Length; index += 2)
        {
            if (args[index] == "--post-update-health" && result.MarkerPath == null)
                result.MarkerPath = Path.GetFullPath(args[index + 1]);
            else if (args[index] == "--post-update-token" && result.Token == null)
                result.Token = args[index + 1];
            else
                throw new InvalidDataException("更新健康参数无效。");
        }
        if (result.MarkerPath == null || result.Token == null ||
            !Regex.IsMatch(result.Token, "^[0-9a-f]{32}$"))
            throw new InvalidDataException("更新健康参数无效。");
        result.Present = true;
        return result;
    }

    private static void ValidateOuterHealth(HealthArguments outer, string updateRoot)
    {
        string parent = Path.GetDirectoryName(outer.MarkerPath);
        if (!String.Equals(
            Path.GetFullPath(updateRoot).TrimEnd(Path.DirectorySeparatorChar),
            Path.GetFullPath(parent).TrimEnd(Path.DirectorySeparatorChar),
            StringComparison.OrdinalIgnoreCase) ||
            !String.Equals(
                Path.GetFileName(outer.MarkerPath),
                "health-" + outer.Token + ".json",
                StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("更新健康标记超出受控目录。");
    }

    private static string GetUpdateRoot()
    {
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (String.IsNullOrWhiteSpace(local))
            local = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
        if (String.IsNullOrWhiteSpace(local))
            throw new InvalidOperationException("无法确定本机更新目录。");
        return Path.GetFullPath(Path.Combine(local, "PhotoCollect", "updates"));
    }

    private static Process StartWorker(
        string worker,
        string state,
        string updateRoot)
    {
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = worker;
        start.Arguments = "--bridge-worker " + Quote(state);
        start.WorkingDirectory = updateRoot;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        Process process = Process.Start(start);
        if (process == null)
            throw new InvalidOperationException("无法启动更新桥接工作进程。");
        return process;
    }

    private static void WriteHealth(string path, string token, int pid)
    {
        string json = "{\"schema_version\":1,\"app\":\"PhotoCollect\","
            + "\"version\":\"" + ProductVersion + "\","
            + "\"token\":\"" + token + "\","
            + "\"pid\":" + pid.ToString(CultureInfo.InvariantCulture) + ","
            + "\"ready_at\":\"" + DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
            + "\"}";
        WriteAtomicText(path, json);
    }

    private static void WriteState(string path, string[] values)
    {
        string[] encoded = new string[values.Length];
        for (int index = 0; index < values.Length; index++)
            encoded[index] = Convert.ToBase64String(Encoding.UTF8.GetBytes(values[index]));
        string temporary = path + ".tmp." + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllLines(temporary, encoded, new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null, true);
            else File.Move(temporary, path);
        }
        finally
        {
            DeleteIfExists(temporary);
        }
    }

    private static void ExtractResource(
        string resourceName,
        string destination,
        long expectedSize,
        string expectedHash)
    {
        string parent = Path.GetDirectoryName(destination);
        if (String.IsNullOrEmpty(parent) || !Directory.Exists(parent))
            throw new InvalidOperationException("更新桥接暂存目录无效。");
        DeleteIfExists(destination);
        Assembly assembly = Assembly.GetExecutingAssembly();
        using (Stream source = assembly.GetManifestResourceStream(resourceName))
        {
            if (source == null)
                throw new InvalidOperationException("更新桥接内嵌资源缺失。");
            using (FileStream target = new FileStream(
                destination,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                1024 * 1024,
                FileOptions.WriteThrough))
            {
                source.CopyTo(target);
                target.Flush(true);
            }
        }
        AssertFile(destination, expectedSize, expectedHash);
    }

    private static void AssertFile(
        string path,
        long expectedSize,
        string expectedHash)
    {
        FileInfo item = new FileInfo(path);
        if (!item.Exists || item.Length != expectedSize ||
            !String.Equals(
                Sha256File(path),
                expectedHash,
                StringComparison.OrdinalIgnoreCase))
        {
            DeleteIfExists(path);
            throw new InvalidDataException("更新桥接内嵌资源校验失败。");
        }
    }

    private static string Sha256File(string path)
    {
        using (SHA256 algorithm = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
        {
            return BitConverter.ToString(algorithm.ComputeHash(stream))
                .Replace("-", "").ToLowerInvariant();
        }
    }

    private static string RandomToken()
    {
        byte[] data = new byte[16];
        using (RandomNumberGenerator random = RandomNumberGenerator.Create())
            random.GetBytes(data);
        return BitConverter.ToString(data).Replace("-", "").ToLowerInvariant();
    }

    private static void WriteAtomicText(string path, string content)
    {
        string temporary = path + ".tmp." + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, content, new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null, true);
            else File.Move(temporary, path);
        }
        finally
        {
            DeleteIfExists(temporary);
        }
    }

    private static string Quote(string value)
    {
        if (value.IndexOf('"') >= 0)
            throw new InvalidDataException("更新桥接路径包含无效字符。");
        return "\"" + value + "\"";
    }

    private static void Log(string path, string message)
    {
        try
        {
            File.AppendAllText(
                path,
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
                    + " " + message + Environment.NewLine,
                new UTF8Encoding(false));
        }
        catch { }
    }

    private static void DeleteIfExists(string path)
    {
        try
        {
            if (File.Exists(path)) File.Delete(path);
        }
        catch { }
    }
}
